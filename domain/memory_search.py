"""Natural-language search over branch-effective campaign memory revisions.

Two-tier retrieval stack:
  1. ChromaDB HNSW dense index  (fastest, requires chromadb + embedding model)
  2. Lexical keyword overlap     (no embedding model needed, always available)

Set ``DND_DENSE_DISABLED=1`` to skip dense retrieval entirely and always use
lexical search. Useful when dense retrieval is not wanted.

ChromaDB is disabled when neither ``CHROMA_DB_URL`` nor ``CHROMA_DB_PATH`` is
set.  In that case all searches fall back to lexical automatically — no
embedding model is ever loaded.
"""

from __future__ import annotations

import os
import re
from typing import Any, Protocol

from sqlalchemy import select

from .db.database import Database
from .db.memory import CampaignMemoryService
from .db.models.runtime import CampaignMemory, CampaignMemoryRevision
from .rules.embedding import (
    BGE_M3_PROFILE,
    BgeM3Embedder,
    EmbeddingProfile,
    collection_name,
    configured_profiles,
    detect_text_language,
    profile_for_language,
)
from .vector.client import VectorStore

COLLECTION_NAME = "dnd_campaign_memories"
_DENSE_DISABLED = os.environ.get("DND_DENSE_DISABLED", "1") != "0"


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


def dense_enabled() -> bool:
    """True when dense retrieval is globally allowed."""
    return not _DENSE_DISABLED


