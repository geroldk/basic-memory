"""BM-002: retain lexical evidence and contain relaxed matches per request."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from basic_memory.models.search import CREATE_SEARCH_INDEX
from basic_memory.repository.script_ngrams import build_script_ngrams
from basic_memory.repository.search_index_row import SearchIndexRow
from basic_memory.repository.search_query import PreparedSearchQuery
from basic_memory.repository.search_reader import (
    FUSION_FORMULA_VERSION,
    RELAXED_FUSION_FORMULA_VERSION,
    fuse,
)
from basic_memory.repository.search_scope import ProjectScope
from basic_memory.repository.search_trace import SearchTraceCollector
from basic_memory.repository.sqlite_search_query import SQLiteFts
from basic_memory.schemas.search import SearchItemType, SearchRetrievalMode
from tests.repository.test_hybrid_fusion import (
    OfflineSemanticSearch,
    fake_vector_retrieval,
    vector_leg,
)

SCOPE = ProjectScope.single(1)


@pytest_asyncio.fixture
async def ranking_fts() -> AsyncIterator[SQLiteFts]:
    """Use real FTS5 cursors and native reader code; no application-state writes."""
    engine = create_async_engine("sqlite+aiosqlite://")
    now = datetime.now(timezone.utc).isoformat()
    documents = [
        (1, 1, "alpha heading", "beta beta beta beta extra", "active"),
        (2, 1, "neutral heading", "alpha extra extra extra extra", "inactive"),
        *[
            (i, 1, "other heading", "alpha filler filler filler filler", "active")
            for i in range(3, 11)
        ],
        (11, 1, "joined terms", "alpha beta extra extra extra", "active"),
        (20, 2, "alpha foreign", "beta foreign", "active"),
    ]
    async with engine.begin() as connection:
        await connection.execute(text(str(CREATE_SEARCH_INDEX)))
        await connection.execute(
            text(
                "CREATE TABLE entity (id INTEGER PRIMARY KEY, project_id INTEGER, "
                "entity_metadata TEXT, content_type TEXT)"
            )
        )
        for row_id, project, title, body, status in documents:
            await connection.execute(
                text(
                    "INSERT INTO entity VALUES (:id, :project, json_object('status', :status), 'text/markdown')"
                ),
                {"id": row_id, "project": project, "status": status},
            )
            await connection.execute(
                text(
                    "INSERT INTO search_index (id, title, content_stems, content_snippet, "
                    "permalink, file_path, type, project_id, entity_id, metadata, created_at, updated_at) "
                    "VALUES (:id, :title, :body, :body, :path, :path, 'entity', :project, :id, '{}', :now, :now)"
                ),
                {
                    "id": row_id,
                    "project": project,
                    "title": title,
                    "body": body,
                    "path": f"notes/{row_id}.md",
                    "now": now,
                },
            )
    yield SQLiteFts(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


def query(search_text: str, **kwargs) -> PreparedSearchQuery:
    return PreparedSearchQuery(
        search_text=search_text, search_item_types=[SearchItemType.ENTITY], **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_filters", [None, {"status": "active"}])
async def test_common_title_word_does_not_hide_rare_body_evidence(ranking_fts, metadata_filters):
    """A combined query must credit both matching phrases on the same note."""
    scores = []
    for words in ["alpha", "beta", "alpha OR beta"]:
        rows = await ranking_fts.search(
            SCOPE, query(words, metadata_filters=metadata_filters), limit=50, offset=0
        )
        score = next(row.score for row in rows if row.id == 1)
        assert score is not None
        scores.append(score)
    # The old SQL-OR query gave this note only one cursor's evidence, so its
    # combined score equalled a single-phrase score instead of crediting both.
    assert scores[2] < min(scores[:2]) - 0.0001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("alpha beta", {11}),
        ("alpha AND beta", {11}),
        ('"alpha beta"', {11}),
        ("alpha OR beta", set(range(1, 12))),
        ("alpha NOT beta", set(range(2, 11))),
        ("alp", set(range(1, 12))),
    ],
)
async def test_word_matching_preserves_column_boundaries_and_query_intent(
    ranking_fts, words, expected
):
    rows = await ranking_fts.search(SCOPE, query(words), limit=50, offset=0)
    assert {row.id for row in rows} == expected
    assert await ranking_fts.count(SCOPE, query(words)) == len(expected)


@pytest.mark.asyncio
async def test_relaxed_retry_and_count_keep_metadata_and_project_filters(ranking_fts):
    request = query("why alpha beta absent", metadata_filters={"status": "active"})
    assert await ranking_fts.search(SCOPE, request, limit=50, offset=0) == []
    trace = SearchTraceCollector()
    rows = await ranking_fts.search(
        SCOPE, request, limit=50, offset=0, allow_relaxed=True, trace=trace
    )
    assert {row.id for row in rows} == set(range(1, 12)) - {2}
    assert all(row.project_id == 1 for row in rows)
    assert await ranking_fts.count(SCOPE, request, allow_relaxed=True) == len(rows)
    assert trace.fts is not None and trace.fts.relaxed_fallback_used
    foreign = await ranking_fts.search(
        ProjectScope.single(2), request, limit=50, offset=0, allow_relaxed=True
    )
    assert [row.id for row in foreign] == [20]


@pytest.mark.asyncio
async def test_exact_identity_filters_and_pagination_survive_shared_match_context(ranking_fts):
    request = query("alpha OR beta")
    full = await ranking_fts.search(SCOPE, request, limit=50, offset=0)
    first = await ranking_fts.search(SCOPE, request, limit=3, offset=0)
    second = await ranking_fts.search(SCOPE, request, limit=3, offset=3)
    assert [(r.id, r.score) for r in first + second] == [(r.id, r.score) for r in full[:6]]
    for identity in [dict(title="alpha heading"), dict(permalink_match="notes/1.md")]:
        rows = await ranking_fts.search(
            SCOPE, query("alpha OR beta", **identity), limit=50, offset=0
        )
        assert [row.id for row in rows] == [1]
        assert await ranking_fts.count(SCOPE, query("alpha OR beta", **identity)) == 1


@pytest.mark.asyncio
async def test_mixed_script_retry_preserves_required_script_grams(ranking_fts):
    async with ranking_fts._session_maker.begin() as session:
        await session.execute(
            text("UPDATE search_index SET script_ngrams=:grams WHERE id=1 AND project_id=1"),
            {"grams": build_script_ngrams("季度報告")},
        )
    request = query("alpha missingword anotherword 季度報告")
    trace = SearchTraceCollector()
    assert await ranking_fts.search(SCOPE, request, limit=50, offset=0) == []
    rows = await ranking_fts.search(
        SCOPE, request, limit=50, offset=0, allow_relaxed=True, trace=trace
    )
    assert [row.id for row in rows] == [1]
    assert trace.fts is not None and trace.fts.relaxed_fallback_used
    assert await ranking_fts.count(SCOPE, request, allow_relaxed=True) == 1


def vector_row(row_id: int, score: float, *, row_type: str = "entity") -> SearchIndexRow:
    now = datetime.now(timezone.utc)
    return SearchIndexRow(
        project_id=1,
        id=row_id,
        type=row_type,
        file_path=f"notes/{row_id}.md",
        created_at=now,
        updated_at=now,
        score=score,
        content_snippet="answer",
    )


def test_relaxed_fusion_rewards_agreement_without_promoting_unanchored_words():
    lexical = [vector_row(1, -10), vector_row(2, -5), vector_row(1, -2, row_type="relation")]
    vectors = [vector_row(2, 0.7), vector_row(3, 0.8)]
    strict = fuse(lexical, vectors)
    relaxed = fuse(lexical, vectors, relaxed_fts=True)
    assert strict.ranked[0][0] == ("entity", 1)
    assert [key for key, _ in relaxed.ranked[:2]] == [("entity", 2), ("entity", 3)]
    assert dict(relaxed.ranked)[("entity", 1)] < 0.475
    assert dict(relaxed.ranked)[("relation", 1)] < 0.475
    assert dict(relaxed.ranked)[("entity", 2)] > 0.7
    assert dict(relaxed.ranked)[("entity", 3)] == 0.8
    zero = fuse([vector_row(1, 0)], [], relaxed_fts=True)
    assert zero.ranked == [(("entity", 1), 0)]


@pytest.mark.asyncio
async def test_retry_status_affects_ordinary_requests_and_stays_call_scoped(ranking_fts):
    semantic = OfflineSemanticSearch(
        ranking_fts._session_maker, SCOPE, ranking_fts, fake_vector_retrieval()
    )
    vectors = [vector_row(100, 0.9), vector_row(1, 0.6)]
    relaxed_query = replace(
        query("why alpha beta absent"), retrieval_mode=SearchRetrievalMode.HYBRID
    )
    strict_query = replace(query("alpha OR beta"), retrieval_mode=SearchRetrievalMode.HYBRID)

    async def run(request, traced):
        trace = SearchTraceCollector() if traced else None
        rows = await semantic.hybrid(request, limit=10, offset=0, trace=trace)
        return rows, trace

    with patch.object(semantic, "_vector_window", vector_leg(vectors)):
        relaxed, strict, traced_relaxed, traced_strict = await asyncio.gather(
            run(relaxed_query, False),
            run(strict_query, False),
            run(relaxed_query, True),
            run(strict_query, True),
        )
    assert relaxed[0][0].id == 100
    assert strict[0][0].id != 100
    assert [(r.id, r.score) for r in relaxed[0]] == [(r.id, r.score) for r in traced_relaxed[0]]
    assert [(r.id, r.score) for r in strict[0]] == [(r.id, r.score) for r in traced_strict[0]]
    assert traced_relaxed[1].fusion.formula_version == RELAXED_FUSION_FORMULA_VERSION
    assert traced_strict[1].fusion.formula_version == FUSION_FORMULA_VERSION
