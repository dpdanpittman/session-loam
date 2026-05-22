"""Command-line interface for session-loam.

Exposes the lib's primitives over a subprocess boundary so non-Python
consumers (n8n nodes, bash handlers, scheduled cron jobs) can participate
without importing the Python package.

Subcommands (day 4):

  loam-cli write     — write an entry; emits the persisted JSON on stdout
  loam-cli get       — fetch one entry by id; emits JSON
  loam-cli search    — FTS5 search + filters; emits JSON array of results
  loam-cli recent    — time-ordered fetch + filters; emits JSON array
  loam-cli ls        — per-agent summary (count, top tags, recent writes)

Day 5 adds supersede / prune / export / import.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from session_loam.entry import Entry
from session_loam.search import DEFAULT_WEIGHTS, RankWeights
from session_loam.store import (
    DEFAULT_BASE_DIR,
    ENV_BASE_DIR,
    EntryAlreadyExists,
    EntryNotFound,
    SchemaVersionMismatch,
    Store,
    _resolve_agent,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _open_store(args: argparse.Namespace) -> Store:
    """Open the store from CLI args. ``--agent`` and ``--path`` are universal."""
    return Store.open(agent=args.agent, path=args.path)


def _split_csv(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


def _read_content(raw: str | None) -> str:
    """Return ``raw`` unchanged, or read stdin when raw is ``None`` or ``-``."""
    if raw is None or raw == "-":
        return sys.stdin.read().rstrip("\n")
    return raw


def _emit(obj: object, *, pretty: bool = False) -> None:
    if pretty:
        print(json.dumps(obj, indent=2, sort_keys=False, default=str))
    else:
        print(json.dumps(obj, default=str))


def _entry_dict(entry: Entry) -> dict:
    return asdict(entry)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def cmd_write(args: argparse.Namespace) -> int:
    content = _read_content(args.content)
    if not content:
        print("error: --content was empty (and no stdin)", file=sys.stderr)
        return 2

    tags = _split_csv(args.tags) or []
    metadata = json.loads(args.metadata) if args.metadata else {}

    entry = Entry(
        type=args.type,
        content=content,
        id=args.id,
        source=args.source,
        confidence=args.confidence,
        tags=tags,
        supersedes=args.supersedes,
        metadata=metadata,
    )

    with _open_store(args) as store:
        written = store.write(entry)

    _emit(_entry_dict(written), pretty=args.pretty)
    return 0


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def cmd_get(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        if args.no_reinforce:
            # Reach below the public API to avoid bumping access_count.
            row = store._conn.execute(
                "SELECT * FROM entry WHERE id = ?", (args.id,)
            ).fetchone()
            if row is None:
                print(f"error: entry not found: {args.id}", file=sys.stderr)
                return 1
            from session_loam.store import _row_to_entry
            entry = _row_to_entry(row)
        else:
            try:
                entry = store.get(args.id)
            except EntryNotFound:
                print(f"error: entry not found: {args.id}", file=sys.stderr)
                return 1

    _emit(_entry_dict(entry), pretty=args.pretty)
    return 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> int:
    weights = RankWeights(
        recency_half_life_days=args.recency_half_life,
        recency_strength=args.recency_strength,
        confidence_strength=args.confidence_strength,
        access_count_strength=args.access_strength,
    ) if _ranking_overridden(args) else DEFAULT_WEIGHTS

    with _open_store(args) as store:
        results = store.search(
            query=args.query,
            tags=_split_csv(args.tags),
            type=args.type,
            since=args.since,
            until=args.until,
            include_superseded=args.include_superseded,
            limit=args.limit,
            weights=weights,
            reinforce=not args.no_reinforce,
        )

    payload = [
        {
            "entry": _entry_dict(r.entry),
            "rank": r.rank,
            "snippet": r.snippet,
        }
        for r in results
    ]
    _emit(payload, pretty=args.pretty)
    return 0


def _ranking_overridden(args: argparse.Namespace) -> bool:
    return (
        args.recency_half_life != DEFAULT_WEIGHTS.recency_half_life_days
        or args.recency_strength != DEFAULT_WEIGHTS.recency_strength
        or args.confidence_strength != DEFAULT_WEIGHTS.confidence_strength
        or args.access_strength != DEFAULT_WEIGHTS.access_count_strength
    )


# ---------------------------------------------------------------------------
# recent
# ---------------------------------------------------------------------------

def cmd_recent(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        entries = store.recent(
            type=args.type,
            tags=_split_csv(args.tags),
            since=args.since,
            until=args.until,
            include_superseded=args.include_superseded,
            limit=args.limit,
            reinforce=not args.no_reinforce,
        )
    _emit([_entry_dict(e) for e in entries], pretty=args.pretty)
    return 0


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------

def cmd_ls(args: argparse.Namespace) -> int:
    base_dir = _resolve_base_dir(args.path)

    if args.agent:
        summaries = [_summarize_store(args.agent, base_dir / f"{_resolve_agent(args.agent)}.db")]
    else:
        if not base_dir.exists():
            if args.json:
                _emit([], pretty=args.pretty)
            else:
                print(f"no store directory at {base_dir}", file=sys.stderr)
            return 0
        dbs = sorted(p for p in base_dir.iterdir() if p.suffix == ".db")
        if not dbs:
            if args.json:
                _emit([], pretty=args.pretty)
            else:
                print(f"no stores under {base_dir}")
            return 0
        summaries = [_summarize_store(p.stem, p) for p in dbs]

    if args.json:
        _emit(summaries, pretty=args.pretty)
        return 0

    print(_render_ls(summaries))
    return 0


def _resolve_base_dir(path_override: str | None) -> Path:
    if path_override:
        # ``--path`` for ls means base dir (for write/get/search it means a specific db)
        return Path(path_override)
    env = os.environ.get(ENV_BASE_DIR)
    return Path(env) if env else DEFAULT_BASE_DIR


def _summarize_store(agent_hint: str, db_path: Path) -> dict:
    if not db_path.exists():
        return {
            "agent": _resolve_agent(agent_hint),
            "path": str(db_path),
            "exists": False,
        }

    size_bytes = db_path.stat().st_size

    with Store.open(agent=agent_hint, path=db_path) as store:
        conn = store._conn
        total = conn.execute("SELECT COUNT(*) FROM entry").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM entry WHERE superseded_by IS NULL"
        ).fetchone()[0]
        last_written_row = conn.execute(
            "SELECT MAX(created_at) FROM entry"
        ).fetchone()
        last_accessed_row = conn.execute(
            "SELECT MAX(last_accessed) FROM entry"
        ).fetchone()
        schema_row = conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()

        by_type = [
            {"type": r[0], "count": r[1]}
            for r in conn.execute(
                """
                SELECT type, COUNT(*) AS c FROM entry
                 WHERE superseded_by IS NULL
                 GROUP BY type
                 ORDER BY c DESC
                """
            )
        ]
        top_tags = [
            {"tag": r[0], "count": r[1]}
            for r in conn.execute(
                """
                SELECT tag, COUNT(*) AS c FROM entry_tag
                 GROUP BY tag
                 ORDER BY c DESC, tag ASC
                 LIMIT 10
                """
            )
        ]

    return {
        "agent": _resolve_agent(agent_hint),
        "path": str(db_path),
        "exists": True,
        "size_bytes": size_bytes,
        "schema_version": schema_row[0] if schema_row else None,
        "entries_total": total,
        "entries_active": active,
        "last_written_at": last_written_row[0] if last_written_row else None,
        "last_accessed_at": last_accessed_row[0] if last_accessed_row else None,
        "by_type": by_type,
        "top_tags": top_tags,
    }


def _render_ls(summaries: list[dict]) -> str:
    parts: list[str] = []
    for s in summaries:
        if not s.get("exists"):
            parts.append(f"agent: {s['agent']}\n  path: {s['path']}\n  (no store yet)")
            continue
        size_kib = (s["size_bytes"] or 0) / 1024.0
        types_str = ", ".join(f"{t['type']}={t['count']}" for t in s["by_type"]) or "(none)"
        tags_str = ", ".join(f"{t['tag']}={t['count']}" for t in s["top_tags"]) or "(none)"
        parts.append(
            f"agent: {s['agent']}  schema_version={s['schema_version']}\n"
            f"  path:             {s['path']}\n"
            f"  size:             {size_kib:.1f} KiB\n"
            f"  entries:          {s['entries_active']} active / {s['entries_total']} total\n"
            f"  last written:     {s['last_written_at'] or '-'}\n"
            f"  last accessed:    {s['last_accessed_at'] or '-'}\n"
            f"  by type:          {types_str}\n"
            f"  top tags:         {tags_str}"
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# supersede
# ---------------------------------------------------------------------------

def cmd_supersede(args: argparse.Namespace) -> int:
    content = _read_content(args.content)
    if not content:
        print("error: --content was empty (and no stdin)", file=sys.stderr)
        return 2

    with _open_store(args) as store:
        try:
            predecessor = store.get(args.from_id) if not args.no_reinforce_predecessor else None
            if predecessor is None:
                # Read without reinforce
                row = store._conn.execute(
                    "SELECT * FROM entry WHERE id = ?", (args.from_id,)
                ).fetchone()
                if row is None:
                    raise EntryNotFound(args.from_id)
                from session_loam.store import _row_to_entry
                predecessor = _row_to_entry(row)
        except EntryNotFound:
            print(f"error: predecessor not found: {args.from_id}", file=sys.stderr)
            return 1

        type_ = args.type if args.type is not None else predecessor.type
        tags = _split_csv(args.tags) if args.tags is not None else list(predecessor.tags)
        source = args.source if args.source is not None else predecessor.source
        confidence = args.confidence if args.confidence is not None else predecessor.confidence

        entry = Entry(
            type=type_,
            content=content,
            tags=tags or [],
            source=source,
            confidence=confidence,
            supersedes=predecessor.id,
            metadata=json.loads(args.metadata) if args.metadata else {},
        )
        written = store.write(entry)

    _emit(_entry_dict(written), pretty=args.pretty)
    return 0


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

def cmd_prune(args: argparse.Namespace) -> int:
    tags = _split_csv(args.tags)
    try:
        with _open_store(args) as store:
            ids = store.prune(
                type=args.type,
                tags=tags,
                older_than=args.older_than,
                access_count_below=args.access_count_below,
                only_superseded=args.only_superseded,
                dry_run=not args.apply,
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "dry_run": not args.apply,
        "count": len(ids),
        "ids": ids,
    }
    _emit(payload, pretty=args.pretty)
    return 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        for entry in store.iter_export():
            # JSONL: one JSON object per line. Compact form (no indent).
            print(json.dumps(_entry_dict(entry), default=str))
    return 0


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def cmd_import(args: argparse.Namespace) -> int:
    if args.file and args.file != "-":
        src = Path(args.file).read_text()
    else:
        src = sys.stdin.read()

    count_added = 0
    count_replaced = 0
    count_skipped = 0
    errors: list[dict] = []

    with _open_store(args) as store:
        for lineno, raw in enumerate(src.splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append({"line": lineno, "error": f"invalid JSON: {exc}"})
                continue
            try:
                entry = _entry_from_dict(obj)
                store.import_entry(entry, replace=args.replace)
                if args.replace:
                    count_replaced += 1
                else:
                    count_added += 1
            except EntryAlreadyExists:
                count_skipped += 1
            except (ValueError, TypeError) as exc:
                errors.append({"line": lineno, "id": obj.get("id"), "error": str(exc)})

    payload = {
        "added": count_added,
        "replaced": count_replaced,
        "skipped_existing": count_skipped,
        "errors": errors,
    }
    _emit(payload, pretty=args.pretty)
    return 0 if not errors else 4


def _entry_from_dict(obj: dict) -> Entry:
    """Reverse of asdict(Entry). Tolerant of missing keys with sensible defaults."""
    return Entry(
        type=obj["type"],
        content=obj["content"],
        id=obj.get("id"),
        agent=obj.get("agent"),
        source=obj.get("source"),
        confidence=obj.get("confidence"),
        tags=list(obj.get("tags") or []),
        created_at=obj.get("created_at"),
        last_accessed=obj.get("last_accessed"),
        access_count=int(obj.get("access_count") or 0),
        supersedes=obj.get("supersedes"),
        superseded_by=obj.get("superseded_by"),
        attestations=list(obj.get("attestations") or []),
        metadata=dict(obj.get("metadata") or {}),
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _add_global_args(p: argparse.ArgumentParser, *, path_help: str = "Override default DB path") -> None:
    p.add_argument("--agent", help="Override agent identity (default: $LOAM_AGENT or hostname)")
    p.add_argument("--path", help=path_help)
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loam-cli",
        description="Command-line interface for session-loam's per-agent memory store.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # write
    p = sub.add_parser("write", help="write an entry; emits JSON of the persisted record")
    p.add_argument("--type", required=True, help="Entry type, e.g. observation, fact, learning")
    p.add_argument("--content", help="Entry body. Use '-' or omit to read from stdin.")
    p.add_argument("--tags", help="Comma-separated tag list")
    p.add_argument("--source")
    p.add_argument("--confidence", type=float, help="0.0–1.0 caller-defined")
    p.add_argument("--id", help="Override auto-generated id")
    p.add_argument("--supersedes", help="ID of an entry this one replaces")
    p.add_argument("--metadata", help="JSON object for consumer-specific fields")
    _add_global_args(p)
    p.set_defaults(func=cmd_write)

    # get
    p = sub.add_parser("get", help="fetch one entry by id")
    p.add_argument("id", help="Entry id")
    p.add_argument("--no-reinforce", action="store_true", help="Read without bumping access_count")
    _add_global_args(p)
    p.set_defaults(func=cmd_get)

    # search
    p = sub.add_parser("search", help="FTS5 search with tag/type/time filters")
    p.add_argument("--query", required=True, help="FTS5 MATCH expression")
    p.add_argument("--tags", help="Comma-separated tag filter (AND)")
    p.add_argument("--type", help="Filter by entry type")
    p.add_argument("--since", help="ISO-8601 lower bound on created_at (inclusive)")
    p.add_argument("--until", help="ISO-8601 upper bound on created_at (inclusive)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--include-superseded", action="store_true")
    p.add_argument("--no-reinforce", action="store_true")
    p.add_argument("--recency-half-life", type=float, default=DEFAULT_WEIGHTS.recency_half_life_days)
    p.add_argument("--recency-strength", type=float, default=DEFAULT_WEIGHTS.recency_strength)
    p.add_argument("--confidence-strength", type=float, default=DEFAULT_WEIGHTS.confidence_strength)
    p.add_argument("--access-strength", type=float, default=DEFAULT_WEIGHTS.access_count_strength)
    _add_global_args(p)
    p.set_defaults(func=cmd_search)

    # recent
    p = sub.add_parser("recent", help="time-ordered fetch (newest first) with filters")
    p.add_argument("--type")
    p.add_argument("--tags")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--include-superseded", action="store_true")
    p.add_argument("--no-reinforce", action="store_true")
    _add_global_args(p)
    p.set_defaults(func=cmd_recent)

    # ls
    p = sub.add_parser("ls", help="human-readable summary of stores under the base dir")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    _add_global_args(p, path_help="Override base directory (default: $LOAM_BASE_DIR or ~/.session-loam/)")
    p.set_defaults(func=cmd_ls)

    # supersede
    p = sub.add_parser(
        "supersede",
        help="write a replacement entry that supersedes an existing one (audit-preserving edit)",
    )
    p.add_argument("--from-id", required=True, help="ID of the predecessor entry")
    p.add_argument("--content", help="New body. Use '-' or omit to read stdin.")
    p.add_argument("--type", help="Override type (default: inherit from predecessor)")
    p.add_argument("--tags", help="Override tag list (default: inherit). Comma-separated.")
    p.add_argument("--source", help="Override source (default: inherit)")
    p.add_argument("--confidence", type=float, help="Override confidence (default: inherit)")
    p.add_argument("--metadata", help="JSON object for the new entry's metadata")
    p.add_argument(
        "--no-reinforce-predecessor",
        action="store_true",
        help="Don't bump access_count on the predecessor when reading it",
    )
    _add_global_args(p)
    p.set_defaults(func=cmd_supersede)

    # prune
    p = sub.add_parser(
        "prune",
        help="delete entries matching a filter (dry-run by default; pass --apply to commit)",
    )
    p.add_argument("--type", help="Match entries with this type")
    p.add_argument("--tags", help="Match entries carrying all these tags (CSV)")
    p.add_argument("--older-than", help="ISO-8601: match entries with created_at < this")
    p.add_argument("--access-count-below", type=int, help="Match entries with access_count < N")
    p.add_argument("--only-superseded", action="store_true", help="Only delete entries that have been superseded")
    p.add_argument("--apply", action="store_true", help="Actually delete (without this, dry-run)")
    _add_global_args(p)
    p.set_defaults(func=cmd_prune)

    # export
    p = sub.add_parser("export", help="dump every entry as JSONL on stdout (ordered by created_at)")
    _add_global_args(p)
    p.set_defaults(func=cmd_export)

    # import
    p = sub.add_parser("import", help="read JSONL on stdin (or --file), restore entries preserving id/timestamps")
    p.add_argument("--file", help="JSONL file path. Default: stdin.")
    p.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite existing entries by id. Default: skip duplicates.",
    )
    _add_global_args(p)
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SchemaVersionMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
