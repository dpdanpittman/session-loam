"""Store — per-agent SQLite-backed memory.

One ``.db`` file per agent identity. Lives at ``~/.session-loam/<agent>.db``
by default; the path is overridable via :meth:`Store.open` for tests and
custom layouts.

Day 2 surface: :meth:`Store.open`, :meth:`Store.write`, :meth:`Store.get`.
Day 3 adds :meth:`Store.search` + :meth:`Store.recent`.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from session_loam.entry import Entry

SCHEMA_VERSION = "0.1"
DEFAULT_BASE_DIR = Path.home() / ".session-loam"
ENV_AGENT = "LOAM_AGENT"
ENV_BASE_DIR = "LOAM_BASE_DIR"

ID_PREFIX = "e."
ID_ALPHABET = "0123456789abcdefghijklmnopqrstuv"  # base32, lowercase
ID_BODY_LEN = 8

_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS entry (
        id              TEXT PRIMARY KEY,
        agent           TEXT NOT NULL,
        type            TEXT NOT NULL,
        content         TEXT NOT NULL,
        source          TEXT,
        confidence      REAL,
        tags            TEXT,
        created_at      TEXT NOT NULL,
        last_accessed   TEXT NOT NULL,
        access_count    INTEGER NOT NULL DEFAULT 0,
        supersedes      TEXT,
        superseded_by   TEXT,
        attestations    TEXT,
        metadata        TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS entry_tag (
        entry_id TEXT NOT NULL,
        tag      TEXT NOT NULL,
        PRIMARY KEY (entry_id, tag),
        FOREIGN KEY (entry_id) REFERENCES entry(id) ON DELETE CASCADE
    );
    """,
    "CREATE INDEX IF NOT EXISTS entry_agent_type    ON entry (agent, type);",
    "CREATE INDEX IF NOT EXISTS entry_created_at    ON entry (created_at);",
    "CREATE INDEX IF NOT EXISTS entry_last_accessed ON entry (last_accessed);",
    "CREATE INDEX IF NOT EXISTS entry_supersedes    ON entry (supersedes);",
    "CREATE INDEX IF NOT EXISTS entry_tag_tag       ON entry_tag (tag);",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS entry_fts USING fts5(
        content,
        tags,
        content_rowid=rowid,
        tokenize='porter unicode61'
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entry_after_insert AFTER INSERT ON entry BEGIN
        INSERT INTO entry_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entry_after_update AFTER UPDATE ON entry BEGIN
        UPDATE entry_fts
           SET content = new.content,
               tags    = new.tags
         WHERE rowid = new.rowid;
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS entry_after_delete AFTER DELETE ON entry BEGIN
        DELETE FROM entry_fts WHERE rowid = old.rowid;
    END;
    """,
)


class SchemaVersionMismatch(RuntimeError):
    """Raised on open when the on-disk schema major version is unsupported."""


class EntryNotFound(KeyError):
    """Raised by :meth:`Store.get` when no entry matches the given id."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    body = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_BODY_LEN))
    return f"{ID_PREFIX}{body}"


def _resolve_agent(explicit: str | None) -> str:
    if explicit:
        return explicit.strip().lower()
    env = os.environ.get(ENV_AGENT)
    if env:
        return env.strip().lower()
    return socket.gethostname().split(".")[0].strip().lower()


def _resolve_db_path(agent: str, path: Path | str | None) -> Path:
    if path is not None:
        return Path(path)
    base = os.environ.get(ENV_BASE_DIR)
    base_dir = Path(base) if base else DEFAULT_BASE_DIR
    return base_dir / f"{agent}.db"


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("\n".join(_SCHEMA_DDL))
    conn.execute(
        "INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def _check_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM _meta WHERE key = 'schema_version'"
    ).fetchone()
    on_disk = row[0] if row else None
    if on_disk is None:
        return
    on_disk_major = on_disk.split(".", 1)[0]
    code_major = SCHEMA_VERSION.split(".", 1)[0]
    if on_disk_major != code_major:
        raise SchemaVersionMismatch(
            f"On-disk schema_version={on_disk!r} incompatible with "
            f"library schema_version={SCHEMA_VERSION!r}"
        )


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        agent=row["agent"],
        type=row["type"],
        content=row["content"],
        source=row["source"],
        confidence=row["confidence"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
        access_count=row["access_count"],
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
        attestations=json.loads(row["attestations"]) if row["attestations"] else [],
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
    )


class Store:
    """Per-agent memory store backed by a single SQLite file."""

    def __init__(self, agent: str, path: Path, conn: sqlite3.Connection) -> None:
        self.agent = agent
        self.path = path
        self._conn = conn

    @classmethod
    def open(
        cls,
        agent: str | None = None,
        *,
        path: Path | str | None = None,
    ) -> "Store":
        """Open (or create) the store for ``agent``.

        Agent resolution order: explicit arg → ``$LOAM_AGENT`` → hostname.
        Path resolution order: explicit ``path`` → ``$LOAM_BASE_DIR/<agent>.db``
        → ``~/.session-loam/<agent>.db``.
        """
        resolved_agent = _resolve_agent(agent)
        db_path = _resolve_db_path(resolved_agent, path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")

        _apply_schema(conn)
        _check_schema_version(conn)

        return cls(agent=resolved_agent, path=db_path, conn=conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write(self, entry: Entry) -> Entry:
        """Persist ``entry``. Returns the entry with id/agent/timestamps filled in.

        If ``entry.supersedes`` references an existing entry, that predecessor
        gets its ``superseded_by`` back-ref set to the new id in the same
        transaction.
        """
        if entry.id is None:
            entry.id = _new_id()
        if entry.agent is None:
            entry.agent = self.agent
        now = _utcnow_iso()
        if entry.created_at is None:
            entry.created_at = now
        if entry.last_accessed is None:
            entry.last_accessed = entry.created_at

        tags_json = json.dumps(list(entry.tags)) if entry.tags else None
        attestations_json = (
            json.dumps(list(entry.attestations)) if entry.attestations else None
        )
        metadata_json = json.dumps(dict(entry.metadata)) if entry.metadata else None

        with self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute(
                """
                INSERT INTO entry (
                    id, agent, type, content, source, confidence, tags,
                    created_at, last_accessed, access_count,
                    supersedes, superseded_by, attestations, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.agent,
                    entry.type,
                    entry.content,
                    entry.source,
                    entry.confidence,
                    tags_json,
                    entry.created_at,
                    entry.last_accessed,
                    entry.access_count,
                    entry.supersedes,
                    entry.superseded_by,
                    attestations_json,
                    metadata_json,
                ),
            )
            self._insert_tags(entry.id, entry.tags)
            if entry.supersedes:
                self._conn.execute(
                    "UPDATE entry SET superseded_by = ? WHERE id = ?",
                    (entry.id, entry.supersedes),
                )

        return entry

    def get(self, entry_id: str) -> Entry:
        """Fetch one entry by id. Reinforces (bumps last_accessed + access_count).

        Raises :class:`EntryNotFound` if no entry matches.
        """
        row = self._conn.execute(
            "SELECT * FROM entry WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise EntryNotFound(entry_id)

        now = _utcnow_iso()
        with self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute(
                """
                UPDATE entry
                   SET last_accessed = ?,
                       access_count  = access_count + 1
                 WHERE id = ?
                """,
                (now, entry_id),
            )

        entry = _row_to_entry(row)
        entry.last_accessed = now
        entry.access_count = (entry.access_count or 0) + 1
        return entry

    def _insert_tags(self, entry_id: str, tags: Iterable[str]) -> None:
        seen: set[str] = set()
        for tag in tags or ():
            normalized = tag.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            self._conn.execute(
                "INSERT OR IGNORE INTO entry_tag (entry_id, tag) VALUES (?, ?)",
                (entry_id, normalized),
            )
