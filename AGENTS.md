# AGENTS.md

Instructions for AI coding agents working in this repository.

## What this is

session-loam is a small Python library and CLI that gives an agent durable memory across sessions. Each agent identity gets one SQLite file (default `~/.session-loam/<agent>.db`) with FTS5 full-text search, tag and time filters, and reinforce-on-retrieve ranking. Edits are supersedes, not overwrites. There are no runtime dependencies beyond the Python standard library. Status: alpha, version 0.1.0, released 2026-05-22 (see "Status & roadmap" in `README.md`). v0.2 and v0.3 are planned but not started in the repo. It is public and licensed AGPL-3.0-or-later.

## Layout

- `session_loam/store.py`: the `Store` class, SQLite schema, WAL setup, path resolution (`LOAM_AGENT`, `LOAM_BASE_DIR`).
- `session_loam/entry.py`: the `Entry` data shape.
- `session_loam/search.py`: FTS5 search and the ranking weights.
- `session_loam/cli.py`: the `loam-cli` entry point (write, get, search, recent, ls, supersede, prune, export, import).
- `tests/`: pytest suite. Tests use temporary directories, not the real `~/.session-loam`.
- `examples/compaction-bridge/`: Claude Code PreCompact and SessionStart hook scripts plus a settings snippet.
- `DESIGN.md`: the design spec.
- `site/`: the Astro 4 + Tailwind documentation site, plus a Kubernetes manifest in `site/k8s/`.

## Commands

Python 3.11 or newer. Use a virtual environment inside the clone (`.venv/` is gitignored).

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                       # whole suite, about 25 seconds
.venv/bin/loam-cli --help
bash -n examples/compaction-bridge/*.sh   # syntax check for the example hooks
```

There is no linter or type checker configured in the repo. Site commands (from `site/package.json`, not verified here):

```bash
cd site && npm install && npm run dev     # local dev server
cd site && npm run build                  # output in site/dist/
```

When you try the CLI by hand, set `LOAM_BASE_DIR` to a temporary folder so you do not write into a real memory store.

## Deployment

The library is not published to PyPI. It is installed from the clone with `pip install -e .`.

The site is built and served from the owner's homelab using `site/k8s/memory-website.yaml`. The owner runs that. An agent does not build, deploy or apply manifests. A change to a running system is a reviewed script the owner runs.

## Rules for agents

- Ask before large changes. Explain the problem and the proposed fix first. Keep diffs small and surgical.
- Never commit secrets (keys, tokens, passwords, `.env` files). Secrets live in a password manager and are read at run time.
- Never delete this repository or rewrite published history.
- Do not change deployment, CI or infrastructure files (`site/k8s/`, `site/astro.config.mjs`) unless that is the task.
- Run `pytest` before saying a change is done. Say plainly what was not run.
- Commit messages: short, imperative, say what changed.
- Keep the library dependency-free (`dependencies = []`). Ask before adding any runtime dependency. Vector embeddings are planned as an opt-in extra, not a core dependency.
- The never-delete rule is part of the design: edits go through supersede, and the old row stays. Only `prune` deletes, and it is dry-run unless `--apply` is passed. Keep that default.
- Schema changes: minor versions must be additive, and the version lives in the `_meta` table. A breaking schema change needs an explicit migration. Ask first.
- Never touch a real store under `~/.session-loam/` while testing. Use `LOAM_BASE_DIR` or pytest's `tmp_path`.
- If you change the CLI or the entry shape, update `README.md`, `DESIGN.md` and the site docs pages together. The README quotes a test count ("78 tests"); update it if that changes.

## Must not be committed or made public

This repository is public.

- No secrets, tokens or `.env` files.
- No SQLite stores (`*.db`, `*.db-wal`, `*.db-shm`). They hold real memories. These are gitignored; keep it that way.
- No personal details about the owner beyond what the repo already shows. Do not add real memory entries as examples.
- No internal hostnames, IP addresses, tailnet names, machine usernames or paths under `/home` in new content. Describe infrastructure generically ("the homelab cluster", "the reverse proxy").
- No build output (`site/dist/`, `*.egg-info/`, `.venv/`, `node_modules/`).
