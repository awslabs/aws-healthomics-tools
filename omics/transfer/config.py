from typing import Optional

from s3transfer.constants import KB


class TransferConfig:
    """Configuration options for the Omics Transfer Manager."""

    def __init__(
        self,
        use_threads: bool = True,
        directory: str = ".",
        max_request_concurrency: int = 10,
        max_submission_concurrency: int = 5,
        max_request_queue_size: int = 1000,
        max_submission_queue_size: int = 1000,
        max_io_queue_size: int = 1000,
        io_chunksize: int = 256 * KB,
        num_download_attempts: int = 5,
        max_bandwidth: Optional[int] = None,
    ):
        """Create a Transfer Manager configuration.

        Args:
            use_threads: If True, threads will be used.
                If False, no threads will be used in performing transfers;
                all logic will be run in the main thread.

            directory: Directory to write data to.

            max_request_concurrency: The maximum number of Omics API
                transfer-related requests that can happen at a time.

            max_submission_concurrency: The maximum number of threads
                processing a call to a TransferManager method. Processing a
                call usually entails determining which Omics API requests need
                to be enqueued, but does **not** entail making any of the
                Omics API data transferring requests needed to perform the transfer.
                The threads controlled by ``max_request_concurrency`` is
                responsible for that.

            max_request_queue_size: The maximum amount of Omics API requests
                that can be queued at a time.

            max_submission_queue_size: The maximum amount of
                TransferManager method calls that can be queued at a time.

            max_io_queue_size: The maximum amount of read parts that
                can be queued to be written to disk per download. The default
                size for each element in this queue is 8 KB.

            io_chunksize: The max size of each chunk in the io queue.

            num_download_attempts: The number of download attempts that
                will be tried upon errors with downloading an object in Omics. Note
                that these retries account for errors that occur when streaming
                down the data from Omics (i.e. socket errors and read timeouts that
                occur after receiving an OK response from Omics).
                Other retryable exceptions such as throttling errors and 5xx errors
                are already retried by botocore (this default is 5). The
                ``num_download_attempts`` does not take into account the
                number of exceptions retried by botocore.

            max_bandwidth: The maximum bandwidth that will be consumed
                in uploading and downloading file content. The value is in terms of
                bytes per second.
        """
        self.use_threads = use_threads
        self.directory = directory
        self.max_request_concurrency = max_request_concurrency
        self.max_submission_concurrency = max_submission_concurrency
        self.max_request_queue_size = max_request_queue_size
        self.max_submission_queue_size = max_submission_queue_size
        self.max_io_queue_size = max_io_queue_size
        self.io_chunksize = io_chunksize
        self.num_download_attempts = num_download_attempts
        self.max_bandwidth = max_bandwidth
        self._validate_attrs_are_nonzero()

    def _validate_attrs_are_nonzero(self) -> None:
        for attr, attr_val in self.__dict__.items():
            if (
                attr_val is not None
                and isinstance(attr_val, int)
                and not isinstance(attr_val, bool)
                and attr_val <= 0
            ):
                raise ValueError(
                    "Provided parameter %s of value %s must be greater than "
                    "0." % (attr, attr_val)
                )
