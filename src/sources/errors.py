class SourceError(Exception):
    """Base class for source-related errors."""


class RetryableError(SourceError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(SourceError):
    """Do not retry."""
