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

from recall_common import SKIP_MARKERS, extract_text

CLAUDE_DIR = Path.home() / ".claude"
CODEX_DIR = Path.home() / ".codex"
DB_PATH = Path.home() / ".recall.db"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"


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


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
CJK_SEGMENT_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
FTS_SPECIAL_QUERY_RE = re.compile(r'["*():]|(^|\s)(AND|OR|NOT)(\s|$)', re.IGNORECASE)
LIKE_ESCAPE = "\\"

def escape_like(value):
    """Escape LIKE wildcards in user-provided terms."""
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


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


def contains_cjk(text):
    """Return True if text includes any CJK characters."""
    return bool(text and CJK_RE.search(text))


def is_simple_query(text):
    """Best-effort check for a plain-text query (no explicit FTS syntax)."""
    return bool(text and not FTS_SPECIAL_QUERY_RE.search(text))


def extract_cjk_terms(text):
    """Extract distinct CJK terms for LIKE fallback matching."""
    seen = set()
    terms = []
    for segment in CJK_SEGMENT_RE.findall(text or ""):
        if segment not in seen:
            seen.add(segment)
            terms.append(segment)
    return terms


def make_excerpt(text, needle=None, max_len=200):
    """Create a short readable excerpt, centered around a match when possible."""
    clean = (text or "").replace("\n", " ").strip()
    if not clean:
        return ""
    if needle:
        idx = clean.find(needle)
        if idx >= 0:
            start = max(idx - 60, 0)
            end = min(idx + len(needle) + 120, len(clean))
            excerpt = clean[start:end]
            if start > 0:
                excerpt = "..." + excerpt
            if end < len(clean):
                excerpt = excerpt + "..."
            return excerpt
    return clean[:max_len] + ("..." if len(clean) > max_len else "")


def project_match_clause(project, alias):
    """Build SQL clause+params to match an exact project or its child paths."""
    normalized = (project or "").rstrip("/")
    if not normalized:
        normalized = "/"

    like_prefix = escape_like(normalized)
    clause = f"({alias}.project = ? OR {alias}.project LIKE ? ESCAPE '{LIKE_ESCAPE}')"
    return clause, [normalized, like_prefix + "/%"]


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
        "timestamp": earliest_ts,
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
                if any(marker in text for marker in SKIP_MARKERS):
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
        "timestamp": earliest_ts,
    }
    return metadata, messages


def build_session_constraints(project=None, days=None, source=None, alias="s2"):
    """Build session-level SQL filter clauses and parameter list."""
    conds = []
    params = []
    if project:
        project_clause, project_params = project_match_clause(project, alias)
        conds.append(project_clause)
        params.extend(project_params)
    if days:
        cutoff = int((time.time() - days * 86400) * 1000)
        conds.append(f"{alias}.timestamp >= ?")
        params.append(cutoff)
    if source:
        conds.append(f"{alias}.source = ?")
        params.append(source)
    return conds, params


def prune_orphan_sessions(conn):
    """Remove indexed sessions whose source files no longer exist."""
    to_delete = []
    for session_id, file_path in conn.execute("SELECT session_id, file_path FROM sessions"):
        if file_path and not os.path.exists(file_path):
            to_delete.append((session_id,))

    if not to_delete:
        return 0

    conn.executemany("DELETE FROM sessions WHERE session_id = ?", to_delete)
    conn.executemany("DELETE FROM messages WHERE session_id = ?", to_delete)
    conn.commit()
    return len(to_delete)


# — Indexing ———————————————————————————————————————————————————————————————

