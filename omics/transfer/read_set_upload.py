import logging
from typing import IO, Any, Awaitable, Dict, List, Optional

from mypy_boto3_omics.client import OmicsClient
from s3transfer.bandwidth import BandwidthLimiter
from s3transfer.futures import BoundedExecutor, TransferFuture
from s3transfer.tasks import SubmissionTask, Task
from s3transfer.upload import (
    UploadFilenameInputManager,
    UploadInputManager,
    UploadNonSeekableInputManager,
    UploadSeekableInputManager,
)
from s3transfer.utils import ChunksizeAdjuster, OSUtils

from omics.common.omics_file_types import ReadSetFileName
from omics.transfer import ReadSetUpload

logger = logging.getLogger(__name__)

MIB_BYTES = 1024 * 1024
UPLOAD_PART_SIZE_BYTES = MIB_BYTES * 100  # 100MiB


class CreateMultipartReadSetUploadTask(Task):
    """Task to initiate a multipart upload."""

    def _main(
        self,
        client: OmicsClient,
        create_args: ReadSetUpload,
    ) -> str:
        """Run the task.

        :param client: The client to use when calling CreateMultipartReadSetUpload
        :param create_args: The arguments to pass to the multipart upload request
        :returns: The upload ID.
        """
        args = {
            "sequenceStoreId": create_args.store_id,
            "sourceFileType": create_args.file_type,
            "subjectId": create_args.subject_id,
            "sampleId": create_args.sample_id,
            "generatedFrom": create_args.generated_from,
            "referenceArn": create_args.reference_arn,
            "name": create_args.name,
            "description": create_args.description,
            "tags": create_args.tags,
        }
        response = client.create_multipart_read_set_upload(
            # Filter out non-required null args to make validation happy
            **{k: v for k, v in args.items() if v is not None}
        )
        upload_id = response["uploadId"]

        if (args["referenceArn"] == "") and (args["sourceFileType"] not in ["FASTQ", "UBAM"]):
            raise AttributeError("Unlinked read set file types must specify a reference ARN")

        # Add a cleanup if the multipart upload fails at any point.
        self._transfer_coordinator.add_failure_cleanup(
            client.abort_multipart_read_set_upload,
            sequenceStoreId=create_args.store_id,
            uploadId=upload_id,
        )

        return upload_id


class UploadReadSetPartTask(Task):
    """Task to upload a part in a multipart upload."""

    def _main(
        self,
        client: OmicsClient,
        fileobj: IO,
        upload_id: str,
        store_id: str,
        part_source: str,
        part_number: int,
    ) -> Dict[str, Any]:
        """Run the task.

        :param client: The client to use when calling UploadReadSetPart
        :param fileobj: The file to upload.
        :param upload_id: The id of the upload
        :param store_id: The store id
        :param part_source: The type of reads being uploaded
        :param part_number: The number representing the part of the multipart upload
        :returns: A dictionary representing a part:
            {'checksum': checksum_value, 'partNumber': part_number, 'file': file_key}
            This value can be appended to a list to be used to complete the multipart upload.
        """
        with fileobj as body:
            response = client.upload_read_set_part(
                sequenceStoreId=store_id,
                uploadId=upload_id,
                partSource=part_source,
                partNumber=part_number,
                payload=body,
            )
        checksum = response["checksum"]
        part_metadata = {"partSource": part_source, "partNumber": part_number, "checksum": checksum}
        return part_metadata


class CompleteMultipartReadSetUploadTask(Task):
    """Task to complete a multipart upload."""

    def _main(
        self,
        client: OmicsClient,
        sequence_store_id: str,
        upload_id: str,
        parts: List[Dict[str, Any]],
    ) -> str:
        """Run the task.

        :param client: The client to use when calling CompleteMultipartReadSetUpload
        :param sequence_store_id: The sequence store id
        :param upload_id: The id of the upload
        :param parts: A list of file parts that were uploaded
        :returns: The newly created read set ID
        """
        response = client.complete_multipart_read_set_upload(
            sequenceStoreId=sequence_store_id,
            uploadId=upload_id,
            parts=parts,
        )
        return response["readSetId"]


