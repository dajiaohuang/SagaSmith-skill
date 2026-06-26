"""ChromaDB-backed dense vector storage and retrieval for D&D content."""

from .client import VectorStore
from .search import chroma_dense_search

__all__ = ["VectorStore", "chroma_dense_search"]
