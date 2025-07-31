from typing import IO, Any, Dict, List, Optional, Union

from s3transfer.futures import TransferFuture
from s3transfer.subscribers import BaseSubscriber

from omics.common.omics_file_types import OmicsFileType


class OmicsTransferSubscriber(BaseSubscriber):
    """Base class for subscribers of Omics data transfer."""


class OmicsTransferFuture(TransferFuture):
    """Future for getting the result of Omics data transfer."""


class FileDownload:
    """Details of an Omics file download."""

    def __init__(
        self,
        store_id: str,
        file_set_id: str,
        filename: str,
        fileobj: Union[IO[Any], str],
        omics_file_type: OmicsFileType,
        subscribers: Optional[List[BaseSubscriber]] = None,
    ):
        """Details of a file download.

        Args:
            store_id: the ID of the data store (either Reference Store or Sequence Store).

            file_set_id: Reference ID or Read Set ID.

            filename: the name of the file when it is stored on the server.

            fileobj: The name of a file or IO object to transfer data to.

            omics_file_type: the type of Omics file being transferred.

            subscribers: The list of subscribers to be invoked in the
                order provided based on the event emit during the process of
                the transfer request.
        """
        self.omics_file_type = omics_file_type
        self.store_id = store_id
        self.file_set_id = file_set_id
        self.filename = filename
        self.fileobj = fileobj
        self.subscribers = [] if subscribers is None else subscribers

        if filename is None:
            raise AttributeError("filename cannot be None")


class ReadSetUpload:
    """Details of an Omics read set upload."""

    def __init__(
        self,
        store_id: str,
        file_type: str,
        name: str,
        subject_id: str,
        sample_id: str,
        fileobj: Union[IO[Any], str],
        reference_arn: Optional[str] = None,
        generated_from: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        subscribers: Optional[List[BaseSubscriber]] = None,
    ):
        """Details of a read set upload.

        :param store_id: The store ID
        :param file_type: The read set file type being uploaded
        :param name: The name of the read set
        :param subject_id: The subject for the read set
        :param sample_id: The sample for the read set
        :param reference_arn: The reference ARN
        :param fileobj: The file being uploaded
        :param generated_from: Where the file was generated from
        :param description: The description of the read set
        :param tags: Tags to add to the read set
        :param subscribers: The list of subscribers to be invoked in the
                order provided based on the event emit during the process of
                the transfer request.
        """
        self.store_id = store_id
        self.file_type = file_type
        self.name = name
        self.subject_id = subject_id
        self.sample_id = sample_id
        self.reference_arn = reference_arn
        self.fileobj = fileobj
        self.generated_from = generated_from
        self.description = description
        self.tags = tags
        self.subscribers = [] if subscribers is None else subscribers
