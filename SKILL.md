---
name: recall
description: >
  Search past Claude Code and Codex sessions. Triggers: /recall, "search old conversations",
  "find a past session", "recall a previous conversation", "search session history",
  "what did we discuss", "remember when we"
metadata:
  author: arjunkmrm
  version: "0.3.0"
  license: MIT
---

# /recall — Search Past Claude & Codex Sessions

Search all past Claude Code and Codex sessions using full-text search with BM25 ranking.

## Usage

```bash
python3 <RECALL_SKILL_DIR>/scripts/recall.py [QUERY] [--list] [--project PATH] [--days N] [--source claude|codex] [--limit N] [--reindex]
```

`<RECALL_SKILL_DIR>` varies by installation. Common examples:
- `~/.claude/skills/recall`
- `~/.agents/skills/recall`

## Examples

```bash
# Simple keyword search
python3 <RECALL_SKILL_DIR>/scripts/recall.py "bufferStore"

# Phrase search (exact match)
python3 <RECALL_SKILL_DIR>/scripts/recall.py '"ACP protocol"'

# Boolean query
python3 <RECALL_SKILL_DIR>/scripts/recall.py "rust AND async"

# Prefix search
python3 <RECALL_SKILL_DIR>/scripts/recall.py "buffer*"

# Filter by project and recency
python3 <RECALL_SKILL_DIR>/scripts/recall.py "state machine" --project ~/my-project --days 7

# Search only Claude Code sessions
python3 <RECALL_SKILL_DIR>/scripts/recall.py "buffer" --source claude

# Search only Codex sessions
python3 <RECALL_SKILL_DIR>/scripts/recall.py "buffer" --source codex

# Force reindex
python3 <RECALL_SKILL_DIR>/scripts/recall.py --reindex "test"

# List recent sessions
python3 <RECALL_SKILL_DIR>/scripts/recall.py --list --limit 20

# List mode with optional text filter
python3 <RECALL_SKILL_DIR>/scripts/recall.py --list "state machine" --limit 20
```

## Query Syntax (FTS5)

- **Words**: `bufferStore` — matches stemmed variants (e.g., "discussing" matches "discuss")
- **Phrases**: `"ACP protocol"` — exact phrase match
- **Boolean**: `rust AND async`, `tauri OR electron`, `NOT deprecated`
- **Prefix**: `buffer*` — matches bufferStore, bufferMap, etc.
- **Combined**: `"state machine" AND test`

## After Finding a Match

To resume a session, `cd` into the project directory and use the appropriate command:

```bash
# Claude Code sessions [claude]
cd /path/to/project
claude --resume SESSION_ID

# Codex sessions [codex]
cd /path/to/project
codex resume SESSION_ID
```

Each result includes a `File:` path. Use it to read the raw transcript (auto-detects format):

```bash
python3 <RECALL_SKILL_DIR>/scripts/read_session.py <File-path-from-result>
```

If results are missing `File:` paths, run `--reindex` to backfill.

## Notes

- Index is stored at `~/.recall.db` (SQLite FTS5, auto-migrated from `~/.claude/recall.db`)
- Indexes both `~/.claude/projects/` (Claude Code) and `~/.codex/sessions/` (Codex)
- First run indexes all sessions (a few seconds); subsequent runs are incremental
- Automatically prunes orphaned DB rows when indexed source files are removed
- Only user and assistant messages are indexed (tool calls, thinking blocks, state snapshots skipped)
- Results show `[claude]` or `[codex]` tags to indicate the source
- For simple CJK queries, adds substring fallback matching to improve recall