def index_sessions(conn, force=False):
    """Scan and index new/changed session files from all sources."""
    if force:
        conn.executescript("""
            DELETE FROM sessions;
            DELETE FROM messages;
        """)

    orphaned = 0
    if not force:
        orphaned = prune_orphan_sessions(conn)

    # Get existing mtimes keyed by file_path (stable across session_id changes)
    existing = {}
    try:
        for row in conn.execute("SELECT file_path, session_id, mtime, timestamp FROM sessions"):
            existing[row[0]] = (row[1], row[2], row[3])
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
    wrote_messages = False

    for fpath, source in sources:
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            continue

        if (
            not force
            and fpath in existing
            and existing[fpath][1] == mtime
            and (existing[fpath][2] or 0) > 0
        ):
            skipped += 1
            continue

        # Remove old data for this file if re-indexing
        if fpath in existing:
            old_sid = existing[fpath][0]
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (old_sid,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (old_sid,))

        if source == "claude":
            result = parse_claude_session(fpath)
        else:
            result = parse_codex_session(fpath)

        if result is None:
            continue

        metadata, messages = result

        # Disable FTS5 automerge only when we know we'll write new rows.
        if not wrote_messages:
            conn.execute("INSERT INTO messages(messages, rank) VALUES('automerge', 0)")
            wrote_messages = True

        session_timestamp = metadata["timestamp"] if metadata["timestamp"] else int(mtime * 1000)

        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, source, file_path, project, slug, timestamp, mtime) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (metadata["session_id"], metadata["source"], metadata["file_path"],
             metadata["project"], metadata["slug"], session_timestamp, mtime),
        )

        conn.executemany(
            "INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)",
            [(metadata["session_id"], role, text) for role, text in messages],
        )

        indexed += 1

    conn.commit()

    # Merge all FTS5 segments into one and restore automerge
    if indexed > 0:
        conn.execute("INSERT INTO messages(messages) VALUES('optimize')")
        conn.execute("INSERT INTO messages(messages, rank) VALUES('automerge', 4)")
        conn.commit()

    # Get totals
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    return indexed, skipped, total_sessions, total_messages, orphaned


# — Search —————————————————————————————————————————————————————————————————

def list_sessions(conn, project=None, days=None, source=None, limit=10, query=None):
    """List sessions ordered by recency, optionally filtered by a text query."""
    conds, params = build_session_constraints(project=project, days=days, source=source, alias="s")

    if not query:
        sql = "SELECT session_id, source, file_path, project, slug, timestamp FROM sessions s"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY timestamp DESC, session_id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [(sid, src, fpath, proj, slug, ts, "", 0.0) for sid, src, fpath, proj, slug, ts in rows]

    # Query-filtered list mode: match text, then sort by recency.
    query_sql = "SELECT s.session_id, s.source, s.file_path, s.project, s.slug, s.timestamp FROM sessions s WHERE "
    if conds:
        query_sql += " AND ".join(conds) + " AND "
    query_sql += "s.session_id IN (SELECT session_id FROM messages WHERE messages MATCH ?) "
    query_sql += "ORDER BY s.timestamp DESC, s.session_id DESC LIMIT ?"

    query_params = params + [query, limit]
    try:
        rows = conn.execute(query_sql, query_params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"List query error: {e}", file=sys.stderr)
        rows = []

    results = [(sid, src, fpath, proj, slug, ts, "", 0.0) for sid, src, fpath, proj, slug, ts in rows]
    if results:
        return results

    # If FTS under-recalls for simple CJK query, use substring fallback and keep recency order.
    if contains_cjk(query) and is_simple_query(query):
        return search_cjk_fallback(
            conn,
            query,
            project=project,
            days=days,
            source=source,
            limit=limit,
            preserve_sql_order=True,
        )
    return results


def search_cjk_fallback(conn, query, project=None, days=None, source=None, limit=10, preserve_sql_order=False):
    """Fallback search for simple CJK queries using escaped substring matching."""
    terms = extract_cjk_terms(query)
    if not terms:
        return []

    like_conds = ["m.text LIKE ? ESCAPE '\\'" for _ in terms]
    like_params = [f"%{escape_like(term)}%" for term in terms]
    session_conds, session_params = build_session_constraints(project=project, days=days, source=source, alias="s")
    session_filter = ""
    if session_conds:
        session_filter = " AND " + " AND ".join(session_conds)

    candidate_limit = limit * 3
    sql = f"""
        SELECT m.session_id, MAX(m.rowid) as match_rowid,
               s.source, s.file_path, s.project, s.slug, s.timestamp
        FROM messages m
        JOIN sessions s ON s.session_id = m.session_id
        WHERE {' AND '.join(like_conds)}{session_filter}
        GROUP BY m.session_id
        ORDER BY s.timestamp DESC, match_rowid DESC
        LIMIT ?
    """
    params = like_params + session_params + [candidate_limit]

    try:
        matched = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"CJK fallback search error: {e}", file=sys.stderr)
        return []

    results = []
    now_ms = time.time() * 1000
    highlight_term = terms[0]
    for session_id, rowid, src, file_path, project_value, slug, timestamp in matched:
        text_row = conn.execute("SELECT text FROM messages WHERE rowid = ?", (rowid,)).fetchone()
        excerpt = make_excerpt(text_row[0] if text_row else "", highlight_term)

        if timestamp:
            age_days = max((now_ms - timestamp) / 86_400_000, 0)
            recency_boost = math.exp(-0.693 * age_days / 30)  # half-life = 30 days
        else:
            recency_boost = 0.0

        # Keep FTS-ranked results ahead of fallback results; use recency within fallback set.
        blended_rank = 1.0 - 0.2 * recency_boost
        results.append((session_id, src, file_path, project_value, slug, timestamp, excerpt, blended_rank))

    if not preserve_sql_order:
        results.sort(key=lambda r: r[7])
    return results[:limit]


