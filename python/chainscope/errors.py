"""Safe errors: never include connection URLs or provider error messages."""


class ChainScopeError(Exception):
    """A failure that can safely be shown in the CLI."""
