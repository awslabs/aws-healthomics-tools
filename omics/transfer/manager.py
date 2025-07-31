import logging
import os
import re
from concurrent.futures import CancelledError
from typing import IO, Any, Dict, List, Optional, Type, Union

from mypy_boto3_omics.client import OmicsClient
from s3transfer.bandwidth import BandwidthLimiter, LeakyBucket
from s3transfer.download import (
    DownloadNonSeekableOutputManager,
    DownloadOutputManager,
    DownloadSeekableOutputManager,
)
from s3transfer.exceptions import FatalError
from s3transfer.futures import (
    BoundedExecutor,
    NonThreadedExecutor,
    TransferCoordinator,
    TransferFuture,
    TransferMeta,
)
from s3transfer.manager import TransferCoordinatorController
from s3transfer.utils import OSUtils, get_callbacks

from omics.common.omics_file_types import (
    OmicsFileType,
    ReadSetFileName,
    ReferenceFileName,
)
from omics.transfer import (
    FileDownload,
    OmicsTransferFuture,
    OmicsTransferSubscriber,
    ReadSetUpload,
)
from omics.transfer.config import TransferConfig
from omics.transfer.download import (
    DownloadSubmissionTask,
    OmicsDownloadFilenameOutputManager,
)
from omics.transfer.read_set_upload import ReadSetUploadSubmissionTask

DONE_CALLBACK_TYPE: str = "done"

# The only supported file type for references is FASTA.
REFERENCE_FILE_TYPE: str = "FASTA"

# Map of file type to data file extension.
FILE_TYPE_EXTENSION_MAP: dict[str, str] = {
    "FASTA": "fasta",
    "FASTQ": "fastq",
    "BAM": "bam",
    "CRAM": "cram",
    "UBAM": "bam",
}

# Map of file type to index file extension.
FILE_TYPE_INDEX_EXTENSION_MAP: dict[str, str] = {
    "FASTA": "fasta.fai",
    "BAM": "bam.bai",
    "CRAM": "cram.crai",
}

logger = logging.getLogger(__name__)


