import gzip
import logging
import socket
from typing import IO, Any, List, Union

from botocore.exceptions import IncompleteReadError, ReadTimeoutError
from mypy_boto3_omics.client import OmicsClient
from s3transfer.download import (
    DownloadChunkIterator,
    DownloadFilenameOutputManager,
    DownloadOutputManager,
)
from s3transfer.exceptions import RetriesExceededError
from s3transfer.futures import BoundedExecutor, TransferFuture
from s3transfer.subscribers import BaseSubscriber
from s3transfer.tasks import SubmissionTask, Task
from s3transfer.utils import (
    CountCallbackInvoker,
    FunctionContainer,
    StreamReaderProgress,
    get_callbacks,
    invoke_progress_callbacks,
)

from omics.common.omics_file_types import OmicsFileType
from omics.transfer import FileDownload
from omics.transfer.config import TransferConfig

logger = logging.getLogger(__name__)

# In python 3, all the socket related errors are in a newly created ConnectionError.
SOCKET_ERROR = ConnectionError
RETRYABLE_DOWNLOAD_ERRORS = (
    socket.timeout,
    ConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
)


class DownloadSubmissionTask(SubmissionTask):
    """Task for submitting tasks to execute a file download."""

    def _submit(
        self,
        transfer_future: TransferFuture,
        client: OmicsClient,
        config: TransferConfig,
        request_executor: BoundedExecutor,
        download_manager: DownloadOutputManager,
        io_executor: BoundedExecutor,
    ) -> None:
        # Get the needed progress callbacks for the task
        progress_callbacks = get_callbacks(transfer_future, "progress")

        # Get a handle to the file that will be used for writing downloaded contents
        fileobj = download_manager.get_fileobj_for_io_writes(transfer_future)

        transfer_args: FileDownload = transfer_future.meta.call_args  # type: ignore

        if transfer_args.omics_file_type == OmicsFileType.REFERENCE:
            metadata_response = client.get_reference_metadata(
                referenceStoreId=transfer_args.store_id, id=transfer_args.file_set_id
            )
            metadata_files = metadata_response["files"]
        elif transfer_args.omics_file_type == OmicsFileType.READSET:
            metadata_response = client.get_read_set_metadata(
                sequenceStoreId=transfer_args.store_id, id=transfer_args.file_set_id
            )  # type: ignore
            metadata_files = metadata_response["files"]
        else:
            raise AttributeError(f"Unexpected Omics file type: {transfer_args.omics_file_type}")

        filename_key = transfer_args.filename.lower()

        if filename_key not in metadata_files.keys():
            raise ValueError(
                f"File '{filename_key}' was not found in sequence store: {transfer_args.store_id}"
            )

        part_size = metadata_files[filename_key]["partSize"]
        num_parts = metadata_files[filename_key]["totalParts"]
        content_length = metadata_files[filename_key]["contentLength"]

        transfer_future.meta.provide_transfer_size(content_length)
        # Get any associated tags for the get object task.
        get_object_tag = download_manager.get_download_task_tag()

        # Create the callback function for finalizing the transfer
        io_finalize_callback = CountCallbackInvoker(
            FunctionContainer(
                self._transfer_coordinator.submit, io_executor, download_manager.get_final_io_task()
            )
        )

        for i in range(num_parts):
            io_finalize_callback.increment()
            self._transfer_coordinator.submit(
                request_executor,
                GetFileTask(
                    transfer_coordinator=self._transfer_coordinator,
                    main_kwargs={
                        "client": client,
                        "omics_file_type": transfer_args.omics_file_type,
                        "store_id": transfer_args.store_id,
                        "file_set_id": transfer_args.file_set_id,
                        "part_number": i + 1,
                        "fileobj": fileobj,
                        "file": transfer_args.filename,
                        "callbacks": progress_callbacks,
                        "max_attempts": config.num_download_attempts,
                        "start_index": i * part_size,
                        "download_output_manager": download_manager,
                        "io_chunksize": config.io_chunksize,
                    },
                    done_callbacks=[io_finalize_callback.decrement],
                ),
                tag=get_object_tag,
            )
        io_finalize_callback.finalize()


class GetFileTask(Task):
    """Task for executing a file download."""

    def _main(
        self,
        client: OmicsClient,
        omics_file_type: OmicsFileType,
        store_id: str,
        file_set_id: str,
        part_number: int,
        fileobj: Union[IO[Any], str, bytes],
        file: str,
        callbacks: List[BaseSubscriber],
        max_attempts: int,
        download_output_manager: DownloadOutputManager,
        io_chunksize: int,
        start_index: int = 0,
    ) -> None:
        last_exception = None
        for i in range(max_attempts):
            current_index = start_index
            try:
                if omics_file_type == OmicsFileType.REFERENCE:
                    response = client.get_reference(
                        referenceStoreId=store_id,
                        id=file_set_id,
                        partNumber=part_number,
                        file=file,
                    )
                elif omics_file_type == OmicsFileType.READSET:
                    response = client.get_read_set(
                        sequenceStoreId=store_id,
                        id=file_set_id,
                        partNumber=part_number,
                        file=file,
                    )
                else:
                    raise AttributeError(f"Unexpected Omics file type: {omics_file_type}")

                streaming_body = StreamReaderProgress(response["payload"], callbacks)
                chunks = DownloadChunkIterator(streaming_body, io_chunksize)
                for chunk in chunks:
                    # If the transfer is done because of a cancellation
                    # or error somewhere else, stop trying to submit more
                    # data to be written and break out of the download.
                    if not self._transfer_coordinator.done():
                        download_output_manager.queue_file_io_task(fileobj, chunk, current_index)
                        current_index += len(chunk)
                    else:
                        return
                return
            except RETRYABLE_DOWNLOAD_ERRORS as e:
                logger.debug(
                    "Retrying exception caught (%s), " "retrying request, (attempt %s / %s)",
                    e,
                    i,
                    max_attempts,
                    exc_info=True,
                )
                last_exception = e
                # Also invoke the progress callbacks to indicate that we
                # are trying to download the stream again and all progress
                # for this GetObject has been lost.
                invoke_progress_callbacks(callbacks, start_index - current_index)
                continue
        raise RetriesExceededError(last_exception)


class OmicsDownloadFilenameOutputManager(DownloadFilenameOutputManager):
    """Download manager for Omics files.

    Overrides the parent class to support modifying the filename after download.
    """

    def get_final_io_task(self):
        """Rename the file from the temporary file to its final location as the final IO task."""
        return OmicsIORenameFileTask(
            transfer_coordinator=self._transfer_coordinator,
            main_kwargs={
                "fileobj": self._temp_fileobj,
                "final_filename": self._final_filename,
                "osutil": self._osutil,
            },
            is_final=True,
        )


class OmicsIORenameFileTask(Task):
    """A task to rename a temporary file to its final filename.

    :param fileobj: The file handle that content was written to.
    :param final_filename: The final name of the file to rename to
        upon completion of writing the contents.
    :param osutil: OS utility
    """

    def _main(self, fileobj, final_filename, osutil):
        fileobj.close()
        if _file_is_gzipped(fileobj.name) and not final_filename.lower().endswith(".gz"):
            final_filename = final_filename + ".gz"
        osutil.rename_file(fileobj.name, final_filename)


def _file_is_gzipped(filename: str) -> bool:
    with gzip.open(filename, "r") as fh:
        try:
            fh.read(1)
        except gzip.BadGzipFile:
            return False
    return True
