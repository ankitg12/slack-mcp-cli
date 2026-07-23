#!/usr/bin/env python3
"""Deterministic Slack thread reader CLI — wraps the official Slack MCP server.

Always fetches detailed responses, which already embed a permalink per
reply, and prints them verbatim.

Usage:
    python read_thread.py C0123456789 1783440000.000000
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from slack_call import call_tool  # noqa: E402


def run(channel_id: str, message_ts: str, as_json: bool, html_path: str | None) -> None:
    raw_text = call_tool(
        "slack_read_thread",
        {"channel_id": channel_id, "message_ts": message_ts},
    )
    payload = json.loads(raw_text)

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    results = payload.get("messages", payload.get("results", raw_text))
    if html_path:
        from render import write_html
        out = write_html(f"Slack thread: {channel_id}/{message_ts}", results, Path(html_path))
        print(f"Wrote {out}", file=sys.stderr)
        return

    print(results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Slack thread reader with guaranteed permalinks")
    parser.add_argument("channel_id", help="Channel ID")
    parser.add_argument("message_ts", help="Timestamp of the parent message (Slack ts format)")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of formatted text")
    parser.add_argument("--html", metavar="PATH", help="Render results as a clickable HTML page and open it (instead of printing text)")
    args = parser.parse_args()
    run(args.channel_id, args.message_ts, args.json, args.html)


if __name__ == "__main__":
    main()
