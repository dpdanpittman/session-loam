"""Retrieval — FTS5 search + recent + ranking.

Day 3 module. Wraps SQLite FTS5 (already wired in :mod:`session_loam.store`)
with tag/type/time-range filters and a tunable ranking layer that combines
FTS5 bm25 with recency / confidence / access-count boosts.

Default policy: superseded entries are hidden from search and recent. Pass
``include_superseded=True`` to override (auditing, restoration, debugging).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from session_loam.entry import Entry, SearchResult


@dataclass(frozen=True)
class RankWeights:
    """Multiplicative-boost coefficients layered on top of bm25.

    Each strength controls how much the corresponding signal can push the
    final score above the bm25 baseline. Set to 0.0 to disable a signal.

    - ``recency_half_life_days``: a hit's recency factor halves every this
      many days of age (computed from ``created_at``).
    - ``recency_strength``: multiplier on the recency factor.
    - ``confidence_strength``: multiplier on ``entry.confidence`` (treats
      missing confidence as 0.5 — neutral).
    - ``access_count_strength``: multiplier on ``log1p(access_count)``.
    """

    recency_half_life_days: float = 14.0
    recency_strength: float = 0.5
    confidence_strength: float = 0.3
    access_count_strength: float = 0.1


DEFAULT_WEIGHTS = RankWeights()

CANDIDATE_OVERSAMPLE = 4
"""Fetch ``limit * CANDIDATE_OVERSAMPLE`` rows by bm25 then re-rank with
boosts. Keeps work bounded on big stores while letting boosts pull
slightly-worse-bm25 hits above slightly-better ones."""


def search(
    conn: sqlite3.Connection,
    *,
    query: str,
    tags: Iterable[str] | None = None,
    type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_superseded: bool = False,
    limit: int = 20,
    weights: RankWeights = DEFAULT_WEIGHTS,
    reinforce: bool = True,
) -> list[SearchResult]:
    """FTS5 + filter + rank.

    ``query`` is an FTS5 MATCH expression (see SQLite FTS5 docs for operators:
    AND, OR, NOT, NEAR, prefix ``*``, phrase ``"..."``).

    Tags filter is AND across the provided set (entry must carry all of them).
    """
    if not query or not query.strip():
        raise ValueError("search() requires a non-empty query; use recent() for filter-only retrieval")

    where, params = _build_filters(
        tags=list(tags or ()),
        type=type,
        since=since,
        until=until,
        include_superseded=include_superseded,
    )

    candidate_limit = max(limit * CANDIDATE_OVERSAMPLE, limit)

    sql = f"""
        SELECT
            e.id, e.agent, e.type, e.content, e.source, e.confidence, e.tags,
            e.created_at, e.last_accessed, e.access_count,
            e.supersedes, e.superseded_by, e.attestations, e.metadata,
            bm25(entry_fts) AS bm25_score,
            snippet(entry_fts, 0, '[', ']', '...', 16) AS fts_snippet
        FROM entry_fts
        JOIN entry e ON e.rowid = entry_fts.rowid
        WHERE entry_fts MATCH ?
          {where}
        ORDER BY bm25_score ASC
        LIMIT ?
    """
    rows = conn.execute(sql, [query, *params, candidate_limit]).fetchall()

    now = datetime.now(timezone.utc)
    scored: list[tuple[float, SearchResult]] = []
    for row in rows:
        entry = _row_to_entry(row)
        bm25_raw = row["bm25_score"] or 0.0
        bm25_norm = -bm25_raw  # bm25 returns negative scores; flip so higher=better
        boost = _compute_boost(entry, now=now, weights=weights)
        final = bm25_norm * (1.0 + boost)
        scored.append((final, SearchResult(entry=entry, rank=final, snippet=row["fts_snippet"] or "")))

    scored.sort(key=lambda t: t[0], reverse=True)
    results = [s for _, s in scored[:limit]]

    if reinforce and results:
        _reinforce([r.entry.id for r in results], conn)
        now_iso = _iso_now()
        for r in results:
            r.entry.last_accessed = now_iso
            r.entry.access_count = (r.entry.access_count or 0) + 1

    return results


def recent(
    conn: sqlite3.Connection,
    *,
    type: str | None = None,
    tags: Iterable[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    include_superseded: bool = False,
    limit: int = 20,
    reinforce: bool = True,
) -> list[Entry]:
    """Time-ordered fetch (newest ``created_at`` first) with filters."""
    where, params = _build_filters(
        tags=list(tags or ()),
        type=type,
        since=since,
        until=until,
        include_superseded=include_superseded,
    )
    sql = f"""
        SELECT
            e.id, e.agent, e.type, e.content, e.source, e.confidence, e.tags,
            e.created_at, e.last_accessed, e.access_count,
            e.supersedes, e.superseded_by, e.attestations, e.metadata
        FROM entry e
        WHERE 1=1
          {where}
        ORDER BY e.created_at DESC
        LIMIT ?
    """
    rows = conn.execute(sql, [*params, limit]).fetchall()
    entries = [_row_to_entry(r) for r in rows]

    if reinforce and entries:
        _reinforce([e.id for e in entries], conn)
        now_iso = _iso_now()
        for e in entries:
            e.last_accessed = now_iso
            e.access_count = (e.access_count or 0) + 1

    return entries


def escape_query(text: str) -> str:
    """Quote a literal string so FTS5 treats it as a phrase, not operators.

    Wraps in double quotes, escaping any embedded double quotes by doubling.
    For when the caller wants exact-phrase retrieval and isn't writing FTS5
    boolean operators themselves.
    """
    return '"' + text.replace('"', '""') + '"'


def _build_filters(
    *,
    tags: list[str],
    type: str | None,
    since: str | None,
    until: str | None,
    include_superseded: bool,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []

    if type is not None:
        clauses.append("AND e.type = ?")
        params.append(type)
    if since is not None:
        clauses.append("AND e.created_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("AND e.created_at <= ?")
        params.append(until)
    if not include_superseded:
        clauses.append("AND e.superseded_by IS NULL")
    if tags:
        normalized = sorted({t.strip().lower() for t in tags if t and t.strip()})
        if normalized:
            placeholders = ",".join("?" * len(normalized))
            clauses.append(
                f"""AND e.id IN (
                    SELECT entry_id FROM entry_tag
                     WHERE tag IN ({placeholders})
                     GROUP BY entry_id
                    HAVING COUNT(DISTINCT tag) = ?
                )"""
            )
            params.extend(normalized)
            params.append(len(normalized))

    return "\n          ".join(clauses), params


def _compute_boost(entry: Entry, *, now: datetime, weights: RankWeights) -> float:
    boost = 0.0
    if weights.recency_strength > 0.0 and entry.created_at:
        try:
            created = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
            age_days = max((now - created).total_seconds() / 86400.0, 0.0)
            half_life = max(weights.recency_half_life_days, 0.0001)
            recency_factor = 0.5 ** (age_days / half_life)
        except (ValueError, AttributeError):
            recency_factor = 0.0
        boost += weights.recency_strength * recency_factor

    if weights.confidence_strength > 0.0:
        conf = entry.confidence if entry.confidence is not None else 0.5
        boost += weights.confidence_strength * conf

    if weights.access_count_strength > 0.0:
        boost += weights.access_count_strength * math.log1p(max(entry.access_count or 0, 0))

    return boost


def _reinforce(entry_ids: list[str], conn: sqlite3.Connection) -> None:
    if not entry_ids:
        return
    placeholders = ",".join("?" * len(entry_ids))
    with conn:
        conn.execute("BEGIN")
        conn.execute(
            f"""
            UPDATE entry
               SET last_accessed = ?,
                   access_count  = access_count + 1
             WHERE id IN ({placeholders})
            """,
            [_iso_now(), *entry_ids],
        )


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_entry(row: sqlite3.Row) -> Entry:
    import json

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
