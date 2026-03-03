---
description: >
  Search past Claude Code sessions. Triggers: /recall, "search old conversations",
  "find a past session", "recall a previous conversation", "search session history",
  "what did we discuss", "remember when we"
---

# /recall — Search Past Claude Sessions

Search all past Claude Code sessions using full-text search with BM25 ranking.

## Usage

```bash
python3 ~/.claude/skills/recall/scripts/recall.py QUERY [--project PATH] [--days N] [--limit N] [--reindex]
```

## Examples

```bash
# Simple keyword search
python3 ~/.claude/skills/recall/scripts/recall.py "bufferStore"

# Phrase search (exact match)
python3 ~/.claude/skills/recall/scripts/recall.py '"ACP protocol"'

# Boolean query
python3 ~/.claude/skills/recall/scripts/recall.py "rust AND async"

# Prefix search
python3 ~/.claude/skills/recall/scripts/recall.py "buffer*"

# Filter by project and recency
python3 ~/.claude/skills/recall/scripts/recall.py "state machine" --project /Users/arjun/Documents/github/sono --days 7

# Force reindex
python3 ~/.claude/skills/recall/scripts/recall.py --reindex "test"
```

## Query Syntax (FTS5)

- **Words**: `bufferStore` — matches stemmed variants (e.g., "discussing" matches "discuss")
- **Phrases**: `"ACP protocol"` — exact phrase match
- **Boolean**: `rust AND async`, `tauri OR electron`, `NOT deprecated`
- **Prefix**: `buffer*` — matches bufferStore, bufferMap, etc.
- **Combined**: `"state machine" AND test`

## After Finding a Match

Once you find a relevant session, read the full transcript:

```bash
cat ~/.claude/projects/<project-key>/<session-id>.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    entry = json.loads(line.strip())
    role = entry.get('role', entry.get('type', ''))
    if role in ('user', 'assistant'):
        content = entry.get('message', {}).get('content', '')
        if isinstance(content, str) and content:
            print(f'--- {role} ---')
            print(content[:500])
            print()
"
```

Or read the JSONL file directly for full content.

## Notes

- Index is stored at `~/.claude/recall.db` (SQLite FTS5)
- First run indexes all sessions (a few seconds); subsequent runs are incremental
- Only user and assistant messages are indexed (tool calls, thinking blocks skipped)
