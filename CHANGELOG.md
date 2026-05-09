# Changelog

## 0.4.1

- Make the positional `query` argument optional. When omitted, list every
  session in the time window without text matching — bypasses FTS entirely
  and queries the `sessions` table by `(timestamp, source, project)`,
  sorted by recency. Useful when callers want to enumerate every session
  in a window rather than search for a term.
- Output banner reads `Listed N sessions ...` in list mode (vs `Found ...`)
- Empty-result message reads `No sessions in the time window.` in list mode
- No schema change. No reindex needed.

### Examples

```bash
recall --days 7                         # every session in the last week
recall --days 7 --source pi             # every pi session in the last week
recall --project ~/my-project --days 30 # this project, last 30 days
recall "buffer" --days 7                # text-search, unchanged
```

## 0.4.0

- Add pi (`mariozechner/pi-coding-agent`) session support — indexes
  `~/.claude/projects/`, `~/.codex/sessions/`, and `~/.pi/agent/sessions/`
- Unified search across Claude Code, Codex, and pi sessions
- Results tagged `[claude]`, `[codex]`, or `[pi]` to show origin
- `--source` choices now include `pi` (was `claude|codex`)
- `read_session.py` auto-detects pi format (header `type: "session"` with `cwd`)
- Pi parser keeps user/assistant text only — skips thinking, toolCall, image,
  toolResult, bashExecution, custom, custom_message, session_info, model_change,
  thinking_level_change, compaction, branch_summary, label

### Upgrading from 0.3.x

Run `--reindex` once to pull pi sessions into the index:

```bash
python3 ~/.claude/skills/recall/scripts/recall.py --reindex "test"
```

## 0.3.0

- Add CJK (Japanese, Chinese, Korean) search support via dual-table FTS
- English queries use Porter stemming (`messages` table), CJK queries use trigram matching (`messages_cjk` table)
- Only messages containing CJK characters are indexed into the trigram table (selective indexing)
- Query routing is automatic based on presence of CJK characters in the search term
- Auto-migration: existing databases get the new CJK table on first run and trigger a reindex

### Upgrading from 0.2.x

Run `--reindex` once to build the CJK index:

```bash
python3 ~/.claude/skills/recall/scripts/recall.py --reindex "test"
```

Closes #4.

## 0.2.2

- Add slight recency bias to search ranking
- Blend BM25 relevance with time-decay boost (half-life: 30 days, 20% weight)
- Over-fetch 3x candidates before re-ranking to avoid cutting off recent results

## 0.2.1

- Batch message inserts with `executemany`
- Disable FTS5 automerge during bulk insert, optimize after
- Add MIT license

### Reindex benchmarks (1939 sessions, ~50K messages)

| Version | Time |
|---|---|
| 0.2.0 | ~10.4s |
| 0.2.1 | ~7.4s |

## 0.2.0

- Add Codex session support — indexes both `~/.claude/projects/` and `~/.codex/sessions/`
- Unified search across Claude Code and Codex sessions
- Results tagged with `[claude]` or `[codex]` to show origin
- New `--source claude|codex` flag to filter by tool
- DB moved from `~/.claude/recall.db` to `~/.recall.db` (auto-migrated on first run)
- Schema migration adds `source` and `file_path` columns to existing databases
- Results now show full `File:` path — works with subagent sessions nested in subdirectories
- New `read_session.py` script for reading transcripts (auto-detects format, JSON by default, `--pretty` for human-readable)
- Concise `extract_text` using list comprehension and `TEXT_BLOCK_TYPES` set

### Backward compatibility
- DB auto-migrated from `~/.claude/recall.db` to `~/.recall.db` on first run
- `source` column defaults to `"claude"` for existing rows
- If results are missing `File:` paths, run `--reindex` to backfill

## 0.1.0

- Initial release
- FTS5 full-text search over Claude Code sessions
- BM25 ranking with snippet extraction
- Incremental indexing via file mtime tracking
- `--project`, `--days`, `--limit`, `--reindex` filters
