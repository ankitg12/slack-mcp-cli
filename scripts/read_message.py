#!/usr/bin/env python3
"""Deterministic single-message Slack reader CLI — wraps the official Slack MCP server.

Accepts a Slack **permalink** (as copied from the client's "Copy link") and
resolves it to the underlying message via `slack_read_thread`. Slack's
`conversations.replies` accepts either a thread parent's ts or any reply's ts,
so a root message returns just itself while a threaded message returns its
whole thread (with the target message included).

Usage:
    python read_message.py https://acme.slack.com/archives/C0123456789/p1784802006076149
    python read_message.py https://acme.slack.com/archives/C0123456789/p1784802006076149?thread_ts=1784800000.000000&cid=C0123456789
    python read_message.py C0123456789 1784802006.076149   # channel_id + ts form
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from slack_client import get_cli_client  # noqa: E402
from render import write_html  # noqa: E402

_ARCHIVE_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d+)")


def parse_permalink(url: str) -> tuple[str, str]:
    """Return (channel_id, message_ts) from a Slack permalink.

    The `pXXXXXXXXXXYYYYYY` path segment encodes the ts with the decimal point
    removed; the last 6 digits are the microsecond fraction. A `thread_ts`
    query param (present on replies) takes precedence so the whole thread is
    fetched with the target message included.
    """
    parsed = urlparse(url)
    match = _ARCHIVE_RE.search(parsed.path)
    if not match:
        raise ValueError(f"Not a Slack archive permalink: {url!r}")
    channel_id = match.group(1)
    digits = match.group(2)
    message_ts = f"{digits[:-6]}.{digits[-6:]}"

    thread_ts = parse_qs(parsed.query).get("thread_ts", [None])[0]
    return channel_id, thread_ts or message_ts


async def run(channel_id: str, message_ts: str, as_json: bool, html_path: str | None) -> None:
    async with get_cli_client() as client:
        result = await client.call_tool(
            "slack_read_thread",
            {"channel_id": channel_id, "message_ts": message_ts, "response_format": "detailed"},
        )

    raw_text = result.content[0].text if result.content else "{}"
    payload = json.loads(raw_text)

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    results = payload.get("messages", payload.get("results", raw_text))
    if html_path:
        out = write_html(f"Slack message: {channel_id}/{message_ts}", results, Path(html_path))
        print(f"Wrote {out}", file=sys.stderr)
        return

    print(results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a single Slack message from its permalink (or channel_id + ts)")
    parser.add_argument("target", help="Slack permalink URL, or channel_id when ts is given as second arg")
    parser.add_argument("ts", nargs="?", help="Message ts (Slack ts format) when first arg is a channel_id")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of formatted text")
    parser.add_argument("--html", metavar="PATH", help="Render results as a clickable HTML page and open it (instead of printing text)")
    args = parser.parse_args()

    if args.ts:
        channel_id, message_ts = args.target, args.ts
    else:
        channel_id, message_ts = parse_permalink(args.target)

    asyncio.run(run(channel_id, message_ts, args.json, args.html))


if __name__ == "__main__":
    main()
