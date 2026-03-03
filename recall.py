#!/usr/bin/env python3
"""Search past Claude Code sessions using FTS5 full-text search."""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from glob import glob
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
DB_PATH = CLAUDE_DIR / "recall.db"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def create_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
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
    """)


def extract_text(content):
    """Extract plain text from message content (string or array format)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype in ("tool_result", "tool_use", "thinking", "image"):
                continue
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)
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


def parse_session_file(path):
    """Parse a JSONL session file, yielding (metadata, messages)."""
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
        "project": project or "",
        "slug": slug,
        "timestamp": earliest_ts or 0,
    }
    return metadata, messages


def index_sessions(conn, force=False):
    """Scan and index new/changed session files."""
    if force:
        conn.executescript("""
            DELETE FROM sessions;
            DELETE FROM messages;
        """)

    # Get existing mtimes
    existing = {}
    try:
        for row in conn.execute("SELECT session_id, mtime FROM sessions"):
            existing[row[0]] = row[1]
    except sqlite3.OperationalError:
        pass

    # Find all JSONL files
    pattern = str(PROJECTS_DIR / "**" / "*.jsonl")
    files = glob(pattern, recursive=True)

    indexed = 0
    skipped = 0

    for fpath in files:
        fname = Path(fpath).stem
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            continue

        if not force and fname in existing and existing[fname] == mtime:
            skipped += 1
            continue

        # Remove old data for this session if re-indexing
        if fname in existing:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (fname,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (fname,))

        result = parse_session_file(fpath)
        if result is None:
            continue

        metadata, messages = result

        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, project, slug, timestamp, mtime) VALUES (?, ?, ?, ?, ?)",
            (metadata["session_id"], metadata["project"], metadata["slug"], metadata["timestamp"], mtime),
        )

        for role, text in messages:
            conn.execute(
                "INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)",
                (metadata["session_id"], role, text),
            )

        indexed += 1

    conn.commit()

    # Get totals
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    return indexed, skipped, total_sessions, total_messages


def search(conn, query, project=None, days=None, limit=10):
    """Search indexed sessions."""
    # FTS5 auxiliary functions (bm25, snippet) don't work with GROUP BY.
    # Use a subquery to get the best-ranking rowid per session, then fetch snippets.
    fts_params = [query]
    session_filter = ""

    if project or days:
        subconds = []
        if project:
            subconds.append("s2.project LIKE ? || '%'")
            fts_params.append(project)
        if days:
            cutoff = int((time.time() - days * 86400) * 1000)
            subconds.append("s2.timestamp >= ?")
            fts_params.append(cutoff)
        session_filter = (
            " AND session_id IN "
            "(SELECT s2.session_id FROM sessions s2 WHERE " + " AND ".join(subconds) + ")"
        )

    fts_params.append(limit)

    # First find best-ranking session_ids.
    # FTS5's rank column is auto-populated with bm25 when using ORDER BY rank.
    inner_sql = f"""
        SELECT session_id, MIN(rank) as best_rank
        FROM messages
        WHERE messages MATCH ?{session_filter}
        GROUP BY session_id
        ORDER BY best_rank
        LIMIT ?
    """

    try:
        # Two-pass: first get sessions+ranks, then fetch snippets individually
        ranked = conn.execute(inner_sql, fts_params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Search error: {e}", file=sys.stderr)
        return []

    results = []
    for session_id, rank in ranked:
        # Get session metadata
        meta = conn.execute(
            "SELECT project, slug, timestamp FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not meta:
            continue

        # Get snippet from the best-matching row
        snippet_row = conn.execute(
            "SELECT snippet(messages, 2, '**', '**', '...', 20) FROM messages WHERE messages MATCH ? AND session_id = ? LIMIT 1",
            (query, session_id),
        ).fetchone()
        excerpt = snippet_row[0] if snippet_row else ""

        results.append((session_id, meta[0], meta[1], meta[2], excerpt, rank))

    return results


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
    parser = argparse.ArgumentParser(description="Search past Claude Code sessions")
    parser.add_argument("query", help="Search query (FTS5 syntax: quotes for phrases, AND/OR/NOT)")
    parser.add_argument("--project", help="Filter to sessions from a specific project path (prefix match)")
    parser.add_argument("--days", type=int, help="Only sessions from last N days")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--reindex", action="store_true", help="Force full rebuild of the index")

    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    create_schema(conn)

    # Index
    t0 = time.time()
    indexed, skipped, total_sessions, total_messages = index_sessions(conn, force=args.reindex)
    index_time = time.time() - t0

    if indexed > 0:
        print(f"Indexed {indexed} sessions in {index_time:.1f}s", file=sys.stderr)

    # Search
    results = search(conn, args.query, project=args.project, days=args.days, limit=args.limit)

    if not results:
        print("No matching sessions found.")
        conn.close()
        return

    print(f"Found {len(results)} sessions (index: {total_sessions} sessions, {total_messages} messages):\n")

    for i, (session_id, project, slug, timestamp, excerpt, rank) in enumerate(results, 1):
        date = format_timestamp(timestamp)
        print(f"[{i}] {date} | {slug} | {Path(project).name if project else 'unknown'}")
        if project:
            print(f"    {project}")
        print(f"    ID: {session_id}")
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
