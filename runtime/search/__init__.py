"""Repository search, indexing, symbols, and bounded context compilation."""

from .context_compiler import RepositoryContextCompiler, RepositoryContextError
from .contracts import (
    RepositoryContextPack,
    RepositoryContextRange,
    RepositorySearchHit,
    RepositorySearchRequest,
)
from .engine import RepositorySearchEngine
from .index_manager import IndexManager, IndexManagerError
from .registry import RepositoryRecord, RepositoryRegistry, RegistryError

__all__ = [
    "IndexManager",
    "IndexManagerError",
    "RepositoryContextCompiler",
    "RepositoryContextError",
    "RepositoryContextPack",
    "RepositoryContextRange",
    "RepositoryRecord",
    "RepositoryRegistry",
    "RepositorySearchEngine",
    "RepositorySearchHit",
    "RepositorySearchRequest",
    "RegistryError",
]
