#!/usr/bin/env python3
"""Search past Claude Code and Codex sessions using FTS5 full-text search."""

import argparse
import json
import os
import re
import sqlite3
import sys
import math
import time
from datetime import datetime
from glob import glob
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
CODEX_DIR = Path.home() / ".codex"
DB_PATH = Path.home() / ".recall.db"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"


CJK_RE = re.compile(
    r'[\u2E80-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF'
    r'\U00020000-\U0002A6DF\U0002A700-\U0002B73F'
    r'\U0002B740-\U0002B81F\U0002B820-\U0002CEAF'
    r'\U0002CEB0-\U0002EBEF\U00030000-\U0003134F]'
)


def has_cjk(text):
    """Return True if text contains any CJK characters."""
    return bool(CJK_RE.search(text))


def create_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            source TEXT,
            file_path TEXT,
            project TEXT,
            slug TEXT,
            timestamp INTEGER,
            mtime REAL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
            session_id UNINDEXED,
            role,
            text,
            tokenize='porter unicode61'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_cjk USING fts5(
            session_id UNINDEXED,
            role,
            text,
            tokenize='trigram'
        );
    """)


def migrate_schema(conn):
    """Add columns if upgrading from an older schema."""
    try:
        conn.execute("SELECT source FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'claude'")
        conn.commit()
    try:
        conn.execute("SELECT file_path FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sessions ADD COLUMN file_path TEXT DEFAULT ''")
        conn.commit()



def migrate_db_location():
    """Move recall.db from ~/.claude/ to ~/ if it exists at the old path."""
    old_path = CLAUDE_DIR / "recall.db"
    if old_path.exists() and not DB_PATH.exists():
        old_path.rename(DB_PATH)
        # Also move the WAL/SHM files if they exist
        for suffix in ("-wal", "-shm"):
            old_extra = Path(str(old_path) + suffix)
            if old_extra.exists():
                old_extra.rename(Path(str(DB_PATH) + suffix))


TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}
CODEX_SKIP_MARKERS = ("<user_instructions>", "<environment_context>", "<permissions instructions>", "# AGENTS.md instructions")


def extract_text(content):
    """Extract plain text from message content (string or array format).

    Accepts "text" (Claude), "input_text" and "output_text" (Codex) block types.
    Skips tool calls, tool results, thinking blocks, and images.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type", "") in TEXT_BLOCK_TYPES
        ]
        return "\n".join(filter(None, parts))
    return ""


def parse_iso_timestamp(ts_str):
    """Parse ISO 8601 timestamp string to epoch milliseconds."""
    if not ts_str or not isinstance(ts_str, str):
        if isinstance(ts_str, (int, float)):
            return int(ts_str)
        return None
    try:
        # Handle "2026-03-03T00:26:57.352Z" format
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


# — Claude Code session parser —————————————————————————————————————————————

def parse_claude_session(path):
    """Parse a Claude Code JSONL session file, returning (metadata, messages)."""
    session_id = Path(path).stem
    project = None
    slug = None
    earliest_ts = None
    messages = []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type", "")

                # Extract cwd from any entry
                if not project:
                    cwd = entry.get("cwd", "")
                    if cwd:
                        project = cwd

                # Extract slug from any entry
                if not slug:
                    slug = entry.get("slug", "") or entry.get("leafName", "")

                # Parse timestamp
                ts_raw = entry.get("timestamp")
                ts_ms = parse_iso_timestamp(ts_raw)
                if ts_ms and (earliest_ts is None or ts_ms < earliest_ts):
                    earliest_ts = ts_ms

                # Determine role: check both "type" and "role" fields
                role = entry.get("role", "")
                if role not in ("user", "assistant"):
                    if etype == "user" or etype == "human":
                        role = "user"
                    elif etype == "assistant":
                        role = "assistant"
                    else:
                        continue

                # Extract text content — handle multiple formats:
                # 1. {message: {content: "..."}} or {message: {content: [{type:"text",...}]}}
                # 2. {content: "..."} or {content: [...]}
                content = entry.get("message", {})
                if isinstance(content, dict):
                    content = content.get("content", "")
                elif isinstance(content, str):
                    # message field is a plain string
                    pass
                else:
                    content = entry.get("content", "")

                text = extract_text(content)
                if text:
                    messages.append((role, text))

    except (OSError, PermissionError) as e:
        print(f"Warning: skipping {path}: {e}", file=sys.stderr)
        return None

    if not slug:
        slug = session_id[:12]

    metadata = {
        "session_id": session_id,
        "source": "claude",
        "file_path": path,
        "project": project or "",
        "slug": slug,
        "timestamp": earliest_ts or 0,
    }
    return metadata, messages


# — Codex session parser ———————————————————————————————————————————————————

