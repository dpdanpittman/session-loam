# session-loam — Design Spec v0.1

Status: **Draft — pending review**
Inspired by: the Inkcloud Architecture Post-Mortem ("Unix Swarm Blueprint") + the substrate-layer playbook that produced [session-essence](https://github.com/dpdanpittman/session-essence) and [swarm-lib](https://github.com/dpdanpittman/swarm-lib).

Companion: `~/.claude/projects/-home-dan-src/memory/project_next_substrates.md` (the "what comes next" thinking that named this project).

---

## TL;DR

A SQLite-backed memory substrate for autonomous agents. Identity-scoped (each agent has its own store). Tag + full-text retrieval via FTS5. Never-delete by default with reinforce-on-retrieve. Designed to compose with session-essence (identity), swarm-lib (work), and tribunal (trust) without depending on any of them — same standalone-usable ethos.

What it solves: **learning-as-state**. Today, what an agent has *learned across runs* either lives in chat history (volatile, lost at compaction), in ad-hoc files (no schema, no retrieval), or in a generic vector DB (no identity scoping, no decay, no attestation). Each option fails a constraint the agentic case needs. session-loam is the purpose-built substrate.

---

## Why this exists

Agentic systems today have three substrate problems already addressed (essence solves identity-continuity, swarm-lib solves work-continuity, tribunal solves output trust). The unsolved one:

| Problem | Manifestation | session-loam answer |
|---|---|---|
| **Learning-as-state** | "I solved this same problem three sessions ago" — but the agent has no durable record of the solution; it solves it again, possibly worse | Persistent, queryable, identity-scoped memory store |
| **No identity scoping in existing tools** | Vector DBs (ChromaDB, pinecone, mem0) treat memory as a global blob. An agent has no concept of "what *I* learned" vs. "what some other agent learned" | Per-agent storage partitioned by essence portrait name; explicit grants for federation |
| **Retrieval shape mismatch** | Vector search is great for semantic similarity but bad for "what did I observe about user X last week?" Tag + time queries require a different index | FTS5 + tags + time ordering as substrate primitives; vector embeddings as opt-in v0.2 |
| **No decay / reinforcement** | Memories that matter should stay; memories that don't should fade. Most stores grow unbounded with equal weight | `last_accessed` + `access_count`; retrieval bumps both. Pruning by relevance is the operator's call. |
| **No attestation pathway** | If a memory turns out to be wrong, there's no canonical way to flag it. The agent just keeps reading the bad memory | `attestations` array per entry; tribunal can sign claims about validity; suspect memories get flagged |

This is the fourth leg next to essence + swarm-lib + tribunal. Same playbook: name the failure mode, build the POSIX-minimal substrate, ship.

---

## Architectural principles

### 1. SQLite is the substrate

One file per agent: `~/.session-loam/<agent>.db` (or `~/.mabus/memory/<agent>.db` when integrated with Mabus OS). FTS5 virtual table indexes content + tags. Single-file durability + ACID + queryable by any SQLite client. No daemon, no broker, no external service.

### 2. Identity-scoped by default

Each agent has its own store. Cross-agent reads are explicit (v0.2 federation work). This matches essence's per-agent portrait model and prevents accidental cross-pollination of distinct identities.

### 3. Standalone-usable

session-loam doesn't require essence, swarm-lib, or tribunal to be installed. It defines integration *hooks* (attestation arrays, lifecycle event subscribers) but they're opt-in. An operator using session-loam without any other substrate gets a perfectly usable memory store.

### 4. Never-delete, reinforce-on-retrieve

Default policy: entries persist forever. Retrieving an entry bumps `last_accessed` + `access_count`. Pruning is the operator's call (manual `loam-cli prune` invocation with a filter). No background sweeper in v0.1.

### 5. Retrieval shape: tag + full-text + time, not vector

v0.1 ships FTS5 (full-text search on content) + tag exact-match + time-range queries. No embedding model dependency. Vector embeddings are a v0.2 bolt-on for semantic similarity — they extend the substrate, they don't replace it.

### 6. Append-mostly, edit-explicit

Entries are written once and rarely edited. When they are edited, we keep the prior version (write a new entry with `supersedes` pointing back). Memory should be auditable.

---

## Schema

### Entry

A single memory entry. Every record in the database is one of these.

```json
{
  "schema_version": "0.1",
  "id": "e.r3x2a",
  "agent": "mabus",
  "type": "observation",
  "content": "Dan prefers commits split by concern; will push back on bundled-PRs in 'safe' work but tolerates bundling for one-shot operations.",
  "source": "session:2026-05-21T03:00Z",
  "confidence": 0.85,
  "tags": ["dan", "preference", "git", "feedback"],
  "created_at": "2026-05-22T04:12:33Z",
  "last_accessed": "2026-05-22T04:12:33Z",
  "access_count": 0,
  "supersedes": null,
  "attestations": [],
  "metadata": {}
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | string | yes | semver of the entry schema; lib refuses to read unknown majors |
| `id` | string | yes | Unique within `(agent,)`. Short prefix convention: `e.<base32>` |
| `agent` | string | yes | Owning agent identity (matches essence portrait name, lowercased) |
| `type` | string | yes | Entry kind: `observation`, `fact`, `learning`, `event`, `reference`, `episode`, free-form |
| `content` | string | yes | The memory itself. Indexed by FTS5. Markdown OK. |
| `source` | string | no | Where this came from: `session:<ts>`, `import:<path>`, `attestation:<id>`, free-form |
| `confidence` | float | no | 0.0–1.0. Caller's belief in the entry's accuracy. Used in retrieval ranking. |
| `tags` | string[] | no | Indexed for exact-match filter. Lowercase by convention. |
| `created_at` | ISO-8601 string | yes | Write time |
| `last_accessed` | ISO-8601 string | yes | Most recent retrieval. Updated on every successful `get` or `search` hit. |
| `access_count` | int | yes | Lifetime retrieval count. Reinforce-on-retrieve. |
| `supersedes` | string \| null | no | If this entry replaces another, the prior `id`. The prior entry is kept (auditable). |
| `attestations` | object[] | no | Signed claims about this entry's validity. Tribunal integration in v0.2. |
| `metadata` | object | no | Free-form. Consumer-specific. Lib doesn't index. |

### Evolution policy

Same as swarm-lib's status.json policy:
- Minor schema bumps (`0.1` → `0.2`) are additive only.
- Major bumps (`0.1` → `1.0`) are breaking; lib supports reading the previous major for one full version cycle.

---

## Storage layout

### Per-agent file

```
~/.session-loam/
└── <agent>.db        # SQLite database, one per agent
```

Schema:

```sql
CREATE TABLE IF NOT EXISTS entry (
  id              TEXT PRIMARY KEY,
  agent           TEXT NOT NULL,
  type            TEXT NOT NULL,
  content         TEXT NOT NULL,
  source          TEXT,
  confidence      REAL,
  tags            TEXT,        -- JSON-encoded array
  created_at      TEXT NOT NULL,
  last_accessed   TEXT NOT NULL,
  access_count    INTEGER NOT NULL DEFAULT 0,
  supersedes      TEXT,
  attestations    TEXT,        -- JSON-encoded array
  metadata        TEXT         -- JSON-encoded object
);

CREATE VIRTUAL TABLE IF NOT EXISTS entry_fts USING fts5(
  content,
  tags,
  content_rowid=rowid,
  tokenize='porter unicode61'
);

CREATE INDEX IF NOT EXISTS entry_agent_type ON entry (agent, type);
CREATE INDEX IF NOT EXISTS entry_created_at ON entry (created_at);
CREATE INDEX IF NOT EXISTS entry_last_accessed ON entry (last_accessed);

-- Triggers keep FTS in sync with entry table
CREATE TRIGGER IF NOT EXISTS entry_after_insert AFTER INSERT ON entry BEGIN
  INSERT INTO entry_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS entry_after_update AFTER UPDATE ON entry BEGIN
  UPDATE entry_fts SET content = new.content, tags = new.tags WHERE rowid = new.rowid;
END;
CREATE TRIGGER IF NOT EXISTS entry_after_delete AFTER DELETE ON entry BEGIN
  DELETE FROM entry_fts WHERE rowid = old.rowid;
END;
```

### Why SQLite (and not the filesystem)?

session-loam diverges from swarm-lib + essence on this point. Those two use plain files because the access pattern is "low-frequency append, atomic rename, durable handoff." Memory's access pattern is "many writes, many reads, FTS queries, time-range filters" — SQLite is the right tool. We still treat *the file* as the substrate (POSIX semantics, single-host, no daemon, backup-able with rsync), so the spirit is preserved.

---

## Python API (v0.1)

```python
# session_loam/store.py
from session_loam import Store, Entry

store = Store.open(agent="mabus")  # opens ~/.session-loam/mabus.db

# Write
store.write(Entry(
    type="observation",
    content="Dan prefers terse responses with no trailing summaries.",
    tags=["dan", "preference"],
    confidence=0.95,
    source="session:2026-05-22T03:00Z",
))

# Retrieve by id (touches last_accessed + access_count)
entry = store.get("e.r3x2a")

# Full-text + tag + type filter
results = store.search(
    query="terse responses",            # FTS5 query syntax
    tags=["preference"],                 # AND across tags
    type="observation",
    since="2026-05-01T00:00Z",
    limit=20,
)

# Time-ordered recent fetch
recent = store.recent(type="learning", limit=10)

# Update (writes new entry with supersedes -> old id; old is preserved)
store.supersede("e.r3x2a", new_content="Dan prefers terse responses; trailing summaries OK only when explicitly asked for.")

# Manual prune (operator-driven)
store.prune(filter={"type": "event", "older_than": "30d", "access_count_below": 2})

# Attestation (v0.2: tribunal will write to this; v0.1 surface exists, semantics TBD)
store.attest("e.r3x2a", attestation={"signer": "tribunal-v0.5", "verdict": "verified", "ts": "..."})
```

### Dataclasses

```python
@dataclass
class Entry:
    type: str
    content: str
    id: str = None              # auto-generated if not provided
    agent: str = None           # filled by Store on write
    source: str = None
    confidence: float = None
    tags: list[str] = field(default_factory=list)
    created_at: str = None      # filled by Store on write
    last_accessed: str = None   # filled by Store on write
    access_count: int = 0
    supersedes: str = None
    attestations: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

@dataclass
class SearchResult:
    entry: Entry
    rank: float                 # FTS5 bm25 score, possibly boosted by recency + confidence
    snippet: str                # FTS5 snippet around the matched terms
```

---

## CLI: `loam-cli`

Subprocess surface for shell consumers (matches swarm-lib's `swarm-cli` pattern).

```text
loam-cli write     # add an entry (--type, --content, --tags, --source, --confidence)
loam-cli get       # fetch one by id; emits JSON
loam-cli search    # FTS5 query + filters; emits JSON array
loam-cli recent    # time-ordered; emits JSON array
loam-cli supersede # write a replacement entry
loam-cli prune     # operator-driven cleanup with filter
loam-cli ls        # human-readable summary: count, last-accessed, top tags, recent writes
loam-cli export    # dump entries as JSONL to stdout
loam-cli import    # read JSONL on stdin, upsert
```

JSON output is single-line, jq-friendly:

```bash
loam-cli search --query "compaction" --agent mabus | jq '.[].entry.content'
```

### Worker_loop / swarm-lib integration

When session-loam is installed alongside swarm-lib, a handler can:

```bash
# Read memory before doing work
RELEVANT=$(loam-cli search --query "$TASK_TYPE" --agent "$SWARM_WORKER_ID" --limit 5)

# Write learnings after the task
echo "..." | loam-cli write --type learning --tags "$TASK_TYPE,outcome:success" \
  --source "swarm:$SWARM_RUN_DIR/$SWARM_TASK_ID"
```

---

## Compaction integration

When essence's PreCompact hook fires (or any session-end signal), session-loam writes a **compact-snapshot** entry:

```json
{
  "type": "compact-snapshot",
  "content": "Summary of the most active threads and decisions from this session...",
  "tags": ["compact-snapshot", "session:<timestamp>"],
  "source": "essence:precompact-hook"
}
```

The next session's SessionStart hook can `loam-cli recent --type compact-snapshot --limit 1` to find the bridge entry. Combined with essence's portrait and swarm-lib's status.json, this is the full three-file picture: who I am, what I was doing, what I learned.

The snapshot content is produced by the same synthesis pass that essence runs; the difference is the destination (portrait vs. memory entry).

---

## Tribunal integration (v0.2)

Each entry has an `attestations` array. Each attestation:

```json
{
  "signer": "tribunal-v0.5",
  "signer_pubkey": "ed25519:...",
  "ts": "2026-05-22T...",
  "verdict": "verified | suspect | refuted",
  "claim": "the URL referenced in this entry returns 200 as of <ts>",
  "signature": "..."
}
```

v0.1 ships the array as data only — no semantic enforcement. v0.2 wires tribunal to write attestations and adds retrieval filters (`store.search(..., min_attestations=1)`, `store.search(..., exclude_suspect=True)`).

---

## Federation (v0.3)

Single-host in v0.1. v0.3 will add:

- **Read-only mounts**: `Store.open(agent="other-agent", mode="readonly")` opens another agent's db with a grant check
- **Grants**: a per-agent `grants.json` declaring "agent X may read entries with tags T from my store"
- **Cross-agent search**: `store.search_federation(...)` queries every store the caller has read grants for and merges results

Crypto-native option later: signed grant tokens on a chain so cross-agent read access is verifiable without trusting the grant file itself.

---

## Out of scope for v0.1

- No vector embeddings (FTS5 + tags only)
- No automatic decay or background pruning (manual via `loam-cli prune`)
- No federation (single-host)
- No GUI / web dashboard (the SQLite file is queryable with any client)
- No tribunal attestation semantics (just the storage shape)
- No automatic compact-snapshot synthesis (the operator wires it via essence hooks)
- No replication / multi-host writes
- No multi-agent concurrent-write safety beyond SQLite's WAL mode

---

## Build plan

| Day | Deliverable |
|---|---|
| 1 | This design doc (v0.1 spec) — already done if you're reading it |
| 2 | `session_loam/store.py` + SQLite schema + smoke tests for write/get |
| 3 | `session_loam/search.py` + FTS5 retrieval + tag + time filters + tests |
| 4 | `loam-cli` exposing write/get/search/recent/ls; matches `swarm-cli` patterns |
| 5 | `loam-cli` supersede/prune/export/import; round-trip tests |
| 6 | Compaction-integration example: an essence PreCompact hook that writes a compact-snapshot to loam |
| 7 | README + minimal docs + cut v0.1.0 tag |

Optional day 8+: marketing site at `loam.mabus.ai` + mabus.ai hub page; PyPI release.

---

## Open questions for review

1. **Repo name vs. package name.** Repo `session-loam`, package `session_loam`, CLI `loam-cli`. The `session-` prefix pairs with `session-essence`. Alternative: drop the prefix (`loam`, `loam-lib`). My lean: keep the prefix because pairing with essence is the point.
2. **Agent identity resolution.** When `Store.open(agent=None)`, what's the default? Env var (`LOAM_AGENT`), hostname, essence portrait name lookup, or required-explicit? My lean: env var with hostname fallback; integration layer (Mabus OS) can override.
3. **Confidence scale.** 0.0–1.0 float vs. enum (low/medium/high) vs. omitted. My lean: 0.0–1.0 float, caller can use whatever conventions they want; lib doesn't enforce ranges.
4. **Supersede semantics.** When entry B supersedes A, does A become invisible to default queries (only surfaces with `--include-superseded`), or stay first-class with a flag? My lean: hidden by default for search/recent; visible for `get` by id and audit queries.
5. **`tags` representation in SQLite.** JSON-encoded array in a TEXT column (simple, full-row reads) vs. junction table (`entry_tag(entry_id, tag)` — properly normalized, faster tag-exact-match queries). My lean: junction table; FTS5 indexes the JSON repr too for free-text matching but exact tag filters use the junction.
6. **Retrieval ranking.** FTS5 bm25 alone, or boosted by recency + confidence + access_count? My lean: bm25 + multiplicative boosts with operator-tunable weights; defaults are reasonable, can override per-query.
7. **`type` taxonomy.** Free-form string or constrained enum? My lean: free-form. Conventions emerge from use; lib doesn't enforce.
8. **Concurrent writes.** SQLite WAL mode handles single-process concurrent reads + serialized writes. For multiple Python processes writing to the same db file, do we add a higher-level lock or rely on SQLite's BUSY retry? My lean: rely on SQLite BUSY retry + WAL; document the operator's responsibility for write-concurrency tuning.

---

## Risks

- **Schema lock-in.** SQLite + FTS5 is hard to migrate away from once you have years of accumulated entries. Mitigate: schema_version + strict major-version refusal; v0.2 vector-embedding addition is purely additive.
- **Identity drift.** If essence's portrait name changes over time (e.g., from `claude` to `mabus`), historical entries are stranded. Mitigate: `agent` field is a stable identifier set once; renaming requires `loam-cli rebind --from --to` operator action.
- **Unbounded growth.** Never-delete + reinforce-on-retrieve means the db file grows over time. SQLite handles GB+ comfortably, but pruning eventually matters. Mitigate: clear `loam-cli prune` UX; documented growth expectations.
- **Bad attestations.** If a tribunal mis-attests an entry as suspect, downstream consumers may exclude it incorrectly. Mitigate: attestations are append-only with signer identity; consumers can ignore specific signers; tribunal v0.6+ adds revocation.
- **FTS5 query syntax exposure.** Operators writing FTS5 query strings will get tripped up by special chars. Mitigate: helper `Store.escape_query()` + clear docs on operators (AND, OR, NOT, NEAR).
- **Single-process write contention.** Heavy concurrent writes from N worker_loop instances could trigger BUSY backoffs and slow writes. Mitigate: WAL mode + retries with jitter; document the upper concurrent-writer limit (likely ~10-20 before contention dominates).
- **Backup story unclear.** Standard SQLite backup tools (`.backup`, `rsync` on a quiescent file) work, but operators need to know not to copy a `*.db-journal`/`-wal` file mid-flight. Mitigate: `loam-cli backup --to <path>` does a proper online backup; docs cover the gotcha.

---

## Inspiration / prior art

- **session-essence** — direct lineage; this is the memory counterpart to essence's identity portrait
- **swarm-lib** — substrate-layer playbook + standalone-usable ethos
- **mem0** — agent memory library; conceptually adjacent but cloud-first and not identity-scoped
- **ChromaDB / Pinecone / Weaviate** — vector DBs; useful for semantic similarity but wrong shape for tag + time queries
- **Memory MCP** (Dan's `memory-mabus` etc.) — existing in-cluster memory service; will likely become a *consumer* of session-loam in production
- **Maildir + SQLite-backed mail clients** — the inspiration for "files are durable; a queryable index lives next to them"
- **CALO / personal knowledge management literature** — the "what should I remember and for how long?" question has decades of prior art

---

## Sign-off

Once this doc gets review/redirect:

1. Start on `session_loam/store.py` per the build plan (day 2)
2. Open a draft PR on the repo with the schema + Entry dataclass + smoke tests
3. Iterate based on feedback before locking the schema

No code lands until you greenlight the design.

