"""ChromaDB client lifecycle and collection access.

The VectorStore is a lightweight singleton that lazily connects to ChromaDB.
When neither CHROMA_DB_URL nor CHROMA_DB_PATH is set, ChromaDB integration is
fully disabled and ``VectorStore().enabled`` returns ``False``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from chromadb import Collection, HttpClient, PersistentClient
from chromadb.config import Settings

if TYPE_CHECKING:
    from ..rules.embedding import EmbeddingProfile

logger = logging.getLogger(__name__)

_COLLECTION_NAMES = ("dnd_rules", "dnd_modules", "dnd_campaign_memories")
_COLLECTION_METADATA = {
    "hnsw:space": "cosine",
}


def _default_chroma_path() -> Path:
    """Return the default ChromaDB persistent store path.

    Defaults to ``<skill_dir>/data/chroma_db`` so the skill is fully
    self-contained.  Override with ``CHROMA_DB_PATH``.
    """
    # Walk up from this file to find the skill root (contains domain/ and tools/)
    current = Path(__file__).resolve().parent
    while current.parent != current:
        if (current / "domain").is_dir() and (current / "tools").is_dir():
            break
        current = current.parent
    chroma_dir = current / "data" / "chroma_db"
    chroma_dir.mkdir(parents=True, exist_ok=True)
    return chroma_dir


class VectorStore:
    """Manage a ChromaDB client and provide collection access.

    Usage::

        store = VectorStore()
        if store.enabled:
            coll = store.collection("dnd_rules")
            coll.query(...)
    """

    def __init__(self) -> None:
        self._client: HttpClient | PersistentClient | None = None
        self._collections: dict[str, Collection] = {}
        self._enabled: bool | None = None

    @property
    def enabled(self) -> bool:
        """True when ChromaDB is reachable.

        ChromaDB is disabled by default.  Set ``CHROMA_DB_URL`` or
        ``CHROMA_DB_PATH`` to enable it, or set ``CHROMA_DB_DISABLED=0`` to
        force-enable with the default local path.
        """
        if self._enabled is None:
            if os.environ.get("CHROMA_DB_DISABLED") == "0":
                self._enabled = True
            else:
                url = os.environ.get("CHROMA_DB_URL")
                path = os.environ.get("CHROMA_DB_PATH")
                self._enabled = bool(url or path)
        return self._enabled

    @staticmethod
    def configured_url() -> str | None:
        """Return the configured HTTP URL, if any."""
        return os.environ.get("CHROMA_DB_URL") or None

    @staticmethod
    def configured_path() -> Path | None:
        """Return the configured persistent path, if any."""
        raw = os.environ.get("CHROMA_DB_PATH")
        return Path(raw).expanduser().resolve() if raw else None

    def _connect(self) -> HttpClient | PersistentClient:
        url = self.configured_url()
        if url:
            logger.info("Connecting to ChromaDB HTTP server at %s", url)
            return HttpClient(host=url, settings=Settings(anonymized_telemetry=False))
        path = self.configured_path() or _default_chroma_path()
        path.mkdir(parents=True, exist_ok=True)
        logger.info("Opening ChromaDB persistent store at %s", path)
        return PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))

    def _ensure_client(self):
        if self._client is None:
            self._client = self._connect()
        return self._client

    def collection(self, name: str) -> Collection:
        """Return a ChromaDB collection, creating it on first access."""
        if name not in _COLLECTION_NAMES and not any(
            name.startswith(f"{base}__") for base in _COLLECTION_NAMES
        ):
            raise ValueError(
                f"unknown ChromaDB collection {name!r}; expected one of {_COLLECTION_NAMES}"
            )
        if name not in self._collections:
            client = self._ensure_client()
            self._collections[name] = client.get_or_create_collection(
                name=name,
                metadata=_COLLECTION_METADATA,
            )
        return self._collections[name]

    def collection_for(self, base_name: str, profile: EmbeddingProfile) -> Collection:
        """Return a model-isolated collection and validate its manifest."""
        from ..rules.embedding import collection_name

        name = collection_name(base_name, profile)
        if name not in self._collections:
            expected = {
                **_COLLECTION_METADATA,
                "embedding_model": profile.model_name,
                "embedding_dimensions": profile.dimensions,
                "embedding_language": profile.language,
                "embedding_index_version": 1,
            }
            collection = self._ensure_client().get_or_create_collection(
                name=name,
                metadata=expected,
            )
            metadata = collection.metadata or {}
            for key in (
                "embedding_model",
                "embedding_dimensions",
                "embedding_language",
                "embedding_index_version",
            ):
                if metadata.get(key) != expected[key]:
                    raise RuntimeError(
                        f"ChromaDB collection {name!r} has an incompatible embedding "
                        f"manifest ({key}={metadata.get(key)!r}, expected {expected[key]!r}); "
                        "rebuild the collection before querying it"
                    )
            self._collections[name] = collection
        return self._collections[name]

    def collection_stats(self, name: str) -> dict:
        """Return approximate row count for a collection."""
        try:
            coll = self.collection(name)
            return {"name": name, "count": coll.count()}
        except Exception as exc:
            return {"name": name, "count": None, "error": str(exc)}

    def drop_collection(self, name: str) -> None:
        """Delete a collection entirely (used by reindex)."""
        if name in self._collections:
            self._collections.pop(name)
        try:
            self._ensure_client().delete_collection(name)
        except Exception:
            logger.debug("Collection %r does not exist or could not be deleted", name)

    def dispose(self) -> None:
        """Release client resources."""
        self._collections.clear()
        self._client = None
