"""Tests for the role column not being searchable.

`role` holds the literal strings "user" and "assistant". Indexing it made both
words match almost every message in the corpus rather than the ones that say
them, so any query containing either was silently narrowed.

Fixtures are generated as strings inside tmpdir — no fixture files committed.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

# Make scripts/ importable as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402

OLD_SCHEMA = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, source TEXT, file_path TEXT,
        project TEXT, slug TEXT, timestamp INTEGER, mtime REAL);
    CREATE VIRTUAL TABLE messages USING fts5(
        session_id UNINDEXED, role, text, tokenize='porter unicode61');
    CREATE VIRTUAL TABLE messages_cjk USING fts5(
        session_id UNINDEXED, role, text, tokenize='trigram');
"""


class RolesAreNotSearchable(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.projects = self.root / "projects"
        self.db = str(self.root / "index.db")

        for name, value in (("CLAUDE_DIR", self.root),
                            ("CLAUDE_PROJECTS_DIR", self.projects),
                            ("CODEX_SESSIONS_DIR", self.root / "none"),
                            ("PI_SESSIONS_DIR", self.root / "none"),
                            ("GROK_SESSIONS_DIR", self.root / "none")):
            self.addCleanup(setattr, recall, name, getattr(recall, name))
            setattr(recall, name, value)

    def session(self, name, entries):
        path = self.projects / "proj" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        os.utime(path, (1_800_000_000, 1_800_000_000))
        return path

    def indexed(self):
        conn = sqlite3.connect(self.db)
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        recall.index_sessions(conn)
        conn.commit()
        return conn

    def test_a_role_name_matches_only_messages_that_say_it(self):
        self.session("aaaa", [
            {"type": "user", "cwd": "/w", "message": {"content": "a question about rust"}},
            {"type": "assistant", "message": {"content": "an answer about rust"}}])
        self.session("bbbb", [
            {"type": "user", "cwd": "/w",
             "message": {"content": "the word assistant appears here"}}])
        conn = self.indexed()
        try:
            hits = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE messages MATCH 'assistant'").fetchone()[0]
            self.assertEqual(hits, 1)
        finally:
            conn.close()

    def test_a_role_word_does_not_narrow_an_ordinary_query(self):
        self.session("cccc", [
            {"type": "user", "cwd": "/w", "message": {"content": "a question about rust"}},
            {"type": "assistant", "message": {"content": "an answer about rust"}}])
        conn = self.indexed()
        try:
            self.assertEqual(len(recall.search(conn, "rust", limit=10)), 1)
            self.assertEqual(recall.search(conn, "user rust", limit=10), [])
        finally:
            conn.close()


class RebuildingAnOlderIndex(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "old.db")

    def old_index(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO messages VALUES ('gone', 'assistant', 'irreplaceable text')")
        conn.execute("INSERT INTO messages_cjk VALUES ('gone', 'assistant', '日本語のテキスト')")
        conn.commit()
        return conn

    def test_both_tables_are_rebuilt_without_losing_rows(self):
        """From the rows already in them — sessions whose files were deleted
        have nothing left to re-read."""
        conn = self.old_index()
        try:
            with redirect_stderr(io.StringIO()):
                recall.migrate_message_columns(conn)
            for table in ("messages", "messages_cjk"):
                self.assertIn("role UNINDEXED", conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0])
            self.assertEqual(conn.execute("SELECT text FROM messages").fetchone()[0],
                             "irreplaceable text")
            self.assertEqual(conn.execute("SELECT text FROM messages_cjk").fetchone()[0],
                             "日本語のテキスト")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE messages MATCH 'assistant'").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE messages MATCH 'irreplaceable'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_an_index_that_is_already_right_is_left_alone(self):
        conn = sqlite3.connect(self.db)
        try:
            recall.create_schema(conn)
            before = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'messages'").fetchone()[0]
            recall.migrate_message_columns(conn)
            self.assertEqual(conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'messages'").fetchone()[0], before)
        finally:
            conn.close()

    def test_the_rebuild_runs_inside_the_index_lock(self):
        """The rebuild takes seconds on a large index, past SQLite's busy
        timeout, so main must only attempt it while holding the index lock —
        otherwise a concurrent run dies with "database is locked", the crash
        the lock exists to prevent. A run without the lock searches the old
        schema, which still works."""
        import inspect
        src = inspect.getsource(recall.main)
        self.assertIn("migrate_message_columns", src)
        self.assertLess(src.index("with index_lock()"),
                        src.index("migrate_message_columns"))

        # And the old schema a lockless run is left searching still answers.
        conn = self.old_index()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE messages MATCH 'irreplaceable'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_an_interrupted_rebuild_leaves_the_index_usable(self):
        """It is one transaction, so a run killed part way through rolls back
        rather than leaving a half-built table for the next run to die on."""
        conn = self.old_index()
        try:
            class DiesPartWay:
                def __init__(self, wrapped):
                    self._wrapped = wrapped

                def execute(self, sql, *args):
                    if sql.strip().startswith("DROP TABLE messages"):
                        raise KeyboardInterrupt("killed mid-rebuild")
                    return self._wrapped.execute(sql, *args)

                def __getattr__(self, name):
                    return getattr(self._wrapped, name)

            with redirect_stderr(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    recall.migrate_message_columns(DiesPartWay(conn))

            leftovers = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_rebuilt'")]
            self.assertEqual(leftovers, [])
            self.assertEqual(conn.execute("SELECT text FROM messages").fetchone()[0],
                             "irreplaceable text")

            with redirect_stderr(io.StringIO()):
                recall.migrate_message_columns(conn)
            self.assertIn("role UNINDEXED", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'messages'").fetchone()[0])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
