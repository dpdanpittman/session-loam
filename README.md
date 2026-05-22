# session-loam

> Memory-continuity substrate for agentic workflows. SQLite + FTS5, identity-scoped, tag + full-text retrieval, never-delete with reinforce-on-retrieve.

The fourth leg next to [session-essence](https://github.com/dpdanpittman/session-essence) (identity), [swarm-lib](https://github.com/dpdanpittman/swarm-lib) (work), and [tribunal](https://github.com/dpdanpittman/tribunal) (trust).

[**memory.mabus.ai**](https://memory.mabus.ai/) · [Design spec](DESIGN.md) · [Patterns](https://memory.mabus.ai/patterns) · [Examples](examples/)

---

## The failure mode

> _"I solved this same problem three sessions ago."_

The agent has no durable record of the solution, so it solves it again — possibly worse. Today there are three answers, and each one fails a constraint the agentic case needs:

| Approach                   | What it gets right                     | What it loses                                                        |
| -------------------------- | -------------------------------------- | -------------------------------------------------------------------- |
| **Chat-history-as-memory** | Zero setup, the agent is already there | Volatile; compaction strips it; nothing accumulates between runs     |
| **Vector DB**              | Strong semantic similarity over prose  | No identity scoping, no tag filters, no time queries, no attestation |
| **Notes folder + grep**    | Human-readable, browseable in editors  | No schema, no retrieval ranking, no reinforcement signal             |

**session-loam** is the purpose-built substrate-layer answer: filesystem-backed (a SQLite file per agent), identity-aware, tag + full-text + time retrieval, never-delete with reinforce-on-retrieve, ready to compose with the other substrates without depending on any of them.

---

## Three primitives

1. **One file per agent.** `~/.session-loam/<agent>.db`. SQLite, ACID, queryable by any client. Identity is the partition.
2. **FTS5 + tags + time.** Full-text search via SQLite's FTS5 virtual table. Tags via a junction table for exact-match filtering. Time-range queries via indexed timestamps. No embedding model in the critical path.
3. **Reinforce on retrieve.** Every successful `get`, `search`, or `recent` hit bumps the entry's `last_accessed` + `access_count`. Frequently-touched memories rank higher; untouched ones fade quietly in retrieval order without disappearing.

Plus the **never-delete rule**: edits are _supersedes_ — the new entry points back at the old one, the old row stays for audit, default search hides it.

---

## 60-second tour

```bash
git clone https://github.com/dpdanpittman/session-loam.git
cd session-loam
pip install -e .
```

```python
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

# Fetch by id (reinforces)
entry = store.get("e.r3x2a")

# FTS5 + tag + type + time search; bm25 + recency + confidence + access boosts
results = store.search(
    query="terse responses",
    tags=["preference"],
    type="observation",
    since="2026-05-01T00:00Z",
    limit=20,
)
for r in results:
    print(r.rank, r.entry.content, r.snippet)

# Time-ordered recall
recent = store.recent(type="learning", limit=10)

# Audit-preserving edit
store.write(Entry(
    type="observation",
    content="Dan tolerates trailing summaries when explicitly asked.",
    tags=["dan", "preference"],
    supersedes=entry.id,
))
```

Same surface over the shell:

```bash
echo "Dan prefers terse responses" | loam-cli write \
  --agent mabus --type observation --tags dan,preference --confidence 0.95

loam-cli search --query "terse responses" --tags preference --pretty
loam-cli recent --type learning --limit 10
loam-cli ls --agent mabus
```

---

## When to use it

**Reach for session-loam when:**

- The same agent identity outlives any single session.
- Learning should accumulate across runs (lessons, preferences, facts about users, prior outcomes).
- Retrieval queries have _shape_ — tag, type, time window, exact phrase — not just "find me semantically similar prose."
- You want to compose with other substrates (identity portraits, work-state queues, trust attestations).
- You want backup to be a single file you can rsync.

**Reach for something else when:**

- Retrieval is purely semantic similarity over large prose corpora → vector DB (Chroma, Pinecone, Weaviate).
- You need multi-host shared state with a network protocol → MCP memory server, Postgres, etc.
- The corpus is small and browsed by humans more than agents → flat markdown + grep.
- You need the agent to do LLM-driven extraction-from-conversation out of the box → mem0 ships that opinion built-in.

---

## How it composes

session-loam doesn't require essence, swarm-lib, or tribunal to be installed. It defines integration _hooks_ (attestation arrays, lifecycle event shape, examples) but they're opt-in.

| Substrate       | Provides                            | session-loam adds                                             |
| --------------- | ----------------------------------- | ------------------------------------------------------------- |
| session-essence | Identity portrait + compact hook    | A `compact-snapshot` bridge entry the next session reads back |
| swarm-lib       | Worker loop + queue substrate       | `loam-cli search` for prior lessons before doing work         |
| tribunal        | Adversarial review verdicts (v0.5+) | `attestations` array on each entry (v0.2 wires the writes)    |

See [`examples/compaction-bridge/`](examples/compaction-bridge/) for the essence integration as runnable bash hooks.

---

## API surface

### Python (`session_loam`)

```python
from session_loam import Store, Entry, RankWeights

# Identity + path resolution
Store.open(agent=None, *, path=None)        # $LOAM_AGENT > hostname; $LOAM_BASE_DIR > ~/.session-loam/

# Core operations
store.write(entry)                          # returns Entry with id/agent/timestamps filled in
store.get(entry_id)                         # reinforces; raises EntryNotFound on miss
store.search(query, *, tags, type, since,   # FTS5 + filters + bm25/recency/confidence/access ranking
             until, include_superseded,
             limit, weights, reinforce)
store.recent(*, type, tags, since, until,   # time-ordered; same filters, no text query
             include_superseded, limit, reinforce)

# Maintenance
store.prune(*, type, tags, older_than,      # refuses to delete without a filter; dry_run=True default
            access_count_below,
            only_superseded, dry_run)
store.iter_export()                         # yields every Entry, ordered by created_at
store.import_entry(entry, *, replace=False) # restore path; preserves id/timestamps/access_count
```

### CLI (`loam-cli`)

| Command              | Purpose                                                                        |
| -------------------- | ------------------------------------------------------------------------------ |
| `loam-cli write`     | Add an entry; reads content from stdin when `--content` omitted or `-`         |
| `loam-cli get <id>`  | Fetch one entry; `--no-reinforce` skips access bump                            |
| `loam-cli search`    | FTS5 + filters + ranking weights; emits JSON array of `{entry, rank, snippet}` |
| `loam-cli recent`    | Time-ordered fetch with filters; emits JSON array                              |
| `loam-cli ls`        | Per-agent summary (count, top tags, by type, last accessed)                    |
| `loam-cli supersede` | Write a replacement; inherits unspecified fields from the predecessor          |
| `loam-cli prune`     | Filter-driven cleanup; dry-run by default, `--apply` to commit                 |
| `loam-cli export`    | Emit every entry as JSONL (ordered, no reinforce)                              |
| `loam-cli import`    | Restore from JSONL; preserves id/timestamps. `--replace` overwrites            |

All commands accept `--agent`, `--path`, `--pretty`. Output is single-line JSON by default for `jq`-friendliness.

---

## Entry shape

```json
{
  "schema_version": "0.1",
  "id": "e.r3x2a",
  "agent": "mabus",
  "type": "observation",
  "content": "Dan prefers terse responses…",
  "source": "session:2026-05-22T03:00Z",
  "confidence": 0.95,
  "tags": ["dan", "preference"],
  "created_at": "2026-05-22T03:12:00Z",
  "last_accessed": "2026-05-22T09:18:21Z",
  "access_count": 7,
  "supersedes": null,
  "superseded_by": null,
  "attestations": [],
  "metadata": {}
}
```

`type` is free-form. Common shapes: `observation`, `fact`, `learning`, `event`, `reference`, `compact-snapshot`. The library doesn't enforce a taxonomy.

---

## Production notes

- **Backups.** Don't copy a hot `.db` with stray `.db-wal` / `.db-shm` siblings. Use SQLite's online backup (`sqlite3 src.db ".backup dest.db"`) or quiesce + checkpoint + rsync.
- **Growth.** Expect roughly 1–2 KB per entry plus FTS index overhead. SQLite handles GB-scale stores comfortably. Prune when backups get expensive, not when the file is "big."
- **Concurrent writers.** WAL mode + 5s `busy_timeout` are on by default. Many readers in parallel; writes serialize. Above ~10 concurrent writers expect `SQLITE_BUSY` backoffs — tune the timeout up or shard by tag prefix.
- **Schema upgrades.** Minor bumps (0.1 → 0.2) are additive. Major bumps refuse to open without an explicit migration. The schema version lives in the `_meta` table.

See [`docs/production`](https://memory.mabus.ai/docs/production) for the full operator playbook.

---

## Status & roadmap

**v0.1.0 (current — 2026-05-22)**

- Per-agent SQLite stores with WAL + FTS5
- Tag-AND retrieval via junction table
- Reinforce-on-retrieve + bm25/recency/confidence/access ranking
- Supersede chain with `superseded_by` back-ref + default hide
- `loam-cli` for write/get/search/recent/ls/supersede/prune/export/import
- Compaction-bridge example wired to session-essence's PreCompact hook
- 78 tests, no external dependencies beyond the Python stdlib

**v0.2 (planned)**

- Vector embeddings as an opt-in _additional_ index alongside FTS5 (not a replacement)
- Tribunal attestation wiring — verified / suspect / refuted verdicts on entries
- Search filters on attestation state (`exclude_suspect`, `min_attestations`)

**v0.3 (planned)**

- Cross-agent federation via read-only mounts + per-agent grants
- `Store.search_federation(...)` querying every store the caller has grants for
- Optional crypto-native grant tokens for verifiable cross-agent access

---

## Inspiration / prior art

- **[session-essence](https://github.com/dpdanpittman/session-essence)** — direct lineage; this is the memory counterpart to essence's identity portrait
- **[swarm-lib](https://github.com/dpdanpittman/swarm-lib)** — substrate-layer playbook + standalone-usable ethos
- **[mem0](https://github.com/mem0ai/mem0)** — conceptually adjacent, cloud-first, not identity-scoped
- **ChromaDB / Pinecone / Weaviate** — vector DBs; useful for semantic similarity but wrong shape for tag + time
- **Memory MCP servers** — knowledge-graph-over-protocol; can sit on top of session-loam
- **Maildir + SQLite-backed mail clients** — files are durable; a queryable index lives next to them

---

## License

[GNU AGPLv3 or later](LICENSE). Open-source, copyleft for network use — anyone running this as a service must publish their modifications under the same license. Code released under MIT in the v0.1.0 tagged release stays MIT for that tag; all `main` development from this commit forward is AGPLv3.
