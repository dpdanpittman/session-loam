"""Smoke tests for session_loam.Store — day 2 surface (write/get)."""

from __future__ import annotations

import socket
import sqlite3
import time
from pathlib import Path

import pytest

from session_loam import Entry, Store
from session_loam.store import (
    ENV_AGENT,
    ENV_BASE_DIR,
    ID_PREFIX,
    SCHEMA_VERSION,
    EntryNotFound,
    SchemaVersionMismatch,
)


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def store(store_path: Path) -> Store:
    s = Store.open(agent="testagent", path=store_path)
    try:
        yield s
    finally:
        s.close()


def test_write_round_trip(store: Store) -> None:
    entry = store.write(
        Entry(
            type="observation",
            content="Dan prefers terse responses.",
            tags=["dan", "preference"],
            confidence=0.9,
            source="session:2026-05-22T00:00Z",
        )
    )
    assert entry.id is not None
    assert entry.id.startswith(ID_PREFIX)
    assert entry.agent == "testagent"
    assert entry.created_at is not None
    assert entry.last_accessed == entry.created_at
    assert entry.access_count == 0

    fetched = store.get(entry.id)
    assert fetched.content == "Dan prefers terse responses."
    assert fetched.tags == ["dan", "preference"]
    assert fetched.confidence == pytest.approx(0.9)
    assert fetched.source == "session:2026-05-22T00:00Z"
    assert fetched.agent == "testagent"


def test_default_id_is_generated(store: Store) -> None:
    e1 = store.write(Entry(type="fact", content="one"))
    e2 = store.write(Entry(type="fact", content="two"))
    assert e1.id != e2.id
    assert e1.id.startswith(ID_PREFIX)
    assert e2.id.startswith(ID_PREFIX)


def test_explicit_id_is_respected(store: Store) -> None:
    entry = store.write(
        Entry(id="e.fixedid1", type="fact", content="custom id")
    )
    assert entry.id == "e.fixedid1"
    fetched = store.get("e.fixedid1")
    assert fetched.content == "custom id"


def test_get_missing_raises(store: Store) -> None:
    with pytest.raises(EntryNotFound):
        store.get("e.doesnotex")


def test_reinforce_on_get_bumps_counters(store: Store) -> None:
    written = store.write(Entry(type="fact", content="reinforce me"))
    assert written.access_count == 0
    initial_last_accessed = written.last_accessed

    time.sleep(1.1)
    hit1 = store.get(written.id)
    assert hit1.access_count == 1
    assert hit1.last_accessed > initial_last_accessed

    time.sleep(1.1)
    hit2 = store.get(written.id)
    assert hit2.access_count == 2
    assert hit2.last_accessed > hit1.last_accessed


def test_tags_stored_in_junction_table(store: Store, store_path: Path) -> None:
    e = store.write(
        Entry(
            type="observation",
            content="check the tag table",
            tags=["alpha", "Beta", "GAMMA", "alpha"],
        )
    )

    raw = sqlite3.connect(str(store_path))
    try:
        rows = raw.execute(
            "SELECT tag FROM entry_tag WHERE entry_id = ? ORDER BY tag",
            (e.id,),
        ).fetchall()
    finally:
        raw.close()

    assert [r[0] for r in rows] == ["alpha", "beta", "gamma"]


def test_supersede_sets_back_ref(store: Store) -> None:
    original = store.write(Entry(type="learning", content="v1"))
    replacement = store.write(
        Entry(type="learning", content="v2", supersedes=original.id)
    )
    refetched_original = store.get(original.id)
    assert refetched_original.superseded_by == replacement.id
    assert refetched_original.content == "v1"


def test_schema_version_persisted(store: Store, store_path: Path) -> None:
    raw = sqlite3.connect(str(store_path))
    try:
        row = raw.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        raw.close()
    assert row[0] == SCHEMA_VERSION


def test_schema_version_mismatch_refused(store_path: Path) -> None:
    s = Store.open(agent="testagent", path=store_path)
    s.close()

    raw = sqlite3.connect(str(store_path))
    try:
        raw.execute(
            "UPDATE _meta SET value = '99.0' WHERE key = 'schema_version'"
        )
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(SchemaVersionMismatch):
        Store.open(agent="testagent", path=store_path)


def test_fts_table_indexes_content(store: Store, store_path: Path) -> None:
    store.write(
        Entry(type="observation", content="filesystem-as-orchestrator pattern")
    )
    store.write(Entry(type="observation", content="unrelated lorem ipsum"))

    raw = sqlite3.connect(str(store_path))
    try:
        rows = raw.execute(
            "SELECT content FROM entry_fts WHERE entry_fts MATCH 'orchestrator'"
        ).fetchall()
    finally:
        raw.close()

    assert len(rows) == 1
    assert "orchestrator" in rows[0][0]


def test_default_agent_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AGENT, "EnvAgent")
    monkeypatch.setenv(ENV_BASE_DIR, str(tmp_path))
    s = Store.open()
    try:
        assert s.agent == "envagent"
        assert s.path == tmp_path / "envagent.db"
    finally:
        s.close()


def test_default_agent_falls_back_to_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_AGENT, raising=False)
    monkeypatch.setenv(ENV_BASE_DIR, str(tmp_path))
    s = Store.open()
    try:
        expected = socket.gethostname().split(".")[0].lower()
        assert s.agent == expected
    finally:
        s.close()


def test_metadata_and_attestations_round_trip(store: Store) -> None:
    e = store.write(
        Entry(
            type="event",
            content="payload",
            metadata={"k": "v", "n": 7},
            attestations=[{"signer": "tribunal", "verdict": "verified"}],
        )
    )
    fetched = store.get(e.id)
    assert fetched.metadata == {"k": "v", "n": 7}
    assert fetched.attestations == [{"signer": "tribunal", "verdict": "verified"}]


def test_context_manager_closes(store_path: Path) -> None:
    with Store.open(agent="ctx", path=store_path) as s:
        s.write(Entry(type="fact", content="ctx test"))
    with pytest.raises(sqlite3.ProgrammingError):
        s._conn.execute("SELECT 1")
