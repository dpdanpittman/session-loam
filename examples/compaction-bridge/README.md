# Compaction bridge

A worked example of session-loam composed with [session-essence](https://github.com/dpdanpittman/session-essence)'s lifecycle hooks. Before Claude Code compacts the session, a `PreCompact` hook synthesizes a one-paragraph summary via a local model and writes it as a `compact-snapshot` entry. The next session's `SessionStart` hook reads the most recent bridge entry and surfaces it as initial context.

## What this solves

The chat window is volatile. When auto-compaction fires, ad-hoc context disappears. The "where was I" thread evaporates.

The bridge pattern keeps the substrate alive across the cliff:

```
session N …
  ↓ PreCompact fires
  ↓ local model synthesizes "what's the live thread about"
  ↓ loam-cli write --type compact-snapshot --tags "session:<ts>"
                          ⋯⋯⋯⋯⋯⋯
  ↓ compaction happens
session N+1 starts
  ↓ SessionStart fires
  ↓ loam-cli recent --type compact-snapshot --limit 1
  ↓ bridge content lands in initial context
```

Reinforce-on-retrieve means bridges that the next session actually reads bump their `access_count`, so threads that keep going stay rankable while one-shot bridges quietly fall behind.

## Files

| File                    | Role                                                                |
| ----------------------- | ------------------------------------------------------------------- |
| `loam-precompact.sh`    | PreCompact hook: synthesize + write bridge entry                    |
| `loam-session-start.sh` | SessionStart hook: read most recent bridge, emit as initial context |
| `settings.example.json` | Snippet to paste into `~/.claude/settings.json`                     |

## Prerequisites

- `session-loam` installed (`pip install -e .` from the repo root; verify with `loam-cli ls`).
- One of:
  - `ollama` with a local model pulled (default: `qwq:32b`); or
  - A custom summarizer command exported via `LOAM_SUMMARIZER`.

## Setup

1. Make the scripts executable:

   ```bash
   chmod +x loam-precompact.sh loam-session-start.sh
   ```

2. Merge `settings.example.json` into `~/.claude/settings.json` under the
   `hooks` key. Adjust the script paths to match where you cloned this repo.

3. Restart Claude Code. The hooks are active on the next session.

## Configuration

Environment variables read by `loam-precompact.sh`:

| Variable          | Default       | Effect                                                            |
| ----------------- | ------------- | ----------------------------------------------------------------- |
| `LOAM_AGENT`      | `hostname -s` | Agent identity for the bridge entry                               |
| `LOAM_MODEL`      | `qwq:32b`     | Ollama model used for synthesis                                   |
| `LOAM_SUMMARIZER` | (unset)       | Override the synthesis command entirely; receives prompt on stdin |
| `LOAM_MAX_WORDS`  | `200`         | Soft target word count for the summary                            |

Environment variables read by `loam-session-start.sh`:

| Variable                | Default  | Effect                                              |
| ----------------------- | -------- | --------------------------------------------------- |
| `LOAM_AGENT`            | hostname | Agent whose bridge to read                          |
| `LOAM_BRIDGE_MAX_AGE_H` | `168`    | Skip bridges older than N hours (default: one week) |

## Swapping the synthesizer

The default uses `ollama run qwq:32b`. To use Claude in non-interactive mode instead:

```bash
export LOAM_SUMMARIZER='claude -p --model haiku --output-file -'
```

To use llama.cpp:

```bash
export LOAM_SUMMARIZER='llama-cli -m ~/models/llama-3.2-3b.gguf -p "$(cat)" -n 400'
```

Any command that reads a prompt on stdin and emits a summary on stdout will work.

## Inspecting what's in your bridge

```bash
loam-cli recent --type compact-snapshot --limit 5 --pretty
```

To purge old bridges that the next session never read (`access_count == 0`)
and are older than a month:

```bash
loam-cli prune \
  --type compact-snapshot \
  --access-count-below 1 \
  --older-than "$(date -u -d '30 days ago' +'%Y-%m-%dT%H:%M:%SZ')" \
  --apply
```

## Failure modes

The hooks fail open: if `loam-cli` isn't on PATH, or `ollama` isn't installed and `LOAM_SUMMARIZER` is unset, or the transcript path isn't passed, the hooks exit 0 silently. They never block compaction or session start.

If the synthesis model produces an empty string (refused / errored / OOM), the precompact hook skips the write — better no bridge than a confusing empty one.
