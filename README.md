# recall

Ever lost a conversation session with Claude Code or Codex and wish you could resume it? This skill lets Claude and your agents search across all your past conversations with full-text search. Builds a SQLite FTS5 index over `~/.claude/projects/` and `~/.codex/sessions/` with BM25 ranking, Porter stemming, CJK support, and incremental updates.

## Install

```bash
npx skills add arjunkmrm/recall
```

Then use `/recall` in Claude Code (or Codex) or ask "find a past session where we talked about foo" (you might need to restart Claude Code).

## How it works
### Index

```
  ~/.claude/projects/**/*.jsonl ──┐
                                  ├─▶ Index ──▶ ~/.recall.db (SQLite FTS5)
  ~/.codex/sessions/**/*.jsonl ───┘   [incremental - mtime-based]


```
### Query
```
  Query ──▶ Detect CJK? ──▶ FTS5 Match ──▶ BM25 rank ──▶ Recency boost ──▶ Results
                │                           [half-life: 30 days]
                │
          ┌─────┴──────┐
          │            │
     No CJK        Has CJK
     porter         trigram
     unicode61      table
          │            │
          └─────┬──────┘
                ▼
         snippet extraction
         highlighted excerpts
```

- Indexes user/assistant messages into a SQLite FTS5 database at `~/.recall.db`
- First run indexes all sessions (a few seconds); subsequent runs only process new/modified files
- Dual-table FTS: Porter stemming for English, trigram tokenizer for CJK (Japanese, Chinese, Korean)
- CJK messages are selectively indexed into the trigram table; query routing is automatic
- Skips tool_use, tool_result, thinking, and image blocks
- Results ranked by BM25 with a slight recency bias (recent sessions get up to a 20% boost, decaying with a 30-day half-life)
- Results tagged `[claude]` or `[codex]` with highlighted excerpts
- No dependencies — Python 3.9+ stdlib only (sqlite3, json, argparse)

## Contributing

Found a bug or have an idea? [Open an issue](https://github.com/arjunkmrm/recall/issues) or submit a pull request — contributions are welcome!

