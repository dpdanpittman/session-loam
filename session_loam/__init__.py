"""session-loam — memory-continuity substrate for agentic workflows.

See ``DESIGN.md`` for the full v0.1 spec.

Public API:

- :class:`Store` — per-agent SQLite-backed memory store
- :class:`Entry` — single memory record dataclass
- :class:`SearchResult` — FTS5 retrieval result (day 3+)
"""

from session_loam import search
from session_loam.entry import Entry, SearchResult
from session_loam.search import RankWeights
from session_loam.store import Store

__all__ = [
    "Store",
    "Entry",
    "SearchResult",
    "RankWeights",
    "search",
]
__version__ = "0.1.0.dev0"
SCHEMA_VERSION = "0.1"
