"""Storage interface boundary."""

from __future__ import annotations

from typing import Protocol


class StorageBackend(Protocol):
    """Common marker protocol for storage backends."""

    def close(self) -> None:
        """Release backend resources."""