class ReadSetUploadSubmissionTask(SubmissionTask):
    """Task for submitting tasks to execute an upload."""

    def _get_upload_input_manager_cls(self, fileobj: IO) -> type[UploadInputManager]:
        """Retrieve a class for managing input for an upload based on file type.

        :param fileobj: The file object being uploaded
        :returns: The appropriate class to use for managing a specific type of
            input for uploads.
        """
        upload_manager_resolver_chain = [
            UploadFilenameInputManager,
            UploadSeekableInputManager,
            UploadNonSeekableInputManager,
        ]

        for upload_manager_cls in upload_manager_resolver_chain:
            if upload_manager_cls.is_compatible(fileobj):
                return upload_manager_cls
        raise RuntimeError(f"Input {fileobj} of type: {type(fileobj)} is not supported.")

    def _submit(
        self,
        client: OmicsClient,
        osutil: OSUtils,
        request_executor: BoundedExecutor,
        transfer_future: TransferFuture,
        paired_transfer_future: Optional[TransferFuture],
        bandwidth_limiter: Optional[BandwidthLimiter] = None,
    ) -> None:
        """Submit the task.

        :param client: The client associated with the transfer manager
        :param osutil: The os utility associated with the transfer manager
        :param request_executor: The request executor associated with the
            transfer manager
        :param transfer_future: The transfer future associated with the
            transfer request that tasks are to be submitted for
        :param paired_transfer_future: The paired read transfer future associated
            with the transfer request that tasks are to be submitted for
        :param bandwidth_limiter: The bandwidth limiter to use for
            limiting bandwidth during the upload.
        """
        upload_args: ReadSetUpload = transfer_future.meta.call_args  # type: ignore

        # Submit the request to create a multipart upload.
        create_multipart_future = self._transfer_coordinator.submit(
            request_executor,
            CreateMultipartReadSetUploadTask(
                transfer_coordinator=self._transfer_coordinator,
                main_kwargs={
                    "client": client,
                    "create_args": upload_args,
                },
            ),
        )

        # Submit requests to upload the parts of the files.
        part_futures = []
        for i, transfer_future in enumerate([transfer_future, paired_transfer_future]):  # type: ignore
            if transfer_future is None:
                continue
            part_futures.extend(
                self._submit_upload_file_part_requests(
                    client=client,
                    osutil=osutil,
                    request_executor=request_executor,
                    transfer_future=transfer_future,
                    create_multipart_future=create_multipart_future,
                    part_source=ReadSetFileName.SOURCE1 if i == 0 else ReadSetFileName.SOURCE2,
                    bandwidth_limiter=bandwidth_limiter,
                )
            )

        # Submit the request to complete the multipart upload.
        self._transfer_coordinator.submit(
            request_executor,
            CompleteMultipartReadSetUploadTask(
                transfer_coordinator=self._transfer_coordinator,
                main_kwargs={
                    "client": client,
                    "sequence_store_id": upload_args.store_id,
                },
                pending_main_kwargs={
                    "upload_id": create_multipart_future,
                    "parts": part_futures,
                },
                is_final=True,
            ),
        )

    def _submit_upload_file_part_requests(
        self,
        client: OmicsClient,
        osutil: OSUtils,
        request_executor: BoundedExecutor,
        transfer_future: TransferFuture,
        create_multipart_future: Awaitable[str],
        part_source: ReadSetFileName,
        bandwidth_limiter: Optional[BandwidthLimiter] = None,
    ):
        """Submit the upload futures for each task.

        :param client: The client associated with the transfer manager
        :param osutil: The os utility associated with the transfer manager
        :param request_executor: The request executor associated with the
            transfer manager
        :param transfer_future: The transfer future associated with the
            transfer request that tasks are to be submitted for
        :param create_multipart_future: A future that creates the multipart upload
        :param part_source: The type of reads being uploaded
        :param bandwidth_limiter: The bandwidth limiter to use for
            limiting bandwidth during the upload.
        """
        # Get the relevant transfer data out of the transfer future
        upload_args: ReadSetUpload = transfer_future.meta.call_args  # type: ignore
        upload_input_manager = self._get_upload_input_manager_cls(upload_args.fileobj)(
            osutil, self._transfer_coordinator, bandwidth_limiter
        )

        # Determine the size if it was not provided
        if transfer_future.meta.size is None:
            upload_input_manager.provide_transfer_size(transfer_future)

        # Get any tags that need to be associated to the submitted task for upload the data
        # TODO: Support tag semaphores to control memory usage
        #       https://github.com/boto/s3transfer/blob/f225103e69f2e2b61e8933ad816519229afaf088/s3transfer/manager.py#L244-L251
        # upload_part_tag = IN_MEMORY_UPLOAD_TAG if upload_input_manager.stores_body_in_memory("upload_part") else None

        part_futures = []
        adjuster = ChunksizeAdjuster()
        chunksize = adjuster.adjust_chunksize(UPLOAD_PART_SIZE_BYTES, transfer_future.meta.size)
        part_iterator = upload_input_manager.yield_upload_part_bodies(transfer_future, chunksize)

        for part_number, fileobj in part_iterator:
            part_futures.append(
                self._transfer_coordinator.submit(
                    request_executor,
                    UploadReadSetPartTask(
                        transfer_coordinator=self._transfer_coordinator,
                        main_kwargs={
                            "client": client,
                            "fileobj": fileobj,
                            "store_id": upload_args.store_id,
                            "part_source": part_source.value,
                            "part_number": part_number,
                        },
                        pending_main_kwargs={"upload_id": create_multipart_future},
                    ),
                )
            )

        return part_futures