def search(conn, query, project=None, days=None, source=None, limit=10):
    """Search indexed sessions."""
    # FTS5 auxiliary functions (bm25, snippet) don't work with GROUP BY.
    # Use a subquery to get the best-ranking rowid per session, then fetch snippets.
    fts_params = [query]
    session_filter = ""

    subconds, subparams = build_session_constraints(project=project, days=days, source=source, alias="s2")
    if subconds:
        session_filter = (
            " AND session_id IN "
            "(SELECT s2.session_id FROM sessions s2 WHERE " + " AND ".join(subconds) + ")"
        )
        fts_params.extend(subparams)

    # Over-fetch candidates so recency re-ranking can surface recent results
    # that pure BM25 might have ranked just outside the cutoff.
    candidate_limit = limit * 3
    fts_params.append(candidate_limit)

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
        snippet_row = conn.execute(
            "SELECT snippet(messages, 2, '**', '**', '...', 20) FROM messages WHERE messages MATCH ? AND session_id = ? LIMIT 1",
            (query, session_id),
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
    parser.add_argument("query", nargs="?", help="Search query; optional in --list mode to filter listed sessions")
    parser.add_argument("--list", action="store_true", help="List recent sessions; optional QUERY filters the list")
    parser.add_argument("--project", help="Filter to sessions from a specific project path (prefix match)")
    parser.add_argument("--days", type=int, help="Only sessions from last N days")
    parser.add_argument("--source", choices=["claude", "codex"], help="Filter by source (claude or codex)")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--reindex", action="store_true", help="Force full rebuild of the index")

    args = parser.parse_args()
    if not args.list and not args.query:
        parser.error("QUERY is required unless --list is used")

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
    indexed, skipped, total_sessions, total_messages, orphaned = index_sessions(conn, force=args.reindex)
    index_time = time.time() - t0

    if indexed > 0:
        print(f"Indexed {indexed} sessions in {index_time:.1f}s", file=sys.stderr)
    if orphaned > 0:
        print(f"Pruned {orphaned} orphaned sessions", file=sys.stderr)

    # Search or list
    if args.list:
        results = list_sessions(
            conn,
            project=args.project,
            days=args.days,
            source=args.source,
            limit=args.limit,
            query=args.query,
        )
    else:
        results = search(conn, args.query, project=args.project, days=args.days, source=args.source, limit=args.limit)

        # For simple Chinese queries, augment sparse FTS results with substring fallback.
        if contains_cjk(args.query) and is_simple_query(args.query) and len(results) < args.limit:
            fallback = search_cjk_fallback(
                conn,
                args.query,
                project=args.project,
                days=args.days,
                source=args.source,
                limit=args.limit,
            )
            existing_ids = {row[0] for row in results}
            for row in fallback:
                if row[0] not in existing_ids:
                    results.append(row)
            results.sort(key=lambda r: r[7])
            results = results[:args.limit]

    if not results:
        print("No sessions found." if args.list else "No matching sessions found.")
        conn.close()
        return

    verb = "Listed" if args.list else "Found"
    print(f"{verb} {len(results)} sessions (index: {total_sessions} sessions, {total_messages} messages):\n")

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
            excerpt_clean = make_excerpt(excerpt)
            print(f"    > {excerpt_clean}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
