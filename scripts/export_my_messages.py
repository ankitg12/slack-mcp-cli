#!/usr/bin/env python3
"""Export every Slack message you've sent, across all channel types, to JSON.

Uses `from:me` with Slack's search API. The Slack MCP server caps cursor
pagination at 20 pages (~400 messages) per search session — raises
`page_limit_exceeded` beyond that (confirmed 2026-07). There is no official
bulk-export API available to a non-admin workspace member (Slack's own
"Guide to Slack import and export tools" confirms full self-serve export is
Enterprise-org-owner-only). To get deeper history within a single member's
own OAuth grant, this script walks backward in date windows (`before`/`after`
on Message_ts) — each window gets its own fresh cursor walk capped at ~400
messages, small enough windows should stay under that cap.

Usage:
    python export_my_messages.py --out messages.json
    python export_my_messages.py --start-days-ago 730 --window-days 14 --out messages.json
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from slack_client import get_client  # noqa: E402

MAX_PER_PAGE = 20  # hard cap enforced by the Slack MCP server's `limit` param
MAX_PAGES_PER_WINDOW = 19  # server raises page_limit_exceeded at page 20

_RESULT_RE = re.compile(
    r"Channel: (?P<channel>.+?)\n"
    r"(?:Participants: .+?\n)?"
    r"From: (?P<sender>.+?) <(?P<sender_email>.*?)> \(ID: (?P<sender_id>\S+)\)\s*\n"
    r"Time: (?P<time>.+?)\n"
    r"Message_ts: (?P<ts>[\d.]+)\n"
    r"(?:Reply count: \d+\n)?"
    r"Permalink: \[link\]\((?P<permalink>.+?)\)\n"
    r"Text: \n(?P<text>.*?)(?=\n---\n|\Z)",
    re.DOTALL,
)


def _parse_results_block(markdown: str) -> list[dict]:
    records = []
    for m in _RESULT_RE.finditer(markdown):
        d = m.groupdict()
        records.append(
            {
                "channel": d["channel"].strip(),
                "sender": d["sender"].strip(),
                "sender_email": d["sender_email"].strip(),
                "time": d["time"].strip(),
                "message_ts": d["ts"].strip(),
                "permalink": d["permalink"].strip().replace("\\/", "/"),
                "text": d["text"].strip(),
            }
        )
    return records


async def _search_window(client, after_ts: float | None, before_ts: float | None) -> tuple[list[dict], bool]:
    """Run one windowed search, paging until exhausted or MAX_PAGES_PER_WINDOW.

    Returns (records, hit_page_limit).
    """
    records: list[dict] = []
    cursor = None
    for page in range(1, MAX_PAGES_PER_WINDOW + 1):
        args = {
            "query": "from:me",
            "limit": MAX_PER_PAGE,
            "sort": "timestamp",
            "sort_dir": "desc",
            "include_context": False,
            "response_format": "detailed",
            "channel_types": "public_channel,private_channel,mpim,im",
        }
        if cursor:
            args["cursor"] = cursor
        if after_ts:
            args["after"] = str(after_ts)
        if before_ts:
            args["before"] = str(before_ts)

        result = await client.call_tool("slack_search_public_and_private", args)
        payload = json.loads(result.content[0].text)
        markdown = payload.get("results", "")
        batch = _parse_results_block(markdown)
        records.extend(batch)

        pag_info = payload.get("pagination_info", "")
        cursor_match = re.search(r"cursor `([^`]+)`", pag_info)
        if not cursor_match or not batch:
            return records, False
        cursor = cursor_match.group(1)
        time.sleep(0.3)

    return records, True  # exhausted MAX_PAGES_PER_WINDOW without finishing


def _write_output(out_path: Path, all_records: dict, start_days_ago: int, window_days: int, complete: bool) -> None:
    sorted_records = sorted(all_records.values(), key=lambda r: float(r["message_ts"]), reverse=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "query": "from:me",
                "count": len(sorted_records),
                "coverage_days": start_days_ago,
                "window_days": window_days,
                "complete": complete,
                "messages": sorted_records,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def run(out_path: Path, start_days_ago: int, window_days: int) -> None:
    now = time.time()
    window_seconds = window_days * 86400
    window_end = now
    window_start = now - window_seconds

    all_records: dict[str, dict] = {}  # keyed by (channel, message_ts) to dedupe overlaps
    oldest_start = now - (start_days_ago * 86400)
    window_num = 0

    async with get_client() as client:
        while window_end > oldest_start:
            window_num += 1
            records, hit_limit = await _search_window(client, after_ts=window_start, before_ts=window_end)
            for r in records:
                all_records[f"{r['channel']}|{r['message_ts']}"] = r

            # Write after every window so a kill/crash mid-run never loses
            # already-fetched data — a write-only-at-the-end design will
            # lose everything if the process is interrupted partway through
            # a large export.
            _write_output(out_path, all_records, start_days_ago, window_days, complete=False)

            print(
                f"window {window_num} [{time.strftime('%Y-%m-%d', time.localtime(window_start))} .. "
                f"{time.strftime('%Y-%m-%d', time.localtime(window_end))}]: "
                f"+{len(records)} messages (total unique {len(all_records)}, saved to {out_path})"
                + (" [HIT PAGE LIMIT — window too wide, narrowing may recover more]" if hit_limit else ""),
                file=sys.stderr,
            )
            window_end = window_start
            window_start = max(oldest_start, window_end - window_seconds)
            if window_start >= window_end:
                break

    _write_output(out_path, all_records, start_days_ago, window_days, complete=True)
    print(f"Done. Wrote {len(all_records)} unique messages to {out_path}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export all of your sent Slack messages to JSON, windowed to work around the MCP server's page_limit_exceeded cap")
    parser.add_argument("--out", required=True, help="Output JSON file path")
    parser.add_argument("--start-days-ago", type=int, default=1825, help="How far back to search, in days (default: 5 years)")
    parser.add_argument("--window-days", type=int, default=14, help="Days per search window (default: 14; narrow this if a window still hits page_limit_exceeded)")
    args = parser.parse_args()
    asyncio.run(run(Path(args.out).expanduser(), args.start_days_ago, args.window_days))


if __name__ == "__main__":
    main()