def parse_codex_session(path):
    """Parse a Codex JSONL session file, returning (metadata, messages).

    Codex sessions live in ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl.
    Supports two formats:
      - Legacy: flat entries with {role, content, record_type, id, ...}
      - Current: wrapped entries with {timestamp, type, payload: {role, content, ...}}
    """
    session_id = Path(path).stem
    project = None
    slug = None
    earliest_ts = None
    messages = []

    # Extract date from path: sessions/YYYY/MM/DD/rollout-...
    path_match = re.search(r"sessions/(\d{4}/\d{2}/\d{2})/", path)
    date_slug = path_match.group(1).replace("/", "-") if path_match else None

    # Extract session UUID from filename: rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl
    uuid_match = re.search(
        r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        session_id,
    )

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Skip state snapshots (legacy format)
                if entry.get("record_type") == "state":
                    continue

                # Parse timestamp (present in both formats at top level)
                ts_raw = entry.get("timestamp")
                if ts_raw:
                    ts_ms = parse_iso_timestamp(ts_raw)
                    if ts_ms and (earliest_ts is None or ts_ms < earliest_ts):
                        earliest_ts = ts_ms

                etype = entry.get("type", "")

                # Current format: {type: "session_meta", payload: {id, cwd, ...}}
                if etype == "session_meta":
                    payload = entry.get("payload", {})
                    entry_id = payload.get("id", "")
                    if entry_id and session_id.startswith("rollout-"):
                        session_id = entry_id
                    if not project:
                        project = payload.get("cwd", "")
                    continue

                # Current format: {type: "response_item", payload: {role, content, ...}}
                # Legacy format: {role, content, ...} (no type or type="message")
                if etype == "response_item":
                    payload = entry.get("payload", {})
                    role = payload.get("role", "")
                    content = payload.get("content", "")
                elif etype in ("event_msg", "turn_context"):
                    continue
                else:
                    # Legacy format — session metadata in first entry
                    if not project and "id" in entry and "instructions" in entry:
                        entry_id = entry.get("id", "")
                        if entry_id and session_id.startswith("rollout-"):
                            session_id = entry_id
                        continue

                    role = entry.get("role", "")
                    content = entry.get("content", "")

                    # Legacy: extract cwd from <environment_context> blocks
                    if not project and isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                text = block.get("text", "")
                                if "Current working directory:" in text:
                                    cwd_match = re.search(
                                        r"Current working directory:\s*(.+)", text
                                    )
                                    if cwd_match:
                                        project = cwd_match.group(1).strip()

                # Only index user and assistant messages (skip developer/system)
                if role not in ("user", "assistant"):
                    continue

                text = extract_text(content)

                # Skip system/instruction blocks injected as user messages
                if not text:
                    continue
                if any(marker in text for marker in CODEX_SKIP_MARKERS):
                    continue

                messages.append((role, text))

    except (OSError, PermissionError) as e:
        print(f"Warning: skipping {path}: {e}", file=sys.stderr)
        return None

    if not slug:
        short_id = uuid_match.group(1)[:8] if uuid_match else session_id[:8]
        slug = f"{date_slug}-{short_id}" if date_slug else short_id

    metadata = {
        "session_id": session_id,
        "source": "codex",
        "file_path": path,
        "project": project or "",
        "slug": slug,
        "timestamp": earliest_ts or 0,
    }
    return metadata, messages


# — Indexing ———————————————————————————————————————————————————————————————

