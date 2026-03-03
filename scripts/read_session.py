#!/usr/bin/env python3
"""Pretty-print a Claude Code or Codex session transcript."""

import json
import sys


def print_claude_session(path):
    """Print a Claude Code JSONL session."""
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
            role = entry.get("role", "")
            if role not in ("user", "assistant"):
                if etype in ("user", "human"):
                    role = "user"
                elif etype == "assistant":
                    role = "assistant"
                else:
                    continue

            content = entry.get("message", {})
            if isinstance(content, dict):
                content = content.get("content", "")
            elif not isinstance(content, str):
                content = entry.get("content", "")

            text = extract_text(content)
            if text:
                print(f"--- {role} ---")
                print(text[:500])
                print()


def print_codex_session(path):
    """Print a Codex JSONL session."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("record_type") == "state":
                continue

            role = entry.get("role", "")
            if role not in ("user", "assistant"):
                continue

            text = extract_text(entry.get("content", ""))

            if text and "<user_instructions>" not in text and "<environment_context>" not in text:
                print(f"--- {role} ---")
                print(text[:500])
                print()


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
            if btype in ("text", "input_text", "output_text"):
                text = block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def detect_format(path):
    """Detect whether a session file is Claude Code or Codex format."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Codex files have record_type or top-level id+instructions
            if entry.get("record_type") == "state":
                return "codex"
            # Claude Code entries have parentUuid or message.role
            if "parentUuid" in entry or "message" in entry:
                return "claude"
            # Codex first entry has id + instructions
            if "id" in entry and "instructions" in entry:
                return "codex"
    return "claude"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <session.jsonl>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    fmt = detect_format(path)

    if fmt == "codex":
        print_codex_session(path)
    else:
        print_claude_session(path)


if __name__ == "__main__":
    main()
