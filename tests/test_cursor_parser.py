"""Tests for parse_cursor_session and cursor detection in read_session."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402
import read_session  # noqa: E402


CURSOR_SAMPLE = [
    {
        "role": "user",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": "<user_query>\nfix sorting when adding tasks\n</user_query>",
                }
            ]
        },
    },
    {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Checking the sort order in tasks.service.ts."},
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"path": "src/app/services/tasks.service.ts"},
                },
            ]
        },
    },
]


def write_jsonl(tmpdir: Path, rel_path: str, entries: list[dict]) -> str:
    path = tmpdir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry))
            f.write("\n")
    return str(path)


class TestDecodeCursorProjectSlug(unittest.TestCase):
    def test_windows_path_without_internal_hyphens(self):
        self.assertEqual(
            recall.decode_cursor_project_slug("c-Users-alice-src-foo"),
            "C:\\Users\\alice\\src\\foo",
        )

    def test_drive_only_slug(self):
        self.assertEqual(recall.decode_cursor_project_slug("D"), "D:\\")

    def test_numeric_slug_returns_empty(self):
        self.assertEqual(recall.decode_cursor_project_slug("1768486658315"), "")


class TestParseCursorSession(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="recall-cursor-test-"))

    def test_extracts_user_and_assistant_text(self):
        sample = list(CURSOR_SAMPLE)
        sample[1] = {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Checking the sort order."},
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {
                            "command": "git status",
                            "working_directory": "C:\\Users\\alice\\src\\foo",
                        },
                    },
                ]
            },
        }
        path = write_jsonl(
            self.tmpdir,
            ".cursor/projects/c-Users-alice-src-foo/agent-transcripts/"
            "2cb862d9-8a30-4c5c-823b-f20169c3d18d/2cb862d9-8a30-4c5c-823b-f20169c3d18d.jsonl",
            sample,
        )
        metadata, messages = recall.parse_cursor_session(path)

        self.assertEqual(metadata["session_id"], "2cb862d9-8a30-4c5c-823b-f20169c3d18d")
        self.assertEqual(metadata["source"], "cursor")
        self.assertEqual(metadata["project"], "C:\\Users\\alice\\src\\foo")
        self.assertEqual(metadata["slug"], "2cb862d9")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0][0], "user")
        self.assertIn("sorting", messages[0][1])
        self.assertEqual(messages[1][0], "assistant")
        self.assertIn("sort order", messages[1][1])

    def test_skips_tool_use_blocks(self):
        path = write_jsonl(
            self.tmpdir,
            "projects/c-Users-alice/agent-transcripts/abc/abc.jsonl",
            CURSOR_SAMPLE,
        )
        _, messages = recall.parse_cursor_session(path)
        assistant_text = messages[1][1]
        self.assertNotIn("tool_use", assistant_text)
        self.assertNotIn("Read", assistant_text)


class TestDetectFormatCursor(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="recall-cursor-test-"))

    def test_detects_cursor_from_path(self):
        path = write_jsonl(
            self.tmpdir,
            ".cursor/projects/c-Users-alice/agent-transcripts/sid/sid.jsonl",
            CURSOR_SAMPLE,
        )
        self.assertEqual(read_session.detect_format(path), "cursor")


class TestRealCursorSession(unittest.TestCase):
    def test_parse_any_real_cursor_session(self):
        cursor_dir = Path.home() / ".cursor" / "projects"
        if not cursor_dir.is_dir():
            self.skipTest("no cursor projects on this host")

        files = sorted(cursor_dir.glob("**/agent-transcripts/*/*.jsonl"))
        files = [f for f in files if f.parent.name == f.stem]
        if not files:
            self.skipTest("no cursor agent transcripts on this host")

        smallest = min(files, key=lambda f: f.stat().st_size)
        result = recall.parse_cursor_session(str(smallest))
        self.assertIsNotNone(result)
        metadata, messages = result
        self.assertEqual(metadata["source"], "cursor")
        self.assertTrue(metadata["session_id"])
        for role, text in messages:
            self.assertIn(role, ("user", "assistant"))
            self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
