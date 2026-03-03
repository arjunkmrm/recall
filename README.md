# recall

Ever lost a conversation session with Claude Code or Codex and wish you could resume it? This skill lets Claude and your agents search across all your past conversations with full-text search. Builds a SQLite FTS5 index over `~/.claude/projects/` and `~/.codex/sessions/` with BM25 ranking, Porter stemming, and incremental updates.

## Install

```bash
npx skills add arjunkmrm/recall
```

Then use `/recall` in Claude Code or ask "find a past session where we talked about foo" (you might need to restart Claude Code).

## How it works

- Indexes user/assistant messages from both `~/.claude/projects/**/*.jsonl` (Claude Code) and `~/.codex/sessions/` (Codex) into a SQLite FTS5 database at `~/.recall.db`
- First run indexes all sessions (a few seconds); subsequent runs only process new/modified files
- Skips tool_use, tool_result, thinking, and image blocks
- Returns results ranked by BM25 with highlighted excerpts, tagged `[claude]` or `[codex]`
- No dependencies — Python 3.9+ stdlib only (sqlite3, json, argparse)

## Query syntax

| Pattern | Example | Description |
|---------|---------|-------------|
| Words | `bufferStore` | Stemmed match ("discussing" → "discuss") |
| Phrases | `"ACP protocol"` | Exact phrase |
| Boolean | `rust AND async` | AND, OR, NOT |
| Prefix | `buffer*` | Wildcard suffix |
| Combined | `"state machine" AND test` | Mix freely |

## Filtering

- `--project PATH` — filter by project directory
- `--days N` — only sessions from the last N days
- `--source claude|codex` — search only Claude Code or Codex sessions
- `--limit N` — max results to return
- `--reindex` — force a full reindex

## Resuming a session

Each result includes a `File:` path and session ID. To resume:

```bash
# Claude Code sessions
claude --resume SESSION_ID

# Codex sessions
codex resume SESSION_ID
```

To read a raw transcript:

```bash
python3 ~/.claude/skills/recall/scripts/read_session.py <File-path-from-result>
```
