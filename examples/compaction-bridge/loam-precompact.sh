#!/usr/bin/env bash
# loam-precompact.sh — PreCompact hook that writes a session-bridge entry
# to session-loam before Claude Code compacts the conversation.
#
# Wire it up in ~/.claude/settings.json under the PreCompact hook list.
# The next session's SessionStart hook (loam-session-start.sh) reads it back.
#
# Env vars expected (Claude Code provides these to PreCompact hooks):
#   TRANSCRIPT_PATH   — absolute path to the live session transcript .jsonl
#   SESSION_ID        — optional, used in tags if present
#
# Optional overrides:
#   LOAM_AGENT        — agent identity (default: hostname-short)
#   LOAM_MODEL        — local model used for synthesis (default: qwq:32b via ollama)
#   LOAM_SUMMARIZER   — override the synthesis command; receives prompt on stdin
#                       and must emit the summary on stdout. Default: ollama run "$LOAM_MODEL".
#   LOAM_MAX_WORDS    — soft target word count for the summary (default: 200)

set -euo pipefail

AGENT="${LOAM_AGENT:-$(hostname -s)}"
MODEL="${LOAM_MODEL:-qwq:32b}"
MAX_WORDS="${LOAM_MAX_WORDS:-200}"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
SESSION_TAG="${SESSION_ID:-$TS}"

# Locate the transcript. If the hook didn't pass it, exit 0 quietly — the
# hook contract is non-blocking; failing here would stall compaction.
if [ -z "${TRANSCRIPT_PATH:-}" ] || [ ! -f "${TRANSCRIPT_PATH:-}" ]; then
  echo "loam-precompact: TRANSCRIPT_PATH unset or missing; skipping" >&2
  exit 0
fi

# Build the synthesis prompt. Tail the last ~600 lines of the transcript —
# enough for context, bounded enough not to overflow a small local model.
read -r -d '' PROMPT <<EOF || true
Summarize the most active threads, in-flight tasks, and decisions from
this session in <= $MAX_WORDS words. Skip greetings, stylistic notes,
and pleasantries. Use short paragraphs. End with an explicit
"In-flight tasks:" list with one item per line.

TRANSCRIPT (most recent first):
$(tail -n 600 "$TRANSCRIPT_PATH")
EOF

# Run the synthesis. Default is `ollama run qwq:32b` reading prompt from stdin.
# Set LOAM_SUMMARIZER if you want to swap in claude -p, llama.cpp, etc.
if [ -n "${LOAM_SUMMARIZER:-}" ]; then
  SUMMARY=$(echo "$PROMPT" | bash -c "$LOAM_SUMMARIZER")
else
  if ! command -v ollama >/dev/null 2>&1; then
    echo "loam-precompact: ollama not found and LOAM_SUMMARIZER unset; skipping" >&2
    exit 0
  fi
  SUMMARY=$(echo "$PROMPT" | ollama run "$MODEL")
fi

# Empty summary means model declined or errored. Don't write garbage.
if [ -z "${SUMMARY// }" ]; then
  echo "loam-precompact: empty summary; skipping" >&2
  exit 0
fi

# Locate loam-cli. Prefer PATH; fall back to a sibling repo if running
# from a development checkout.
if ! command -v loam-cli >/dev/null 2>&1; then
  echo "loam-precompact: loam-cli not on PATH; install session-loam first" >&2
  exit 0
fi

# Write the bridge entry. Reinforcement happens later when the next
# session's reader hook fetches it.
printf '%s\n' "$SUMMARY" | loam-cli write \
  --agent "$AGENT" \
  --type compact-snapshot \
  --tags "compact-snapshot,session:$SESSION_TAG" \
  --source "essence:precompact-hook" >/dev/null

echo "loam-precompact: bridge entry written for agent=$AGENT session=$SESSION_TAG" >&2
exit 0