class CampaignMemorySearchService:
    """Index memory revisions and search only the revisions effective at one save."""

    def __init__(
        self,
        database: Database,
        *,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.database = database
        self.memory = CampaignMemoryService(database)
        self.embedder = embedder
        self.vector_store = vector_store or VectorStore()

    def search(
        self,
        campaign_id: str,
        query: str,
        *,
        save_id: str | None = None,
        statuses: list[str] | None = None,
        top_k: int = 8,
        dense: bool = True,
    ) -> dict[str, Any]:
        scope = self.memory.scope(campaign_id, save_id=save_id)
        memories = self.memory.get_effective(
            campaign_id,
            save_id=scope["save_id"],
            statuses=statuses,
        )
        if not memories:
            return {
                **scope,
                "query": query,
                "retrieval": "empty",
                "hits": [],
            }

        use_dense = (
            dense
            and dense_enabled()
            and self.vector_store.enabled
        )

        if use_dense:
            self.index_rows(memories)
            scored = self._dense_scores(query, memories, top_k=top_k)
            retrieval = "chroma"
        else:
            scored = self._lexical_scores(query, memories)
            retrieval = "lexical"

        hits = []
        for score, row in scored[: max(1, min(top_k, 20))]:
            hit = dict(row)
            hit["score"] = round(float(score), 6)
            hits.append(hit)
        return {
            **scope,
            "query": query,
            "retrieval": retrieval,
            "hits": hits,
        }

    def index_revision_ids(self, revision_ids: list[str]) -> int:
        ids = [value for value in revision_ids if value]
        if not ids or not self.vector_store.enabled or not dense_enabled():
            return 0
        with self.database.transaction() as session:
            rows = session.execute(
                select(CampaignMemoryRevision, CampaignMemory)
                .join(CampaignMemory, CampaignMemory.id == CampaignMemoryRevision.memory_id)
                .where(CampaignMemoryRevision.id.in_(ids))
            ).all()
            payload = [
                _joined_revision_row(revision, memory)
                for revision, memory in rows
            ]
        return self.index_rows(payload)

    def reindex(self, campaign_id: str | None = None) -> int:
        if not self.vector_store.enabled or not dense_enabled():
            return 0
        if campaign_id:
            for profile in self._profiles():
                self._collection(profile).delete(where={"campaign_id": campaign_id})
        else:
            for profile in self._profiles():
                self.vector_store.drop_collection(
                    collection_name(COLLECTION_NAME, profile)
                    if self.embedder is None
                    else COLLECTION_NAME
                )
        with self.database.transaction() as session:
            statement = (
                select(CampaignMemoryRevision, CampaignMemory)
                .join(CampaignMemory, CampaignMemory.id == CampaignMemoryRevision.memory_id)
                .order_by(CampaignMemoryRevision.created_at)
            )
            if campaign_id:
                statement = statement.where(
                    CampaignMemoryRevision.campaign_id == campaign_id
                )
            rows = session.execute(statement).all()
            payload = [
                _joined_revision_row(revision, memory)
                for revision, memory in rows
            ]
        return self.index_rows(payload)

    def index_rows(self, rows: list[dict[str, Any]]) -> int:
        if not rows or not self.vector_store.enabled or not dense_enabled():
            return 0
        grouped: dict[EmbeddingProfile, list[dict[str, Any]]] = {}
        if self.embedder is not None:
            grouped[self._profiles()[0]] = rows
        else:
            for row in rows:
                text = _embedding_text(row)
                profile = profile_for_language(detect_text_language(text))
                grouped.setdefault(profile, []).append(row)
        for profile, profile_rows in grouped.items():
            texts = [_embedding_text(row) for row in profile_rows]
            embedder = self.embedder or BgeM3Embedder(profile=profile)
            vectors = embedder.encode(texts)
            self._collection(profile).upsert(
                ids=[str(row["revision_id"]) for row in profile_rows],
                embeddings=vectors,
                documents=texts,
                metadatas=[_metadata(row) for row in profile_rows],
            )
        return len(rows)

    def status(self) -> dict[str, Any]:
        return {
            "dense_disabled": not dense_enabled(),
            "chroma_enabled": self.vector_store.enabled,
            "collections": (
                [
                    self.vector_store.collection_stats(
                        collection_name(COLLECTION_NAME, profile)
                        if self.embedder is None
                        else COLLECTION_NAME
                    )
                    for profile in self._profiles()
                ]
                if self.vector_store.enabled
                else []
            ),
        }

    def _dense_scores(
        self,
        query: str,
        rows: list[dict[str, Any]],
        *,
        top_k: int,
    ) -> list[tuple[float, dict[str, Any]]]:
        profile = (
            self._profiles()[0]
            if self.embedder is not None
            else profile_for_language(detect_text_language(query))
        )
        embedder = self.embedder or BgeM3Embedder(profile=profile)
        query_vector = embedder.encode([query])[0]
        collection = self._collection(profile)
        try:
            result = collection.query(
                query_embeddings=[query_vector],
                n_results=min(top_k * 4, len(rows)),
                include=["distances"],
            )
            ids = (result.get("ids") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            if not ids:
                return self._lexical_scores(query, rows)
            row_by_id = {str(row["revision_id"]): row for row in rows}
            scored = [
                (1.0 - float(dist), row_by_id[rid])
                for rid, dist in zip(ids, distances, strict=True)
                if rid in row_by_id
            ]
            scored.sort(key=lambda item: item[0], reverse=True)
            return scored
        except Exception:
            return self._lexical_scores(query, rows)

    @staticmethod
    def _lexical_scores(
        query: str,
        rows: list[dict[str, Any]],
    ) -> list[tuple[float, dict[str, Any]]]:
        query_terms = _search_terms(query)
        scored = []
        for row in rows:
            haystack = " ".join([
                str(row.get("text") or ""),
                str(row.get("kind") or ""),
                str(row.get("entity_id") or ""),
                str(row.get("fact_type") or ""),
            ])
            terms = _search_terms(haystack)
            overlap = len(query_terms & terms)
            score = overlap / max(1, len(query_terms))
            if query.casefold() in haystack.casefold():
                score += 1.0
            scored.append((score, row))
        scored.sort(
            key=lambda item: (
                item[0],
                _priority_rank(str(item[1].get("priority") or "")),
                -int(item[1].get("distance") or 0),
            ),
            reverse=True,
        )
        return scored

    def _profiles(self) -> tuple[EmbeddingProfile, ...]:
        if self.embedder is not None:
            return (getattr(self.embedder, "profile", BGE_M3_PROFILE),)
        return configured_profiles()

    def _collection(self, profile: EmbeddingProfile):
        if self.embedder is None:
            return self.vector_store.collection_for(COLLECTION_NAME, profile)
        return self.vector_store.collection(COLLECTION_NAME)


def _joined_revision_row(
    revision: CampaignMemoryRevision,
    memory: CampaignMemory,
) -> dict[str, Any]:
    return {
        "id": memory.id,
        "revision_id": revision.id,
        "campaign_id": memory.campaign_id,
        "kind": memory.kind,
        "entity_type": memory.entity_type,
        "entity_id": memory.entity_id,
        "fact_type": memory.fact_type,
        "text": revision.text,
        "priority": revision.priority,
        "status": revision.status,
        "operation": revision.operation,
        "source_save_id": revision.save_id,
        "distance": 0,
        "created_at": revision.created_at.isoformat(),
    }


def _embedding_text(row: dict[str, Any]) -> str:
    return " | ".join([
        str(row.get("kind") or ""),
        str(row.get("entity_type") or ""),
        str(row.get("entity_id") or ""),
        str(row.get("fact_type") or ""),
        str(row.get("text") or ""),
    ])


def _metadata(row: dict[str, Any]) -> dict[str, str]:
    return {
        "campaign_id": str(row["campaign_id"]),
        "memory_id": str(row["id"]),
        "save_id": str(row["source_save_id"]),
        "kind": str(row["kind"]),
        "entity_type": str(row["entity_type"]),
        "entity_id": str(row["entity_id"]),
        "fact_type": str(row["fact_type"]),
        "priority": str(row["priority"]),
        "status": str(row["status"]),
    }


def _search_terms(text: str) -> set[str]:
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    words = set(re.findall(r"[a-z0-9_]+", normalized))
    compact = re.sub(r"\s+", "", normalized)
    grams = {
        compact[index:index + 2]
        for index in range(max(0, len(compact) - 1))
    }
    return words | grams


def _priority_rank(priority: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(priority, 0)