class TransferManager:
    """Omics data transfer manager which uses multiple threads for parallel processing."""

    def __init__(
        self,
        client: OmicsClient,
        config: Optional[TransferConfig] = None,
    ):
        """Initialize a Transfer Manager.

        Args:
            client: Client of the Amazon Omics Service.

            config: Configuration details for the Transfer Manager.
        """
        self._client = client
        self._config = config if config is not None else TransferConfig()
        self._osutil = OSUtils()
        self._coordinator_controller = TransferCoordinatorController()
        self._current_transfer_id = 1

        # A counter to create unique id's for each transfer submitted.
        self._id_counter = 0

        # Passing a NonThreadedExecutor to BoundedExecutor causes it to run
        # everything in a single thread.  The default is ThreadPoolExecutor.
        executor_cls = None if self._config.use_threads else NonThreadedExecutor

        # The executor responsible for making Omics API transfer requests
        self._request_executor = BoundedExecutor(
            max_size=self._config.max_request_queue_size,
            max_num_threads=self._config.max_request_concurrency,
            executor_cls=executor_cls,
        )

        # The executor responsible for submitting the necessary tasks to
        # perform the desired transfer
        self._submission_executor = BoundedExecutor(
            max_size=self._config.max_submission_queue_size,
            max_num_threads=self._config.max_submission_concurrency,
            executor_cls=executor_cls,
        )

        # There is one thread available for writing to disk. It will handle
        # downloads for all files.
        self._io_executor = BoundedExecutor(
            max_size=self._config.max_io_queue_size,
            max_num_threads=1,
            executor_cls=executor_cls,
        )

        # The component responsible for limiting bandwidth usage if it is configured.
        self._bandwidth_limiter = None
        if self._config.max_bandwidth is not None:
            logger.debug(f"Setting max_bandwidth to {self._config.max_bandwidth}")
            leaky_bucket = LeakyBucket(self._config.max_bandwidth)
            self._bandwidth_limiter = BandwidthLimiter(leaky_bucket)

    def download_reference(
        self,
        reference_store_id: str,
        reference_id: str,
        directory: Optional[str] = None,
        subscribers: List[OmicsTransferSubscriber] = [],
        wait: bool = True,
    ) -> List[OmicsTransferFuture]:
        """Download all files for an Omics reference dataset.

        Args:
            reference_store_id: ID of the Omics reference store.

            reference_id: ID of the Omics reference.

            directory: Local directory to place the files.
                Defaults to the value in TransferConfig.directory.

            subscribers: one or more subscribers for receiving transfer events.

            wait: True = block until all files have been downloaded (default).
                False = return a list of futures for controlling how to wait.
        """
        reference_metadata = self._client.get_reference_metadata(
            referenceStoreId=reference_store_id, id=reference_id
        )

        transfer_futures: List[OmicsTransferFuture] = []
        if directory is None:
            directory = self._config.directory
        _create_directory(directory)

        for filename in reference_metadata["files"]:
            reference_file = ReferenceFileName.from_object(filename.upper())
            file_path = os.path.join(
                directory,
                _format_local_filename(
                    reference_metadata["name"],
                    reference_file,
                    REFERENCE_FILE_TYPE,
                ),
            )

            transfer_future = self.download_reference_file(
                reference_store_id=reference_store_id,
                reference_id=reference_id,
                server_filename=reference_file,
                client_fileobj=file_path,
                subscribers=subscribers,
                wait=False,
            )
            transfer_futures.append(transfer_future)

        if wait:
            for future in transfer_futures:
                future.result()
        return transfer_futures

    def download_reference_file(
        self,
        reference_store_id: str,
        reference_id: str,
        server_filename: ReferenceFileName,
        client_fileobj: Optional[Union[IO[Any], str]] = None,
        subscribers: List[OmicsTransferSubscriber] = [],
        wait: bool = True,
    ) -> OmicsTransferFuture:
        """Download a single Omics reference file.

        Args:
            reference_store_id: ID of the Omics reference store.

            reference_id: ID of the Omics reference.

            server_filename: name of the file to download from the service.

            client_fileobj: either the name of a file to write to (a string variable)
                or an IO writer (ex: open file or data stream).

            subscribers: one or more subscribers for receiving transfer events.

            wait: True = block until all files have been downloaded (default).
                False = return a list of futures for controlling how to wait.
        """
        server_filename_enum = ReferenceFileName.from_object(server_filename)

        # If a file object was not supplied then format a filename from the original name
        if client_fileobj is None:
            reference_metadata = self._client.get_reference_metadata(
                referenceStoreId=reference_store_id, id=reference_id
            )
            _create_directory(self._config.directory)
            client_fileobj = os.path.join(
                self._config.directory,
                _format_local_filename(
                    reference_metadata["name"],
                    server_filename,
                    REFERENCE_FILE_TYPE,
                ),
            )

        return self._download_file(
            OmicsFileType.REFERENCE,
            reference_store_id,
            reference_id,
            server_filename_enum.value,
            client_fileobj,
            subscribers,
            wait,
        )

    def download_read_set(
        self,
        sequence_store_id: str,
        read_set_id: str,
        directory: Optional[str] = None,
        subscribers: List[OmicsTransferSubscriber] = [],
        wait: bool = True,
    ) -> List[OmicsTransferFuture]:
        """Download all files for an Omics read set.

        Args:
            sequence_store_id: ID of the Omics sequence store.

            read_set_id: ID of the Omics read set.

            directory: local directory to place the files.
                Defaults to the value in TransferConfig.directory.

            subscribers: one or more subscribers for receiving transfer events.

            wait: True = block until all files have been downloaded (default).
                False = return a list of futures for controlling how to wait.
        """
        read_set_metadata = self._client.get_read_set_metadata(
            sequenceStoreId=sequence_store_id, id=read_set_id
        )

        transfer_futures: List[OmicsTransferFuture] = []
        if directory is None:
            directory = self._config.directory
        _create_directory(directory)

        add_source_counter = True if "source2" in read_set_metadata["files"] else False

        for filename in read_set_metadata["files"]:
            read_set_file = ReadSetFileName.from_object(filename.upper())
            file_path = os.path.join(
                directory,
                _format_local_filename(
                    read_set_metadata["name"],
                    read_set_file,
                    read_set_metadata["fileType"],
                    add_source_counter,
                ),
            )

            transfer_future = self.download_read_set_file(
                sequence_store_id=sequence_store_id,
                read_set_id=read_set_id,
                server_filename=read_set_file,
                client_fileobj=file_path,
                subscribers=subscribers,
                wait=False,
            )
            transfer_futures.append(transfer_future)

        if wait:
            for future in transfer_futures:
                future.result()
        return transfer_futures

    def download_read_set_file(
        self,
        sequence_store_id: str,
        read_set_id: str,
        server_filename: ReadSetFileName,
        client_fileobj: Optional[Union[IO[Any], str]] = None,
        subscribers: List[OmicsTransferSubscriber] = [],
        wait: bool = True,
    ) -> OmicsTransferFuture:
        """Download a single Omics read set file.

        Args:
            sequence_store_id: ID of the Omics sequence store.

            read_set_id: ID of the Omics read set.

            server_filename: name of the file to download from the service.

            client_fileobj: either the name of a file to write to (a string variable)
                or an IO writer (ex: open file or data stream).

            subscribers: one or more subscribers for receiving transfer events.

            wait: True = block until all files have been downloaded (default).
                False = return a list of futures for controlling how to wait.
        """
        server_filename_enum = ReadSetFileName.from_object(server_filename)

        # If a file object was not supplied then format a filename from the original name
        if client_fileobj is None:
            read_set_metadata = self._client.get_read_set_metadata(
                sequenceStoreId=sequence_store_id, id=read_set_id
            )
            add_source_counter = True if "source2" in read_set_metadata["files"] else False
            _create_directory(self._config.directory)
            client_fileobj = os.path.join(
                self._config.directory,
                _format_local_filename(
                    read_set_metadata["name"],
                    server_filename,
                    read_set_metadata["fileType"],
                    add_source_counter,
                ),
            )

        return self._download_file(
            OmicsFileType.READSET,
            sequence_store_id,
            read_set_id,
            server_filename_enum.value,
            client_fileobj,
            subscribers,
            wait,
        )

    def upload_read_set(
        self,
        fileobjs: Union[IO[Any], str, List[Union[IO[Any], str]]],
        sequence_store_id: str,
        file_type: str,
        name: str,
        subject_id: str,
        sample_id: str,
        reference_arn: Optional[str] = None,
        generated_from: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        subscribers: Optional[List[OmicsTransferSubscriber]] = None,
        wait: bool = True,
    ) -> Union[TransferFuture, str]:
        """Upload files and create a read set.

        :param fileobjs: One or two file-like objects to be uploaded as a read set
        :param sequence_store_id: ID of the Omics sequence store.
        :param file_type: The type of file being uploaded.
        :param name: The name of the read set.
        :param subject_id: The subject ID for the read set.
        :param sample_id: The sample ID for the read set.
        :param reference_arn: The reference ARN.
        :param generated_from: Where the file was generated from.
        :param description: The description of the read set.
        :param tags: Tags to assign to the read set.
        :param subscribers: One or more subscribers for receiving transfer events.
        :param wait: True = block until all files have been uploaded (default).
            False = return a futures for controlling how to wait.
        :return: The ID of the created read set or a future returning it.
        """
        # Always treat fileobjs as a list even if we only have a single file
        fileobjs = fileobjs if type(fileobjs) is list else [fileobjs]  # type: ignore

        if len(fileobjs) > 2:
            raise AttributeError("at most two files can be uploaded to a read set")

        if len(fileobjs) > 1 and file_type != "FASTQ":
            raise AttributeError("paired end read files only supported for FASTQ")

        if (reference_arn is None) and (file_type not in ["FASTQ", "UBAM"]):
            raise AttributeError("Unlinked read set file types must specify a reference ARN")

        transfer_coordinator = self._get_future_coordinator()
        transfer_futures = []
        for fileobj in fileobjs:
            upload_args = ReadSetUpload(
                store_id=sequence_store_id,
                file_type=file_type,
                name=name,
                subject_id=subject_id,
                sample_id=sample_id,
                reference_arn=reference_arn,
                fileobj=fileobj,
                generated_from=generated_from,
                description=description,
                tags=tags,
                subscribers=subscribers,
            )

            transfer_meta = TransferMeta(upload_args, transfer_coordinator.transfer_id)
            transfer_futures.append(OmicsTransferFuture(transfer_meta, transfer_coordinator))

        transfer_future = transfer_futures[0]
        paired_transfer_future = None if len(transfer_futures) < 2 else transfer_futures[1]

        # Add any provided done callbacks to the first created transfer future
        # to be invoked on the entire upload being complete.
        for callback in get_callbacks(transfer_future, DONE_CALLBACK_TYPE):
            transfer_coordinator.add_done_callback(callback)

        # Return the upload task so that users can await the read set ID it creates
        self._submission_executor.submit(
            ReadSetUploadSubmissionTask(
                transfer_coordinator=transfer_coordinator,
                main_kwargs={
                    "client": self._client,
                    "osutil": self._osutil,
                    "request_executor": self._request_executor,
                    "transfer_future": transfer_future,
                    "paired_transfer_future": paired_transfer_future,
                    "bandwidth_limiter": self._bandwidth_limiter,
                },
            )
        )

        return transfer_future if not wait else transfer_future.result()

    def _get_future_coordinator(self) -> TransferCoordinator:
        transfer_id = self._get_next_transfer_id()
        # Creates a new transfer future along with its components
        transfer_coordinator = TransferCoordinator(transfer_id=transfer_id)
        # Track the transfer coordinator for transfers to manage.
        self._coordinator_controller.add_transfer_coordinator(transfer_coordinator)
        # Also make sure that the transfer coordinator is removed once
        # the transfer completes so it does not stick around in memory.
        transfer_coordinator.add_done_callback(
            self._coordinator_controller.remove_transfer_coordinator,
            transfer_coordinator,
        )
        return transfer_coordinator

    def _download_file(
        self,
        omics_file_type: OmicsFileType,
        store_id: str,
        file_set_id: str,
        server_filename: str,
        client_fileobj: Union[IO[Any], str],
        subscribers: List[OmicsTransferSubscriber] = [],
        wait: bool = False,
    ) -> OmicsTransferFuture:
        """Private helper method for downloading a file."""
        if client_fileobj is None:
            raise ValueError("client_fileobj parameter is required")

        transfer_coordinator = self._get_future_coordinator()
        if OmicsDownloadFilenameOutputManager.is_compatible(client_fileobj, self._osutil):
            download_manager: DownloadOutputManager = OmicsDownloadFilenameOutputManager(
                self._osutil, transfer_coordinator, self._io_executor
            )
        elif DownloadSeekableOutputManager.is_compatible(client_fileobj, self._osutil):
            download_manager = DownloadSeekableOutputManager(
                self._osutil, transfer_coordinator, self._io_executor
            )
        elif DownloadNonSeekableOutputManager.is_compatible(client_fileobj, self._osutil):
            download_manager = DownloadNonSeekableOutputManager(
                self._osutil, transfer_coordinator, self._io_executor
            )
        else:
            raise ValueError(f"The client_fileobj (type: {type(client_fileobj)}) is not supported")

        file_transfer = FileDownload(
            store_id=store_id,
            file_set_id=file_set_id,
            filename=server_filename,
            fileobj=client_fileobj,
            subscribers=subscribers,
            omics_file_type=omics_file_type,
        )
        transfer_meta = TransferMeta(file_transfer, transfer_coordinator.transfer_id)
        transfer_future = OmicsTransferFuture(transfer_meta, transfer_coordinator)

        # Add any provided done callbacks to the created transfer future
        # to be invoked on the transfer future being complete.
        for callback in get_callbacks(transfer_future, DONE_CALLBACK_TYPE):
            transfer_coordinator.add_done_callback(callback)

        main_kwargs = {
            "client": self._client,
            "config": self._config,
            "request_executor": self._request_executor,
            "transfer_future": transfer_future,
            "download_manager": download_manager,
            "io_executor": self._io_executor,
        }

        # Submit a SubmissionTask that will submit all of the necessary
        # tasks needed to complete the omics transfer.
        self._submission_executor.submit(
            DownloadSubmissionTask(
                transfer_coordinator=transfer_coordinator,
                main_kwargs=main_kwargs,
            )
        )
        if wait:
            transfer_future.result()

        return transfer_future

    def _get_next_transfer_id(self) -> int:
        self._current_transfer_id += 1
        return self._current_transfer_id - 1

    def __enter__(self) -> Any:
        """Return self when entering a 'with' block."""
        return self

    def __exit__(self, exc_type: Type[BaseException], exc_value: BaseException, *args: Any) -> None:
        """Clean up when a 'with' block is complete."""
        cancel = False
        cancel_msg = ""
        cancel_exc_type: Type[BaseException] = FatalError
        # If an exception was raised in the context handler, signal to cancel
        # all of the in progress futures in the shutdown.
        if exc_type:
            cancel = True
            cancel_msg = str(exc_value)
            if not cancel_msg:
                cancel_msg = repr(exc_value)
            # If it was a KeyboardInterrupt, the cancellation was initiated
            # by the user.
            if isinstance(exc_value, KeyboardInterrupt):
                cancel_exc_type = CancelledError
        self._shutdown(cancel, cancel_msg, cancel_exc_type)

    def shutdown(self, cancel: bool = False, cancel_msg: str = "") -> None:
        """Shut down the Transfer Manager.

        It will wait till all transfers complete before it completely shuts down.

        Args:
            cancel: If True, calls TransferFuture.cancel() for
                all in-progress in transfers. This is useful if you want the
                shutdown to happen faster.

            cancel_msg: The message to specify if canceling all in-progress transfers.
        """
        self._shutdown(cancel, cancel_msg)

    def _shutdown(
        self,
        cancel: bool,
        cancel_msg: str,
        exc_type: Type[BaseException] = CancelledError,
    ) -> None:
        if cancel:
            # Cancel all in-flight transfers if requested, before waiting
            # for them to complete.
            self._coordinator_controller.cancel(cancel_msg, exc_type)
        try:
            # Wait until there are no more in-progress transfers. This is
            # wrapped in a try statement because this can be interrupted
            # with a KeyboardInterrupt that needs to be caught.
            self._coordinator_controller.wait()
        except KeyboardInterrupt:
            # If not errors were raised in the try block, the cancel should
            # have no coordinators it needs to run cancel on. If there was
            # an error raised in the try statement we want to cancel all of
            # the inflight transfers before shutting down to speed that
            # process up.
            self._coordinator_controller.cancel("KeyboardInterrupt()")
            raise
        finally:
            # Shutdown all of the executors.
            self._submission_executor.shutdown()
            self._request_executor.shutdown()
            self._io_executor.shutdown()