def index_sessions(conn, force=False):
    """Scan and index new/changed session files from all sources."""
    if force:
        conn.executescript("""
            DELETE FROM sessions;
            DELETE FROM messages;
            DELETE FROM messages_cjk;
        """)

    # Get existing mtimes keyed by file_path (stable across session_id changes)
    existing = {}
    try:
        for row in conn.execute("SELECT file_path, session_id, mtime FROM sessions"):
            existing[row[0]] = (row[1], row[2])
    except sqlite3.OperationalError:
        pass

    # Collect files from both sources
    sources = []

    # Claude Code: ~/.claude/projects/**/*.jsonl
    claude_pattern = str(CLAUDE_PROJECTS_DIR / "**" / "*.jsonl")
    for fpath in glob(claude_pattern, recursive=True):
        sources.append((fpath, "claude"))

    # Codex: ~/.codex/sessions/**/*.jsonl
    codex_pattern = str(CODEX_SESSIONS_DIR / "**" / "*.jsonl")
    for fpath in glob(codex_pattern, recursive=True):
        sources.append((fpath, "codex"))

    indexed = 0
    skipped = 0

    # Disable FTS5 automerge during bulk insert to avoid repeated segment merges
    conn.execute("INSERT INTO messages(messages, rank) VALUES('automerge', 0)")
    conn.execute("INSERT INTO messages_cjk(messages_cjk, rank) VALUES('automerge', 0)")

    for fpath, source in sources:
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            continue

        if not force and fpath in existing and existing[fpath][1] == mtime:
            skipped += 1
            continue

        # Remove old data for this file if re-indexing
        if fpath in existing:
            old_sid = existing[fpath][0]
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (old_sid,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (old_sid,))
            conn.execute("DELETE FROM messages_cjk WHERE session_id = ?", (old_sid,))

        if source == "claude":
            result = parse_claude_session(fpath)
        else:
            result = parse_codex_session(fpath)

        if result is None:
            continue

        metadata, messages = result

        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, source, file_path, project, slug, timestamp, mtime) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (metadata["session_id"], metadata["source"], metadata["file_path"],
             metadata["project"], metadata["slug"], metadata["timestamp"], mtime),
        )

        msg_rows = [(metadata["session_id"], role, text) for role, text in messages]
        conn.executemany(
            "INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)",
            msg_rows,
        )
        cjk_rows = [r for r in msg_rows if has_cjk(r[2])]
        if cjk_rows:
            conn.executemany(
                "INSERT INTO messages_cjk (session_id, role, text) VALUES (?, ?, ?)",
                cjk_rows,
            )

        indexed += 1

    conn.commit()

    # Merge all FTS5 segments into one and restore automerge
    if indexed > 0:
        conn.execute("INSERT INTO messages(messages) VALUES('optimize')")
        conn.execute("INSERT INTO messages(messages, rank) VALUES('automerge', 4)")
        conn.execute("INSERT INTO messages_cjk(messages_cjk) VALUES('optimize')")
        conn.execute("INSERT INTO messages_cjk(messages_cjk, rank) VALUES('automerge', 4)")
        conn.commit()

    # Get totals
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    return indexed, skipped, total_sessions, total_messages


# — Search —————————————————————————————————————————————————————————————————

def sanitize_fts_query(query):
    """Sanitize a query for FTS5 MATCH.

    FTS5 interprets bare hyphens as the NOT operator, so 'ask-codex' becomes
    'ask NOT codex' which errors out when 'codex' isn't a column name.
    Fix: split hyphenated words into individually quoted segments so
    'ask-codex' -> '"ask" "codex"' (proximity match, no boolean interpretation).
    User-quoted phrases and explicit boolean operators are preserved.
    """
    # Don't touch anything inside double quotes (phrases)
    parts = []
    in_quote = False
    for segment in query.split('"'):
        if in_quote:
            parts.append(f'"{segment}"')
        else:
            # Quote each part of hyphenated words individually
            # e.g. "ask-codex" -> '"ask" "codex"'
            segment = re.sub(
                r'\b(\w+(?:-\w+)+)\b',
                lambda m: ' '.join(f'"{w}"' for w in m.group().split('-')),
                segment,
            )
            parts.append(segment)
        in_quote = not in_quote
    return ''.join(parts)


