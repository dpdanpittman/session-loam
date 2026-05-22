"""Tests for Store.search + Store.recent — day 3 retrieval surface."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from session_loam import Entry, RankWeights, Store
from session_loam.search import escape_query


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(agent="searchtest", path=tmp_path / "search.db")
    try:
        yield s
    finally:
        s.close()


def _seed(store: Store) -> dict[str, str]:
    """Write a small fixture set and return content -> id map."""
    ids: dict[str, str] = {}
    fixtures = [
        Entry(
            type="observation",
            content="filesystem-as-orchestrator pattern for agentic workflows",
            tags=["pattern", "swarm"],
            confidence=0.9,
        ),
        Entry(
            type="observation",
            content="atomic rename is the orchestrator primitive",
            tags=["primitive", "swarm"],
            confidence=0.85,
        ),
        Entry(
            type="learning",
            content="compaction can lose mid-flight work without a substrate",
            tags=["compaction", "lesson"],
            confidence=0.7,
        ),
        Entry(
            type="fact",
            content="ChromaDB is a vector database for semantic similarity",
            tags=["external", "tooling"],
            confidence=0.95,
        ),
        Entry(
            type="event",
            content="zaphod cluster outage on 2026-05-01",
            tags=["incident", "infra"],
            confidence=1.0,
        ),
    ]
    for f in fixtures:
        written = store.write(f)
        ids[f.content] = written.id
    return ids


def test_search_fts_match(store: Store) -> None:
    _seed(store)
    results = store.search(query="orchestrator")
    assert len(results) == 2
    contents = {r.entry.content for r in results}
    assert "filesystem-as-orchestrator pattern for agentic workflows" in contents
    assert "atomic rename is the orchestrator primitive" in contents


def test_search_returns_snippet_with_terms(store: Store) -> None:
    _seed(store)
    results = store.search(query="orchestrator")
    assert results
    assert any("orchestrator" in r.snippet.lower() for r in results)


def test_search_tag_filter_AND(store: Store) -> None:
    _seed(store)
    results = store.search(query="orchestrator", tags=["swarm", "primitive"])
    assert len(results) == 1
    assert "atomic rename" in results[0].entry.content


def test_search_type_filter(store: Store) -> None:
    _seed(store)
    results = store.search(query="orchestrator OR substrate OR vector", type="fact")
    assert all(r.entry.type == "fact" for r in results)


def test_search_no_match_returns_empty(store: Store) -> None:
    _seed(store)
    results = store.search(query="zztopxylophone")
    assert results == []


def test_search_reinforces_hits(store: Store) -> None:
    ids = _seed(store)
    target_id = ids["filesystem-as-orchestrator pattern for agentic workflows"]
    before = store.get(target_id)
    # get() already bumped access_count to 1; record that
    initial_count = before.access_count
    initial_accessed = before.last_accessed

    time.sleep(1.1)
    results = store.search(query="orchestrator")
    matched = next(r for r in results if r.entry.id == target_id)
    assert matched.entry.access_count == initial_count + 1
    assert matched.entry.last_accessed > initial_accessed

    # And the bump is persisted: a fresh get sees the increased count
    fresh = store.get(target_id)
    # The fresh get adds another reinforcement on top
    assert fresh.access_count == initial_count + 2


def test_search_reinforce_disabled(store: Store) -> None:
    ids = _seed(store)
    target_id = ids["filesystem-as-orchestrator pattern for agentic workflows"]
    before = store.get(target_id)
    initial_count = before.access_count

    results = store.search(query="orchestrator", reinforce=False)
    matched = next(r for r in results if r.entry.id == target_id)
    # In the returned object, no bump applied
    assert matched.entry.access_count == initial_count

    # And not persisted to disk either
    fresh_raw_count = store._conn.execute(
        "SELECT access_count FROM entry WHERE id = ?", (target_id,)
    ).fetchone()[0]
    # get() above bumped to initial_count; search with reinforce=False didn't add to it
    assert fresh_raw_count == initial_count


def test_search_hides_superseded_by_default(store: Store) -> None:
    original = store.write(
        Entry(type="learning", content="v1 takeaway about orchestrator", tags=["lesson"])
    )
    store.write(
        Entry(
            type="learning",
            content="v2 takeaway about orchestrator",
            tags=["lesson"],
            supersedes=original.id,
        )
    )
    results = store.search(query="orchestrator")
    ids = {r.entry.id for r in results}
    assert original.id not in ids

    with_super = store.search(query="orchestrator", include_superseded=True)
    ids_with_super = {r.entry.id for r in with_super}
    assert original.id in ids_with_super


def test_search_time_range(store: Store) -> None:
    from datetime import datetime, timezone

    a = store.write(Entry(type="event", content="orchestrator event one"))
    time.sleep(1.1)
    cut = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    time.sleep(1.1)
    b = store.write(Entry(type="event", content="orchestrator event two"))

    after_cut = store.search(query="orchestrator", since=cut)
    after_ids = {r.entry.id for r in after_cut}
    assert b.id in after_ids
    assert a.id not in after_ids

    before_cut = store.search(query="orchestrator", until=cut)
    before_ids = {r.entry.id for r in before_cut}
    assert a.id in before_ids
    assert b.id not in before_ids


def test_search_ranking_recency_breaks_tie(store: Store) -> None:
    # Two entries with identical content -> identical bm25; recency should
    # push the newer one first.
    old = store.write(Entry(type="fact", content="same content here"))
    time.sleep(1.1)
    new = store.write(Entry(type="fact", content="same content here"))

    results = store.search(query="content", weights=RankWeights(recency_strength=1.0))
    assert results[0].entry.id == new.id
    assert results[1].entry.id == old.id


def test_search_ranking_confidence_breaks_tie(store: Store) -> None:
    low = store.write(Entry(type="fact", content="same content here", confidence=0.1))
    high = store.write(Entry(type="fact", content="same content here", confidence=0.9))

    results = store.search(
        query="content",
        weights=RankWeights(
            recency_strength=0.0,
            confidence_strength=1.0,
            access_count_strength=0.0,
        ),
    )
    assert results[0].entry.id == high.id
    assert results[1].entry.id == low.id


def test_recent_returns_newest_first(store: Store) -> None:
    a = store.write(Entry(type="observation", content="first"))
    time.sleep(1.1)
    b = store.write(Entry(type="observation", content="second"))
    time.sleep(1.1)
    c = store.write(Entry(type="observation", content="third"))

    results = store.recent(type="observation")
    assert [r.id for r in results] == [c.id, b.id, a.id]


def test_recent_filters_by_tag_AND(store: Store) -> None:
    _seed(store)
    results = store.recent(tags=["swarm"])
    contents = {e.content for e in results}
    assert "filesystem-as-orchestrator pattern for agentic workflows" in contents
    assert "atomic rename is the orchestrator primitive" in contents
    assert "ChromaDB is a vector database for semantic similarity" not in contents


def test_recent_hides_superseded_by_default(store: Store) -> None:
    original = store.write(Entry(type="learning", content="v1"))
    store.write(Entry(type="learning", content="v2", supersedes=original.id))

    results = store.recent(type="learning")
    assert all(e.id != original.id for e in results)

    with_super = store.recent(type="learning", include_superseded=True)
    assert any(e.id == original.id for e in with_super)


def test_recent_limit_respected(store: Store) -> None:
    for i in range(5):
        store.write(Entry(type="event", content=f"event {i}"))
    results = store.recent(type="event", limit=3)
    assert len(results) == 3


def test_search_requires_query(store: Store) -> None:
    with pytest.raises(ValueError):
        store.search(query="")
    with pytest.raises(ValueError):
        store.search(query="   ")


def test_escape_query_wraps_phrase(store: Store) -> None:
    store.write(Entry(type="fact", content="the quick brown fox jumps"))
    store.write(Entry(type="fact", content="quick fox brown the jumps"))

    phrase = escape_query("quick brown fox")
    results = store.search(query=phrase)
    assert len(results) == 1
    assert "quick brown fox" in results[0].entry.content


def test_escape_query_escapes_embedded_quotes() -> None:
    assert escape_query('he said "hi"') == '"he said ""hi"""'
