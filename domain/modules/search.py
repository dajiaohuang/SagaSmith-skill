"""Campaign-scoped lexical and profile-aware BGE module retrieval."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, or_, select

from ..db.database import Database
from ..db.models import (
    EmbeddingModel,
    ModuleChapter,
    ModuleChunk,
    ModuleSource,
    SceneIndex,
)
from ..rules.embedding import BgeM3Embedder, Embedder
from ..vector.client import VectorStore
from ..vector.search import chroma_dense_search

_DENSE_DISABLED = os.environ.get("DND_DENSE_DISABLED", "1") != "0"


class ModuleSearchError(RuntimeError):
    """Base error for module retrieval."""


@dataclass(frozen=True)
class ModuleSearchHit:
    rank: int
    score: float
    chunk_id: str
    module_name: str
    chapter_key: str
    chapter_title: str
    scene_id: str | None
    scene_title: str | None
    breadcrumb: str
    text: str
    start_line: int
    end_line: int
    page_start: int | None
    page_end: int | None
    chunk_type: str
    channels: tuple[str, ...]


class ModuleSearchService:
    """Search only the active imported module for one campaign."""

    def __init__(self, database: Database, *, embedder: Embedder | None = None) -> None:
        self.database = database
        self.embedder = embedder

    def search(
        self,
        query: str,
        *,
        campaign_id: str,
        top_k: int = 5,
        dense: bool = True,
    ) -> list[ModuleSearchHit]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1 or top_k > 50:
            raise ValueError("top_k must be between 1 and 50")
        with self.database.transaction() as session:
            allowed = list(
                session.scalars(
                    select(ModuleChunk.id)
                    .join(ModuleSource, ModuleSource.id == ModuleChunk.module_id)
                    .where(
                        ModuleSource.campaign_id == campaign_id,
                        ModuleSource.is_active.is_(True),
                    )
                )
            )
            if not allowed:
                return []
            lexical = self._lexical_ids(session, query, allowed, limit=max(top_k * 10, 50))
            dense_ids: list[str] = []
            dense_scores: dict[str, float] = {}
            if dense and not _DENSE_DISABLED:
                embedder = self._embedder_for_chunks(session, allowed)
                vector = embedder.encode([query])[0]
                dense_ids, dense_scores = self._dense_ids(
                    session,
                    vector,
                    campaign_id,
                    allowed,
                    embedder=embedder,
                    limit=max(top_k * 10, 50),
                )

            scores: dict[str, float] = {}
            channels: dict[str, set[str]] = {}
            for rank, chunk_id in enumerate(lexical, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
                channels.setdefault(chunk_id, set()).add("lexical")
            for rank, chunk_id in enumerate(dense_ids, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + (
                    dense_scores[chunk_id] + 1.0
                ) / 2.0 + 1.0 / (60 + rank)
                channels.setdefault(chunk_id, set()).add("dense")
            if not scores:
                return []
            ordered = sorted(scores, key=scores.get, reverse=True)[:top_k]
            hits = self._materialize(session, ordered, scores, channels)
            return [replace(hit, rank=index) for index, hit in enumerate(hits, start=1)]

    def expand(self, chunk_id: str, *, campaign_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as session:
            chunk = session.get(ModuleChunk, chunk_id)
            if chunk is None:
                raise ModuleSearchError(f"module chunk not found: {chunk_id}")
            chapter = session.get(ModuleChapter, chunk.chapter_id)
            module = session.get(ModuleSource, chunk.module_id)
            scene = session.get(SceneIndex, chunk.scene_id) if chunk.scene_id else None
            if chapter is None or module is None:
                raise ModuleSearchError(f"module chunk has missing parents: {chunk_id}")
            if campaign_id is not None and module.campaign_id != campaign_id:
                raise ModuleSearchError(
                    f"module chunk does not belong to campaign {campaign_id}: {chunk_id}"
                )
            if scene is not None:
                lines = chapter.content.splitlines(keepends=True)
                text = "".join(lines[scene.start_line - 1 : scene.end_line])
            else:
                text = chunk.chunk_text
            return {
                "chunk_id": chunk.id,
                "module_name": module.name,
                "chapter_key": chapter.chapter_key,
                "chapter_title": chapter.title,
                "scene_id": scene.id if scene else None,
                "scene_title": scene.title if scene else None,
                "text": text,
                "start_line": scene.start_line if scene else chunk.start_line,
                "end_line": scene.end_line if scene else chunk.end_line,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "chunk_type": chunk.chunk_type,
            }

    @staticmethod
    def _lexical_ids(session, query: str, allowed: list[str], *, limit: int) -> list[str]:
        terms = re.findall(r"[\w'-]+", query.casefold(), flags=re.UNICODE)[:12]
        if not terms:
            return []
        return list(
            session.scalars(
                select(ModuleChunk.id)
                .where(
                    ModuleChunk.id.in_(allowed),
                    or_(
                        *[
                            func.lower(ModuleChunk.search_text).contains(term)
                            for term in terms
                        ]
                    ),
                )
                .order_by(ModuleChunk.chunk_index)
                .limit(limit)
            )
        )

    def _dense_ids(
        self,
        session,
        query_vector: list[float],
        campaign_id: str,
        allowed: list[str],
        *,
        embedder: Embedder,
        limit: int,
    ) -> tuple[list[str], dict[str, float]]:
        """Return ChromaDB-backed dense IDs or empty when ChromaDB is unavailable."""
        if not VectorStore().enabled:
            return [], {}
        results = chroma_dense_search(
            "dnd_modules",
            query_vector,
            {"campaign_id": campaign_id},
            profile=embedder.profile,
            limit=limit,
        )
        # Post-filter: only return chunks that also exist in the
        # allowed set (active module).  ChromaDB metadata stores the
        # campaign but not whether the module is active — the SQL
        # side owns that truth.
        allowed_set = set(allowed)
        filtered = [
            (cid, score) for cid, score in results if cid in allowed_set
        ]
        ordered = [cid for cid, _ in filtered]
        scores = {cid: score for cid, score in filtered}
        return ordered, scores

    def _embedder_for_chunks(self, session, allowed: list[str]) -> Embedder:
        rows = list(
            session.scalars(
                select(EmbeddingModel)
                .join(ModuleChunk, ModuleChunk.embedding_model_id == EmbeddingModel.id)
                .where(ModuleChunk.id.in_(allowed))
                .distinct()
            )
        )
        if len(rows) > 1:
            raise ModuleSearchError(
                "active module contains multiple embedding models; rebuild it with one profile"
            )
        if self.embedder is not None:
            if rows and (
                rows[0].model_name != self.embedder.model_name
                or rows[0].dimensions != self.embedder.dimensions
            ):
                raise ModuleSearchError(
                    "query embedder does not match the model used to build this module index"
                )
            return self.embedder
        if rows:
            return BgeM3Embedder(model_name=rows[0].model_name)
        return BgeM3Embedder()

    @staticmethod
    def _materialize(
        session,
        ordered: list[str],
        scores: dict[str, float],
        channels: dict[str, set[str]],
    ) -> list[ModuleSearchHit]:
        rows = session.execute(
            select(ModuleChunk, ModuleChapter, ModuleSource, SceneIndex)
            .join(ModuleChapter, ModuleChapter.id == ModuleChunk.chapter_id)
            .join(ModuleSource, ModuleSource.id == ModuleChunk.module_id)
            .outerjoin(SceneIndex, SceneIndex.id == ModuleChunk.scene_id)
            .where(ModuleChunk.id.in_(ordered))
        )
        by_id = {row[0].id: row for row in rows}
        hits: list[ModuleSearchHit] = []
        for chunk_id in ordered:
            chunk, chapter, module, scene = by_id[chunk_id]
            hits.append(
                ModuleSearchHit(
                    rank=0,
                    score=scores[chunk_id],
                    chunk_id=chunk.id,
                    module_name=module.name,
                    chapter_key=chapter.chapter_key,
                    chapter_title=chapter.title,
                    scene_id=scene.id if scene else None,
                    scene_title=scene.title if scene else None,
                    breadcrumb=chunk.breadcrumb,
                    text=chunk.chunk_text,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    chunk_type=chunk.chunk_type,
                    channels=tuple(sorted(channels.get(chunk_id, set()))),
                )
            )
        return hits
