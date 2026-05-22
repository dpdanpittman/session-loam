"""Tests for Store.prune, Store.iter_export, Store.import_entry — day 5 surface."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from session_loam import Entry, Store
from session_loam.store import EntryAlreadyExists


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(agent="day5test", path=tmp_path / "d5.db")
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

def test_prune_requires_filter(store: Store) -> None:
    with pytest.raises(ValueError):
        store.prune()


def test_prune_dry_run_does_not_delete(store: Store) -> None:
    e = store.write(Entry(type="event", content="kill me", tags=["delete"]))
    ids = store.prune(tags=["delete"])  # dry_run defaults True
    assert ids == [e.id]

    # Still there
    fetched = store.get(e.id)
    assert fetched.content == "kill me"


def test_prune_apply_deletes_matching(store: Store) -> None:
    keep = store.write(Entry(type="fact", content="keep me", tags=["keep"]))
    kill = store.write(Entry(type="fact", content="kill me", tags=["delete"]))

    ids = store.prune(tags=["delete"], dry_run=False)
    assert ids == [kill.id]

    fetched = store.get(keep.id)
    assert fetched.id == keep.id

    from session_loam.store import EntryNotFound

    with pytest.raises(EntryNotFound):
        store.get(kill.id)


def test_prune_filter_by_type(store: Store) -> None:
    a = store.write(Entry(type="event", content="a"))
    b = store.write(Entry(type="event", content="b"))
    c = store.write(Entry(type="fact", content="c"))

    ids = store.prune(type="event", dry_run=False)
    assert set(ids) == {a.id, b.id}
    # c survives
    assert store.get(c.id).id == c.id


def test_prune_filter_older_than(store: Store) -> None:
    old = store.write(Entry(type="event", content="old"))
    time.sleep(1.1)
    from datetime import datetime, timezone

    cut = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    time.sleep(1.1)
    new = store.write(Entry(type="event", content="new"))

    ids = store.prune(older_than=cut, dry_run=False)
    assert ids == [old.id]
    assert store.get(new.id).id == new.id


def test_prune_only_superseded(store: Store) -> None:
    original = store.write(Entry(type="learning", content="v1"))
    store.write(Entry(type="learning", content="v2", supersedes=original.id))
    untouched = store.write(Entry(type="learning", content="standalone"))

    ids = store.prune(only_superseded=True, dry_run=False)
    assert ids == [original.id]
    assert store.get(untouched.id).id == untouched.id


def test_prune_filter_access_count(store: Store) -> None:
    cold = store.write(Entry(type="event", content="cold"))
    warm = store.write(Entry(type="event", content="warm"))
    # Bump warm's access_count by reading it twice
    store.get(warm.id)
    store.get(warm.id)

    ids = store.prune(access_count_below=2, dry_run=False)
    assert ids == [cold.id]


def test_prune_cleans_tag_junction(store: Store, tmp_path: Path) -> None:
    import sqlite3

    e = store.write(Entry(type="event", content="tagged", tags=["a", "b"]))
    assert (
        store._conn.execute(
            "SELECT COUNT(*) FROM entry_tag WHERE entry_id = ?", (e.id,)
        ).fetchone()[0]
        == 2
    )

    store.prune(tags=["a"], dry_run=False)
    assert (
        store._conn.execute(
            "SELECT COUNT(*) FROM entry_tag WHERE entry_id = ?", (e.id,)
        ).fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# iter_export + import_entry
# ---------------------------------------------------------------------------

def test_iter_export_ordered_by_created_at(store: Store) -> None:
    a = store.write(Entry(type="fact", content="a"))
    time.sleep(1.1)
    b = store.write(Entry(type="fact", content="b"))

    exported = list(store.iter_export())
    assert [e.id for e in exported] == [a.id, b.id]


def test_import_entry_preserves_id_and_timestamps(tmp_path: Path) -> None:
    src = Store.open(agent="src", path=tmp_path / "src.db")
    dst = Store.open(agent="dst", path=tmp_path / "dst.db")
    try:
        a = src.write(
            Entry(
                type="learning",
                content="lesson",
                tags=["alpha"],
                confidence=0.8,
            )
        )
        # Bump access_count on src so we can verify preservation
        src.get(a.id)
        src.get(a.id)

        # Round-trip
        for entry in src.iter_export():
            dst.import_entry(entry)

        fetched_raw = dst._conn.execute(
            "SELECT * FROM entry WHERE id = ?", (a.id,)
        ).fetchone()
        from session_loam.store import _row_to_entry

        fetched = _row_to_entry(fetched_raw)
        assert fetched.id == a.id
        assert fetched.created_at == a.created_at
        assert fetched.access_count == 2
        assert fetched.tags == ["alpha"]
        assert fetched.confidence == pytest.approx(0.8)
    finally:
        src.close()
        dst.close()


def test_import_entry_skips_existing_by_default(store: Store) -> None:
    original = store.write(Entry(type="fact", content="original"))
    duplicate = Entry(
        id=original.id,
        type="fact",
        content="overwrite attempt",
        created_at=original.created_at,
        last_accessed=original.last_accessed,
    )
    with pytest.raises(EntryAlreadyExists):
        store.import_entry(duplicate)


def test_import_entry_replace_overwrites(store: Store) -> None:
    original = store.write(Entry(type="fact", content="original"))
    duplicate = Entry(
        id=original.id,
        type="fact",
        content="overwrite content",
        tags=["new"],
        created_at=original.created_at,
        last_accessed=original.last_accessed,
    )
    store.import_entry(duplicate, replace=True)
    # No-reinforce read
    row = store._conn.execute(
        "SELECT * FROM entry WHERE id = ?", (original.id,)
    ).fetchone()
    from session_loam.store import _row_to_entry

    refetched = _row_to_entry(row)
    assert refetched.content == "overwrite content"
    assert refetched.tags == ["new"]


def test_import_entry_requires_timestamps(store: Store) -> None:
    e = Entry(type="fact", content="missing timestamps", id="e.imptest1")
    with pytest.raises(ValueError):
        store.import_entry(e)
