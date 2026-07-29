"""Tests that a forced reindex does not discard saved sessions."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make scripts/ importable as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402


class ReindexPreservesHistory(unittest.TestCase):
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

    def session(self, name, text, mtime=1_800_000_000):
        path = self.projects / "proj" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "type": "user",
            "cwd": "/work",
            "message": {"content": text},
        }) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def connect(self):
        conn = sqlite3.connect(self.db)
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        return conn

    def contents(self):
        conn = sqlite3.connect(self.db)
        try:
            sessions = list(conn.execute(
                "SELECT session_id, file_path FROM sessions ORDER BY session_id"))
            messages = list(conn.execute(
                "SELECT session_id, role, text FROM messages ORDER BY session_id, text"))
            cjk = list(conn.execute(
                "SELECT session_id, role, text FROM messages_cjk ORDER BY session_id, text"))
            return sessions, messages, cjk
        finally:
            conn.close()

    def test_reindex_keeps_a_session_whose_file_is_gone(self):
        gone = self.session("gone", "irreplaceable history")
        live = self.session("live", "before")
        conn = self.connect()
        try:
            recall.index_sessions(conn)
            gone.unlink()
            self.session("live", "after")

            recall.index_sessions(conn, force=True)

            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE text = 'irreplaceable history'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE text = 'after'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE text = 'before'"
            ).fetchone()[0], 0)
            self.assertTrue(live.exists())
        finally:
            conn.close()

    def test_reindex_keeps_a_session_that_cannot_be_read(self):
        path = self.session("unreadable", "saved before the read failed")
        conn = self.connect()
        try:
            recall.index_sessions(conn)
            with patch.object(recall, "parse_claude_session", return_value=None):
                recall.index_sessions(conn, force=True)

            self.assertEqual(conn.execute(
                "SELECT text FROM messages WHERE session_id = ?",
                (path.stem,),
            ).fetchone()[0], "saved before the read failed")
        finally:
            conn.close()

    def test_reindex_replaces_a_legacy_row_without_duplicating_it(self):
        path = self.session("legacy", "current text")
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, source, file_path, project, slug, timestamp, mtime) "
                "VALUES ('legacy', 'claude', '', '/work', 'legacy', 0, 0)"
            )
            conn.execute(
                "INSERT INTO messages VALUES "
                "('legacy', 'user', 'text from the old index')"
            )
            conn.commit()

            recall.index_sessions(conn, force=True)

            self.assertEqual(conn.execute(
                "SELECT file_path FROM sessions WHERE session_id = 'legacy'"
            ).fetchone()[0], str(path))
            self.assertEqual(list(conn.execute(
                "SELECT text FROM messages WHERE session_id = 'legacy'"
            )), [("current text",)])
        finally:
            conn.close()

    def test_an_interrupted_reindex_leaves_the_old_index_intact(self):
        self.session("first", "old first")
        self.session("second", "old second")
        conn = self.connect()
        recall.index_sessions(conn)
        conn.close()
        before = self.contents()

        self.session("first", "new first")
        self.session("second", "new second")
        original = recall.parse_claude_session
        calls = 0

        def interrupt_second_file(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("stopped during reindex")
            return original(path)

        conn = self.connect()
        try:
            with patch.object(recall, "parse_claude_session",
                              side_effect=interrupt_second_file):
                with self.assertRaises(KeyboardInterrupt):
                    recall.index_sessions(conn, force=True)
        finally:
            conn.close()

        self.assertEqual(self.contents(), before)


if __name__ == "__main__":
    unittest.main()
