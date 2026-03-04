# recall

Ever lost a conversation session with Claude Code or Codex and wish you could resume it? This skill lets Claude and your agents search across all your past conversations with full-text search. Builds a SQLite FTS5 index over `~/.claude/projects/` and `~/.codex/sessions/` with BM25 ranking, recency-aware results, and incremental updates.

## Install

```bash
npx skills add arjunkmrm/recall
```

Then use `/recall` in Claude Code (or Codex) or ask "find a past session where we talked about foo" (you might need to restart Claude Code).

## Usage

```bash
# Full-text search
python3 scripts/recall.py "state machine"

# List recent sessions (no query required)
python3 scripts/recall.py --list --limit 20

# List mode with optional text filter
python3 scripts/recall.py --list "auth api" --source codex --limit 20

# Search only Codex sessions from the last 7 days
python3 scripts/recall.py "mock api" --source codex --days 7
```

## How it works

```
  ~/.claude/projects/**/*.jsonl ──┐
                                  ├─▶ Index ──▶ ~/.recall.db (SQLite FTS5)
  ~/.codex/sessions/**/*.jsonl ──-┘      │
                                         │  incremental (mtime-based)
                                         │
  Query ──▶ FTS5 Match ──▶ BM25 rank ──▶ Recency boost ──▶ Results
                │                    [half-life: 30 days]
                │  [Porter stemming
                │   phrase/boolean/prefix]
                ▼
         snippet extraction
         highlighted excerpts
```

- Indexes user/assistant messages into a SQLite FTS5 database at `~/.recall.db`
- First run indexes all sessions (a few seconds); subsequent runs only process new/modified files
- Automatically prunes orphaned DB rows when the backing JSONL file is gone
- Skips tool_use, tool_result, thinking, and image blocks
- Results ranked by BM25 with a slight recency bias (recent sessions get up to a 20% boost, decaying with a 30-day half-life)
- Adds a CJK substring fallback for simple Chinese/Japanese/Korean queries when FTS recall is sparse
- Supports `--list [QUERY]` to browse recent sessions, with optional text filtering
- Results tagged `[claude]` or `[codex]` with highlighted excerpts
- No dependencies — Python 3.9+ stdlib only (sqlite3, json, argparse)

## Contributing

Found a bug or have an idea? [Open an issue](https://github.com/arjunkmrm/recall/issues) or submit a pull request — contributions are welcome!
