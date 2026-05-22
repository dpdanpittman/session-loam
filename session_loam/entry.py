"""Entry + SearchResult dataclasses.

Storage-layer is in :mod:`session_loam.store`; this module only defines the
in-memory shape that callers construct and receive.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Entry:
    """A single memory entry.

    Fields left as ``None`` on write get filled in by :meth:`Store.write`:
    ``id``, ``agent``, ``created_at``, ``last_accessed``.
    """

    type: str
    content: str
    id: str | None = None
    agent: str | None = None
    source: str | None = None
    confidence: float | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str | None = None
    last_accessed: str | None = None
    access_count: int = 0
    supersedes: str | None = None
    superseded_by: str | None = None
    attestations: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    """Result of a :meth:`Store.search` call. Populated on day 3."""

    entry: Entry
    rank: float
    snippet: str
