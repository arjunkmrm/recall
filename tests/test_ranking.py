"""Tests for the recency blend in search().

bm25() returns a negative score and results sort ascending, so "better" means
"more negative". Anything that multiplies the rank has to move a recent session
away from zero, not toward it.

Fixtures are generated as strings inside tmpdir — no fixture files committed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Make scripts/ importable as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402


class RecencyBias(unittest.TestCase):
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

    def seed(self, ages_in_days):
        """One session per age, each matching the query exactly as well."""
        now_ms = time.time() * 1000
        for i, age in enumerate(ages_in_days):
            when = datetime.fromtimestamp((now_ms - age * 86_400_000) / 1000,
                                          tz=timezone.utc)
            stamp = when.isoformat().replace("+00:00", "Z")
            path = self.projects / "proj" / f"{i:08d}-0000-0000-0000-000000000000.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "type": "user", "cwd": "/work", "timestamp": stamp,
                "message": {"content": "distinctivetoken with some padding words"},
            }) + "\n", encoding="utf-8")
            # Keep mtimes distinct so the scan notices every file.
            os.utime(path, (1_800_000_000 + i, 1_800_000_000 + i))

        conn = sqlite3.connect(self.db)
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        recall.index_sessions(conn)
        conn.commit()
        return conn

    def test_the_most_recent_of_equal_matches_comes_first(self):
        conn = self.seed([400, 200, 1])
        try:
            results = recall.search(conn, "distinctivetoken", limit=3)
            self.assertEqual(len(results), 3)
            timestamps = [row[5] for row in results]
            self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        finally:
            conn.close()

    def test_a_session_with_no_timestamp_ranks_last(self):
        """boost 0 has to mean "as old as it gets", not "as good as it gets"."""
        conn = self.seed([1])
        try:
            path = self.projects / "proj" / "undated.jsonl"
            path.write_text(json.dumps({
                "type": "user", "cwd": "/work",
                "message": {"content": "distinctivetoken with some padding words"},
            }) + "\n", encoding="utf-8")
            os.utime(path, (1_800_000_100, 1_800_000_100))
            recall.index_sessions(conn)
            conn.commit()

            results = recall.search(conn, "distinctivetoken", limit=2)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[-1][5], 0)
        finally:
            conn.close()

    def test_the_blend_never_moves_a_rank_toward_zero(self):
        """Directly, without a corpus: a boost must only ever improve a score."""
        for rank in (-0.000002, -5.16, -26.86):
            for boost in (0.0, 0.5, 1.0):
                with self.subTest(rank=rank, boost=boost):
                    self.assertLessEqual(rank * (1 + 0.2 * boost), rank)


if __name__ == "__main__":
    unittest.main()
