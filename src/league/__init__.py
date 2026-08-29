"""League's repository-local command and storage package."""

from .storage import ConnectionPolicy, Storage, StorageRefusal

__version__ = "0.2.3"
MAX_ACCEPTANCE_SENTINEL_PATHS = 16

__all__ = [
    "ConnectionPolicy",
    "MAX_ACCEPTANCE_SENTINEL_PATHS",
    "Storage",
    "StorageRefusal",
    "__version__",
]
