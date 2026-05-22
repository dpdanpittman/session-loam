"""CLI tests for day-5 subcommands: supersede, prune, export, import."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def run_cli(
    *args: str,
    tmp_path: Path,
    stdin: str | None = None,
    agent: str = "d5cli",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LOAM_BASE_DIR"] = str(tmp_path)
    env["LOAM_AGENT"] = agent
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    return subprocess.run(
        [sys.executable, "-m", "session_loam.cli", *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
    )


@pytest.fixture
def tmp_base(tmp_path: Path) -> Path:
    return tmp_path


def _write(tmp_base: Path, **kwargs) -> dict:
    args = ["write"]
    for k, v in kwargs.items():
        args.extend([f"--{k.replace('_', '-')}", str(v)])
    proc = run_cli(*args, tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# supersede
# ---------------------------------------------------------------------------

def test_supersede_inherits_predecessor_fields(tmp_base: Path) -> None:
    a = _write(
        tmp_base,
        type="observation",
        content="v1",
        tags="alpha,beta",
        confidence=0.7,
        source="initial",
    )
    proc = run_cli(
        "supersede",
        "--from-id", a["id"],
        "--content", "v2",
        tmp_path=tmp_base,
    )
    assert proc.returncode == 0, proc.stderr
    b = json.loads(proc.stdout)
    assert b["supersedes"] == a["id"]
    assert b["type"] == "observation"  # inherited
    assert sorted(b["tags"]) == ["alpha", "beta"]
    assert b["confidence"] == pytest.approx(0.7)
    assert b["source"] == "initial"

    # Predecessor now has superseded_by back-ref
    got = json.loads(run_cli("get", a["id"], tmp_path=tmp_base).stdout)
    assert got["superseded_by"] == b["id"]


def test_supersede_overrides_take_effect(tmp_base: Path) -> None:
    a = _write(tmp_base, type="observation", content="v1", tags="alpha")
    proc = run_cli(
        "supersede",
        "--from-id", a["id"],
        "--content", "v2",
        "--type", "learning",
        "--tags", "gamma,delta",
        "--confidence", "0.99",
        tmp_path=tmp_base,
    )
    b = json.loads(proc.stdout)
    assert b["type"] == "learning"
    assert sorted(b["tags"]) == ["delta", "gamma"]
    assert b["confidence"] == pytest.approx(0.99)


def test_supersede_missing_predecessor(tmp_base: Path) -> None:
    proc = run_cli(
        "supersede",
        "--from-id", "e.nosuch01",
        "--content", "x",
        tmp_path=tmp_base,
    )
    assert proc.returncode == 1
    assert "predecessor not found" in proc.stderr.lower()


def test_supersede_content_from_stdin(tmp_base: Path) -> None:
    a = _write(tmp_base, type="fact", content="v1")
    proc = run_cli(
        "supersede",
        "--from-id", a["id"],
        tmp_path=tmp_base,
        stdin="v2 from pipe",
    )
    assert proc.returncode == 0, proc.stderr
    b = json.loads(proc.stdout)
    assert b["content"] == "v2 from pipe"
    assert b["supersedes"] == a["id"]


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

def test_prune_dry_run_reports_without_deleting(tmp_base: Path) -> None:
    a = _write(tmp_base, type="event", content="kill", tags="x")
    b = _write(tmp_base, type="event", content="keep", tags="y")

    proc = run_cli("prune", "--tags", "x", tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["dry_run"] is True
    assert payload["count"] == 1
    assert payload["ids"] == [a["id"]]

    # Both still present
    assert json.loads(run_cli("get", a["id"], tmp_path=tmp_base).stdout)["id"] == a["id"]
    assert json.loads(run_cli("get", b["id"], tmp_path=tmp_base).stdout)["id"] == b["id"]


def test_prune_apply_actually_deletes(tmp_base: Path) -> None:
    a = _write(tmp_base, type="event", content="kill", tags="x")
    b = _write(tmp_base, type="event", content="keep", tags="y")

    proc = run_cli("prune", "--tags", "x", "--apply", tmp_path=tmp_base)
    payload = json.loads(proc.stdout)
    assert payload["dry_run"] is False
    assert payload["count"] == 1

    # Verify
    miss = run_cli("get", a["id"], tmp_path=tmp_base)
    assert miss.returncode == 1
    hit = run_cli("get", b["id"], tmp_path=tmp_base)
    assert hit.returncode == 0


def test_prune_refuses_no_filter(tmp_base: Path) -> None:
    _write(tmp_base, type="fact", content="anything")
    proc = run_cli("prune", "--apply", tmp_path=tmp_base)
    assert proc.returncode == 2
    assert "filter" in proc.stderr.lower()


def test_prune_older_than(tmp_base: Path) -> None:
    from datetime import datetime, timezone

    a = _write(tmp_base, type="event", content="old")
    time.sleep(1.1)
    cut = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    time.sleep(1.1)
    b = _write(tmp_base, type="event", content="new")

    proc = run_cli("prune", "--older-than", cut, "--apply", tmp_path=tmp_base)
    payload = json.loads(proc.stdout)
    assert payload["ids"] == [a["id"]]

    # b survives
    assert run_cli("get", b["id"], tmp_path=tmp_base).returncode == 0


def test_prune_only_superseded(tmp_base: Path) -> None:
    a = _write(tmp_base, type="learning", content="v1")
    run_cli(
        "supersede", "--from-id", a["id"], "--content", "v2", tmp_path=tmp_base
    )
    standalone = _write(tmp_base, type="learning", content="standalone")

    proc = run_cli("prune", "--only-superseded", "--apply", tmp_path=tmp_base)
    payload = json.loads(proc.stdout)
    assert payload["ids"] == [a["id"]]
    assert run_cli("get", standalone["id"], tmp_path=tmp_base).returncode == 0


# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------

def test_export_jsonl_round_trip(tmp_path: Path) -> None:
    """Write -> export -> import into a fresh agent -> entries match."""
    src_base = tmp_path / "src"
    src_base.mkdir()
    dst_base = tmp_path / "dst"
    dst_base.mkdir()

    # Seed src
    for i in range(3):
        run_cli(
            "write",
            "--type", "fact",
            "--content", f"entry {i}",
            "--tags", f"i{i},shared",
            tmp_path=src_base,
            agent="src",
        )
        time.sleep(1.1)  # ensure ordering by created_at

    # Export
    export_proc = run_cli("export", tmp_path=src_base, agent="src")
    assert export_proc.returncode == 0
    jsonl = export_proc.stdout
    lines = [l for l in jsonl.splitlines() if l.strip()]
    assert len(lines) == 3
    # Parseable JSONL
    src_entries = [json.loads(l) for l in lines]
    assert [e["content"] for e in src_entries] == ["entry 0", "entry 1", "entry 2"]

    # Import into dst
    import_proc = run_cli(
        "import",
        tmp_path=dst_base,
        agent="dst",
        stdin=jsonl,
    )
    assert import_proc.returncode == 0, import_proc.stderr
    report = json.loads(import_proc.stdout)
    assert report["added"] == 3
    assert report["skipped_existing"] == 0
    assert report["replaced"] == 0
    assert report["errors"] == []

    # Round-trip via export from dst
    re_export = run_cli("export", tmp_path=dst_base, agent="dst")
    re_lines = [l for l in re_export.stdout.splitlines() if l.strip()]
    re_entries = [json.loads(l) for l in re_lines]

    # IDs and timestamps preserved
    assert [e["id"] for e in src_entries] == [e["id"] for e in re_entries]
    assert [e["created_at"] for e in src_entries] == [e["created_at"] for e in re_entries]


def test_import_skips_duplicates_without_replace(tmp_base: Path) -> None:
    written = _write(tmp_base, type="fact", content="duplicate test")

    # Export then re-import — every line is a duplicate
    export = run_cli("export", tmp_path=tmp_base).stdout

    proc = run_cli("import", tmp_path=tmp_base, stdin=export)
    payload = json.loads(proc.stdout)
    assert payload["added"] == 0
    assert payload["skipped_existing"] == 1


def test_import_replace_overwrites(tmp_base: Path) -> None:
    written = _write(tmp_base, type="fact", content="original")
    # Hand-mutate the export
    record = json.loads(run_cli("export", tmp_path=tmp_base).stdout)
    record["content"] = "overwrite"
    jsonl = json.dumps(record)

    proc = run_cli("import", "--replace", tmp_path=tmp_base, stdin=jsonl)
    payload = json.loads(proc.stdout)
    assert payload["replaced"] == 1
    assert payload["skipped_existing"] == 0

    # And the content is updated
    got = json.loads(run_cli("get", written["id"], tmp_path=tmp_base).stdout)
    assert got["content"] == "overwrite"


def test_import_invalid_line_reported_in_errors(tmp_base: Path) -> None:
    bad_jsonl = "{not valid json\n"
    proc = run_cli("import", tmp_path=tmp_base, stdin=bad_jsonl)
    assert proc.returncode == 4
    payload = json.loads(proc.stdout)
    assert payload["added"] == 0
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["line"] == 1


def test_import_from_file(tmp_base: Path) -> None:
    _write(tmp_base, type="fact", content="for file import", agent="src")
    src_db_dir = tmp_base
    export = run_cli("export", tmp_path=src_db_dir, agent="src").stdout

    file_path = tmp_base / "snapshot.jsonl"
    file_path.write_text(export)

    proc = run_cli(
        "import", "--file", str(file_path),
        tmp_path=tmp_base, agent="dst",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["added"] == 1
