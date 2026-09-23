"""Errors the CLI reports by exit code (DESIGN.md "Error policy": loud where money or correctness is at stake)."""

from __future__ import annotations


class ToolError(Exception):
    """A failure the user must act on; ``code`` becomes the process exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class UsageError(ToolError):
    """Bad arguments — exit 2, like argparse."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=2)


class ConfigError(ToolError):
    """A config or price override that cannot be parsed or changes a model's shape — exit 2, names the path."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=2)


class PricingError(ToolError):
    """A row that cannot be priced and carries no reported cost — exit 5, names the model."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=5)