def _create_directory(directory: str) -> None:
    """Create a directory if one does not exist yet."""
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)


def _format_local_filename(
    file_name: str,
    server_filename: Union[ReferenceFileName, ReadSetFileName],
    file_type: str,
    add_source_counter: bool = False,
) -> str:
    """Format the name of the local file from the name and file type.

    Args:
        file_name: The name of the file as specified in the manifest.

        server_filename: The name of the file as it is stored on the server (ex: index, source, source1, source2).

        file_type: The type of the file on the server.

        add_source_counter: Whether to add `_1` or `_2` to the name before the extension.
            (This should be true if there is both a source1 and source2 file on the server)
    """
    # File type should be uppercase, though we support passing in lower case.
    file_type = file_type.upper()

    # Remove extensions that that users may have added to the name since we will be adding our own.
    file_name = _remove_file_extensions(file_name)

    # Remove or replace characters that are not valid for a file name.
    file_name = _get_valid_filename(file_name)

    # Handle index file names first.
    if server_filename in [ReferenceFileName.INDEX, ReadSetFileName.INDEX]:
        if file_type in FILE_TYPE_INDEX_EXTENSION_MAP:
            return f"{file_name}.{FILE_TYPE_INDEX_EXTENSION_MAP[file_type]}"
        else:
            print(
                f"Unexpected file type: {file_type}.  Applying extension 'index' to the index file."
            )
            return f"{file_name}.index"

    # Apply a counter to the file if necessary.
    if server_filename == ReadSetFileName.SOURCE2:
        file_name = file_name + "_2"
    elif add_source_counter:
        file_name = file_name + "_1"

    # Apply the extension.
    if file_type in FILE_TYPE_EXTENSION_MAP:
        file_name = f"{file_name}.{FILE_TYPE_EXTENSION_MAP[file_type]}"
    else:
        print(f"Unexpected file type: {file_type}.  No extension applied.")

    return file_name


def _remove_file_extensions(file_name: str) -> str:
    """Remove Omics file extensions from the file name."""
    # First remove gzip extension (if present).
    if file_name[-3:].lower() == ".gz":
        file_name = file_name[:-3]

    # Iterate the possible omics file types.
    for ext in FILE_TYPE_EXTENSION_MAP.values():
        ext = "." + ext  # We're interested in the extension with the period
        if file_name[(-len(ext)) :].lower() == ext:
            file_name = file_name[: (-len(ext))]

    return file_name


def _get_valid_filename(name: str):
    """
    Taken from `https://github.com/django/django/blob/main/django/utils/text.py`.

    Return the given string converted to a string that can be used for a clean
    filename. Remove leading and trailing spaces; convert other spaces to
    underscores; and remove anything that is not an alphanumeric, dash,
    underscore, or dot.
    >>> get_valid_filename("john's portrait in 2004.jpg")
    'johns_portrait_in_2004.jpg'
    """
    s = str(name).strip().replace(" ", "_")
    s = re.sub(r"(?u)[^-\w.]", "", s)
    if s in {"", ".", ".."}:
        raise ValueError(f"Could not derive file name from '{s}'")
    return s
