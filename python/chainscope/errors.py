"""Safe errors: never include connection URLs or provider error messages."""


class ChainScopeError(Exception):
    """A failure that can safely be shown in the CLI."""


class DataMismatch(ChainScopeError):
    """Collected data disagrees with stored data or a second source.

    The window is quarantined: marked failed, kept out of the watermark and not
    retried automatically, with the differences saved as evidence.
    """

    kind = "data_mismatch"

    def __init__(self, message: str, diffs: list[dict] | None = None):
        super().__init__(message)
        self.diffs = diffs or []


class EventConflict(DataMismatch):
    kind = "EventConflict"


class ReadbackMismatch(DataMismatch):
    kind = "ReadbackMismatch"


class ReconcileMismatch(DataMismatch):
    kind = "ReconcileMismatch"


class RecheckChanged(DataMismatch):
    kind = "RecheckChanged"


class BlockTooLarge(DataMismatch):
    """One block holds more items than the hard limit; it cannot be split."""

    kind = "BlockTooLarge"
