#!/usr/bin/env python3
"""Deterministic Slack channel history CLI — wraps the official Slack MCP server.

Always fetches detailed responses, which already embed a permalink per
message, and prints them verbatim.

Usage:
    python read_channel.py C0123456789 --limit 20
    python read_channel.py C0123456789 --oldest 1783440000.000000 --json
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from slack_client import get_client  # noqa: E402
from render import write_html  # noqa: E402


async def run(channel_id: str, limit: int, oldest: str | None, latest: str | None, as_json: bool, html_path: str | None) -> None:
    args = {"channel_id": channel_id, "limit": limit, "response_format": "detailed"}
    if oldest:
        args["oldest"] = oldest
    if latest:
        args["latest"] = latest

    async with get_client() as client:
        result = await client.call_tool("slack_read_channel", args)

    raw_text = result.content[0].text if result.content else "{}"
    payload = json.loads(raw_text)

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    results = payload.get("results", raw_text)
    if html_path:
        out = write_html(f"Slack channel: {channel_id}", results, Path(html_path))
        print(f"Wrote {out}", file=sys.stderr)
        return

    print(results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Slack channel history with guaranteed permalinks")
    parser.add_argument("channel_id", help="Channel ID (or user_id for DM history)")
    parser.add_argument("--limit", type=int, default=50, help="Max messages (default: 50)")
    parser.add_argument("--oldest", help="Start timestamp filter")
    parser.add_argument("--latest", help="End timestamp filter")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of formatted text")
    parser.add_argument("--html", metavar="PATH", help="Render results as a clickable HTML page and open it (instead of printing text)")
    args = parser.parse_args()
    asyncio.run(run(args.channel_id, args.limit, args.oldest, args.latest, args.json, args.html))


if __name__ == "__main__":
    main()
