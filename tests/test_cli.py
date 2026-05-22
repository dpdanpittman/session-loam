"""Tests for the loam-cli subprocess surface — day 4 deliverable.

Invokes the CLI as ``python -m session_loam.cli`` so the entry-point install
isn't a prerequisite. Each test uses a tmp_path-rooted $LOAM_BASE_DIR.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(
    *args: str,
    tmp_path: Path,
    stdin: str | None = None,
    agent: str = "clitest",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LOAM_BASE_DIR"] = str(tmp_path)
    env["LOAM_AGENT"] = agent
    # Make sure the in-tree package is importable
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


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def test_write_round_trip(tmp_base: Path) -> None:
    proc = run_cli(
        "write",
        "--type", "observation",
        "--content", "CLI round-trip test",
        "--tags", "test,cli",
        "--confidence", "0.8",
        tmp_path=tmp_base,
    )
    assert proc.returncode == 0, proc.stderr
    written = json.loads(proc.stdout)
    assert written["type"] == "observation"
    assert written["content"] == "CLI round-trip test"
    assert sorted(written["tags"]) == ["cli", "test"]
    assert written["confidence"] == pytest.approx(0.8)
    assert written["agent"] == "clitest"
    assert written["id"].startswith("e.")

    # File landed where we expect
    assert (tmp_base / "clitest.db").exists()


def test_write_reads_content_from_stdin(tmp_base: Path) -> None:
    proc = run_cli(
        "write",
        "--type", "fact",
        # no --content -> reads stdin
        tmp_path=tmp_base,
        stdin="content from pipe",
    )
    assert proc.returncode == 0, proc.stderr
    written = json.loads(proc.stdout)
    assert written["content"] == "content from pipe"


def test_write_with_dash_reads_stdin(tmp_base: Path) -> None:
    proc = run_cli(
        "write",
        "--type", "fact",
        "--content", "-",
        tmp_path=tmp_base,
        stdin="explicit dash stdin",
    )
    assert proc.returncode == 0, proc.stderr
    written = json.loads(proc.stdout)
    assert written["content"] == "explicit dash stdin"


def test_write_metadata_round_trip(tmp_base: Path) -> None:
    proc = run_cli(
        "write",
        "--type", "fact",
        "--content", "with metadata",
        "--metadata", '{"k": "v", "n": 7}',
        tmp_path=tmp_base,
    )
    assert proc.returncode == 0, proc.stderr
    written = json.loads(proc.stdout)
    assert written["metadata"] == {"k": "v", "n": 7}


def test_write_supersedes(tmp_base: Path) -> None:
    a = json.loads(
        run_cli(
            "write", "--type", "learning", "--content", "v1", tmp_path=tmp_base
        ).stdout
    )
    b = json.loads(
        run_cli(
            "write", "--type", "learning", "--content", "v2",
            "--supersedes", a["id"],
            tmp_path=tmp_base,
        ).stdout
    )
    # Verify back-ref via get on the original
    got = json.loads(
        run_cli("get", a["id"], tmp_path=tmp_base).stdout
    )
    assert got["superseded_by"] == b["id"]


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_round_trip(tmp_base: Path) -> None:
    written = json.loads(
        run_cli(
            "write", "--type", "fact", "--content", "fetch me",
            tmp_path=tmp_base,
        ).stdout
    )
    proc = run_cli("get", written["id"], tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    fetched = json.loads(proc.stdout)
    assert fetched["id"] == written["id"]
    assert fetched["content"] == "fetch me"
    # First get bumps access_count to 1
    assert fetched["access_count"] == 1


def test_get_no_reinforce(tmp_base: Path) -> None:
    written = json.loads(
        run_cli(
            "write", "--type", "fact", "--content", "no bump",
            tmp_path=tmp_base,
        ).stdout
    )
    proc = run_cli("get", written["id"], "--no-reinforce", tmp_path=tmp_base)
    fetched = json.loads(proc.stdout)
    assert fetched["access_count"] == 0


def test_get_missing_id_exits_nonzero(tmp_base: Path) -> None:
    proc = run_cli("get", "e.nosuchid0", tmp_path=tmp_base)
    assert proc.returncode == 1
    assert "not found" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def _seed_cli(tmp_base: Path) -> dict[str, dict]:
    written: dict[str, dict] = {}
    fixtures = [
        ("observation", "filesystem-as-orchestrator pattern", "pattern,swarm"),
        ("observation", "atomic rename is the orchestrator primitive", "primitive,swarm"),
        ("fact", "ChromaDB is a vector database", "external"),
        ("learning", "compaction can lose mid-flight work", "compaction,lesson"),
    ]
    for typ, content, tags in fixtures:
        proc = run_cli(
            "write", "--type", typ, "--content", content, "--tags", tags,
            tmp_path=tmp_base,
        )
        assert proc.returncode == 0, proc.stderr
        e = json.loads(proc.stdout)
        written[content] = e
    return written


def test_search_returns_array(tmp_base: Path) -> None:
    _seed_cli(tmp_base)
    proc = run_cli("search", "--query", "orchestrator", tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    assert isinstance(results, list)
    assert len(results) == 2
    assert all("entry" in r and "rank" in r and "snippet" in r for r in results)
    contents = {r["entry"]["content"] for r in results}
    assert "filesystem-as-orchestrator pattern" in contents


def test_search_empty_returns_empty_array(tmp_base: Path) -> None:
    _seed_cli(tmp_base)
    proc = run_cli("search", "--query", "zztopxylo", tmp_path=tmp_base)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == []


def test_search_tag_and_type_filter(tmp_base: Path) -> None:
    _seed_cli(tmp_base)
    proc = run_cli(
        "search", "--query", "orchestrator",
        "--tags", "swarm,primitive",
        tmp_path=tmp_base,
    )
    results = json.loads(proc.stdout)
    assert len(results) == 1
    assert "atomic rename" in results[0]["entry"]["content"]


# ---------------------------------------------------------------------------
# recent
# ---------------------------------------------------------------------------

def test_recent_returns_newest_first(tmp_base: Path) -> None:
    import time

    a = json.loads(run_cli("write", "--type", "event", "--content", "first", tmp_path=tmp_base).stdout)
    time.sleep(1.1)
    b = json.loads(run_cli("write", "--type", "event", "--content", "second", tmp_path=tmp_base).stdout)
    time.sleep(1.1)
    c = json.loads(run_cli("write", "--type", "event", "--content", "third", tmp_path=tmp_base).stdout)

    proc = run_cli("recent", "--type", "event", tmp_path=tmp_base)
    assert proc.returncode == 0
    items = json.loads(proc.stdout)
    assert [e["id"] for e in items] == [c["id"], b["id"], a["id"]]


def test_recent_limit(tmp_base: Path) -> None:
    for i in range(5):
        run_cli("write", "--type", "event", "--content", f"e{i}", tmp_path=tmp_base)
    proc = run_cli("recent", "--type", "event", "--limit", "2", tmp_path=tmp_base)
    items = json.loads(proc.stdout)
    assert len(items) == 2


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------

def test_ls_json_specific_agent(tmp_base: Path) -> None:
    _seed_cli(tmp_base)
    proc = run_cli("ls", "--json", "--agent", "clitest", tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    items = json.loads(proc.stdout)
    assert len(items) == 1
    s = items[0]
    assert s["agent"] == "clitest"
    assert s["entries_total"] == 4
    assert s["entries_active"] == 4
    assert s["schema_version"] == "0.1"
    type_map = {t["type"]: t["count"] for t in s["by_type"]}
    assert type_map == {"observation": 2, "fact": 1, "learning": 1}
    tag_map = {t["tag"]: t["count"] for t in s["top_tags"]}
    assert tag_map["swarm"] == 2


def test_ls_json_all_stores(tmp_base: Path) -> None:
    # Seed two distinct agents
    run_cli("write", "--type", "fact", "--content", "from a",
            tmp_path=tmp_base, agent="agenta")
    run_cli("write", "--type", "fact", "--content", "from b",
            tmp_path=tmp_base, agent="agentb")

    proc = run_cli("ls", "--json", tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    items = json.loads(proc.stdout)
    agents = {s["agent"] for s in items}
    assert agents == {"agenta", "agentb"}


def test_ls_human_format(tmp_base: Path) -> None:
    _seed_cli(tmp_base)
    proc = run_cli("ls", "--agent", "clitest", tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "agent: clitest" in out
    assert "schema_version=0.1" in out
    assert "entries:" in out
    assert "by type:" in out
    assert "top tags:" in out


def test_ls_empty_base_dir_json(tmp_base: Path) -> None:
    # tmp_base is empty -> no .db files
    proc = run_cli("ls", "--json", tmp_path=tmp_base)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


# ---------------------------------------------------------------------------
# global options
# ---------------------------------------------------------------------------

def test_write_with_explicit_agent_override(tmp_base: Path) -> None:
    proc = run_cli(
        "write", "--type", "fact", "--content", "alt agent",
        "--agent", "ALT",
        tmp_path=tmp_base,
    )
    assert proc.returncode == 0, proc.stderr
    written = json.loads(proc.stdout)
    assert written["agent"] == "alt"
    assert (tmp_base / "alt.db").exists()


def test_pretty_json(tmp_base: Path) -> None:
    proc = run_cli(
        "write", "--type", "fact", "--content", "pretty",
        "--pretty",
        tmp_path=tmp_base,
    )
    assert proc.returncode == 0
    # Pretty output has indentation (newlines)
    assert "\n" in proc.stdout.rstrip()