def search(conn, query, project=None, days=None, source=None, limit=10):
    """Search indexed sessions. Uses trigram table for CJK queries, porter table otherwise."""
    # Pick the right FTS table based on query content
    use_cjk = has_cjk(query)
    fts_table = "messages_cjk" if use_cjk else "messages"

    # Trigram requires 3+ char queries. For shorter CJK queries, fall back to LIKE.
    use_like = use_cjk and len(query.strip()) < 3

    # Build session filter (shared by both paths)
    session_filter_conds = []
    filter_params = []
    if project:
        session_filter_conds.append("s2.project LIKE ? || '%'")
        filter_params.append(project)
    if days:
        cutoff = int((time.time() - days * 86400) * 1000)
        session_filter_conds.append("s2.timestamp >= ?")
        filter_params.append(cutoff)
    if source:
        session_filter_conds.append("s2.source = ?")
        filter_params.append(source)

    session_filter = ""
    if session_filter_conds:
        session_filter = (
            " AND session_id IN "
            "(SELECT s2.session_id FROM sessions s2 WHERE " + " AND ".join(session_filter_conds) + ")"
        )

    # Over-fetch candidates so recency re-ranking can surface recent results
    candidate_limit = limit * 3

    if use_like:
        # LIKE fallback for short CJK queries (< 3 chars)
        like_params = [f"%{query}%"] + filter_params + [candidate_limit]
        like_sql = f"""
            SELECT session_id, -1.0 as best_rank
            FROM messages_cjk
            WHERE text LIKE ?{session_filter}
            GROUP BY session_id
            LIMIT ?
        """
        try:
            ranked = conn.execute(like_sql, like_params).fetchall()
        except sqlite3.OperationalError as e:
            print(f"Search error: {e}", file=sys.stderr)
            return []
    else:
        # FTS5 MATCH path (normal)
        sanitized = sanitize_fts_query(query)
        fts_params = [sanitized] + filter_params + [candidate_limit]
        inner_sql = f"""
            SELECT session_id, MIN(rank) as best_rank
            FROM {fts_table}
            WHERE {fts_table} MATCH ?{session_filter}
            GROUP BY session_id
            ORDER BY best_rank
            LIMIT ?
        """
        try:
            ranked = conn.execute(inner_sql, fts_params).fetchall()
        except sqlite3.OperationalError as e:
            print(f"Search error: {e}", file=sys.stderr)
            return []

    results = []
    now_ms = time.time() * 1000
    for session_id, rank in ranked:
        # Get session metadata
        meta = conn.execute(
            "SELECT source, file_path, project, slug, timestamp FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not meta:
            continue

        # Get snippet from the best-matching row
        if use_like:
            snippet_row = conn.execute(
                "SELECT text FROM messages_cjk WHERE text LIKE ? AND session_id = ? LIMIT 1",
                (f"%{query}%", session_id),
            ).fetchone()
            excerpt = snippet_row[0] if snippet_row else ""
        else:
            snippet_row = conn.execute(
                f"SELECT snippet({fts_table}, 2, '**', '**', '...', 20) FROM {fts_table} WHERE {fts_table} MATCH ? AND session_id = ? LIMIT 1",
                (sanitized, session_id),
            ).fetchone()
            excerpt = snippet_row[0] if snippet_row else ""

        # Apply recency bias: blend BM25 score with a time-decay boost.
        # BM25 rank is negative (more negative = better match).
        # Recency boost: 1.0 for today, decaying with a half-life of 30 days.
        timestamp = meta[4]
        if timestamp:
            age_days = max((now_ms - timestamp) / 86_400_000, 0)
            recency_boost = math.exp(-0.693 * age_days / 30)  # half-life = 30 days
        else:
            recency_boost = 0.0
        # Blend: 80% BM25, 20% recency. Recency term scales with typical BM25 magnitude.
        blended_rank = rank * (1 - 0.2 * recency_boost)

        results.append((session_id, meta[0], meta[1], meta[2], meta[3], meta[4], excerpt, blended_rank))

    # Re-sort by blended rank and trim to requested limit.
    results.sort(key=lambda r: r[7])
    return results[:limit]


def format_timestamp(ts_ms):
    """Format millisecond timestamp to date string."""
    if not ts_ms:
        return "unknown"
    try:
        ts = float(ts_ms) / 1000  # epoch ms to seconds
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OSError, ValueError, TypeError):
        return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Search past Claude Code and Codex sessions")
    parser.add_argument("query", help="Search query (FTS5 syntax: quotes for phrases, AND/OR/NOT)")
    parser.add_argument("--project", help="Filter to sessions from a specific project path (prefix match)")
    parser.add_argument("--days", type=int, help="Only sessions from last N days")
    parser.add_argument("--source", choices=["claude", "codex"], help="Filter by source (claude or codex)")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--reindex", action="store_true", help="Force full rebuild of the index")

    args = parser.parse_args()

    migrate_db_location()
    new_db = not DB_PATH.exists()
    old_umask = os.umask(0o077)
    conn = sqlite3.connect(str(DB_PATH))
    os.umask(old_umask)
    if new_db:
        os.chmod(str(DB_PATH), 0o600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    create_schema(conn)
    migrate_schema(conn)

    # Index
    t0 = time.time()
    indexed, skipped, total_sessions, total_messages = index_sessions(conn, force=args.reindex)
    index_time = time.time() - t0

    if indexed > 0:
        print(f"Indexed {indexed} sessions in {index_time:.1f}s", file=sys.stderr)

    # Search
    results = search(conn, args.query, project=args.project, days=args.days, source=args.source, limit=args.limit)

    if not results:
        print("No matching sessions found.")
        conn.close()
        return

    print(f"Found {len(results)} sessions (index: {total_sessions} sessions, {total_messages} messages):\n")

    for i, (session_id, source, file_path, project, slug, timestamp, excerpt, rank) in enumerate(results, 1):
        date = format_timestamp(timestamp)
        src_tag = f"[{source}]" if source else ""
        proj_name = Path(project).name if project else "unknown"
        print(f"[{i}] {date} | {slug} | {proj_name} {src_tag}")
        if project:
            print(f"    {project}")
        print(f"    ID: {session_id}")
        if file_path:
            print(f"    File: {file_path}")
        if excerpt:
            # Clean up excerpt for display
            excerpt_clean = excerpt.replace("\n", " ").strip()
            if len(excerpt_clean) > 200:
                excerpt_clean = excerpt_clean[:200] + "..."
            print(f"    > {excerpt_clean}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
