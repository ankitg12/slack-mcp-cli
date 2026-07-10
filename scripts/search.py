#!/usr/bin/env python3
"""Deterministic Slack search CLI — wraps the official Slack MCP server.

Unlike an LLM agent calling the MCP tool directly (where permalink
inclusion depends on the model remembering to keep response_format
='detailed' and to carry the Permalink field into its answer), this script
ALWAYS fetches detailed results — which already embed a
`Permalink: [link](...)` line per message — and prints/exports them
verbatim. No LLM in the loop, no risk of a summary dropping the link.

Normal mode (single search, up to --limit results):
    python search.py "deploy failure" --limit 10
    python search.py "budget" --private --limit 5
    python search.py "incident" --json
    python search.py "roadmap type:pdfs" --private --content-types files

Export mode (--export PATH): any query, walked across a long time range and
written incrementally to JSON. The Slack MCP server caps cursor pagination
at 20 pages (~400 messages) per continuous search session — raises
`page_limit_exceeded` beyond that. To get deeper history, export mode walks
backward in fixed-size date windows (`before`/`after` on the message
timestamp); each window gets its own fresh cursor walk, and results are
merged/deduplicated across windows. The output file is written after every
window (not just at the end — `"complete": false` while running, `true`
once finished), so it's safe to interrupt at any point without losing
already-fetched data:
    python search.py "from:me" --private --export messages.json --start-days-ago 365
    python search.py "in:#general" --export general.json --window-days 7
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


async def _run_search(client, tool: str, query: str, limit: int, content_types: str) -> dict:
    args = {"query": query, "limit": limit, "response_format": "detailed"}
    if content_types:
        args["content_types"] = content_types
    result = await client.call_tool(tool, args)
    raw_text = result.content[0].text if result.content else "{}"
    return json.loads(raw_text)


async def _search_window(client, tool: str, query: str, after_ts: float | None, before_ts: float | None) -> tuple[list[dict], bool]:
    """Run one windowed search, paging until exhausted or MAX_PAGES_PER_WINDOW.

    Returns (records, hit_page_limit).
    """
    records: list[dict] = []
    cursor = None
    for _page in range(1, MAX_PAGES_PER_WINDOW + 1):
        args = {
            "query": query,
            "limit": MAX_PER_PAGE,
            "sort": "timestamp",
            "sort_dir": "desc",
            "include_context": False,
            "response_format": "detailed",
        }
        if cursor:
            args["cursor"] = cursor
        if after_ts:
            args["after"] = str(after_ts)
        if before_ts:
            args["before"] = str(before_ts)

        result = await client.call_tool(tool, args)
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


def _write_export(out_path: Path, query: str, all_records: dict, start_days_ago: int, window_days: int, complete: bool) -> None:
    sorted_records = sorted(all_records.values(), key=lambda r: float(r["message_ts"]), reverse=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "query": query,
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


async def run_export(tool: str, query: str, out_path: Path, start_days_ago: int, window_days: int) -> None:
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
            records, hit_limit = await _search_window(client, tool, query, after_ts=window_start, before_ts=window_end)
            for r in records:
                all_records[f"{r['channel']}|{r['message_ts']}"] = r

            # Write after every window so a kill/crash mid-run never loses
            # already-fetched data — a write-only-at-the-end design will
            # lose everything if the process is interrupted partway through
            # a large export.
            _write_export(out_path, query, all_records, start_days_ago, window_days, complete=False)

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

    _write_export(out_path, query, all_records, start_days_ago, window_days, complete=True)
    print(f"Done. Wrote {len(all_records)} unique messages to {out_path}", file=sys.stderr)


async def run_search(tool: str, query: str, limit: int, content_types: str, as_json: bool) -> None:
    async with get_client() as client:
        payload = await _run_search(client, tool, query, limit, content_types)

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    print(payload.get("results", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Slack search with guaranteed permalinks; any Slack search query, including its modifiers (in:, from:, after:, type:pdfs, etc.)")
    parser.add_argument("query", help="Slack search query")
    parser.add_argument("--limit", type=int, default=10, help="Max results for a normal (non-export) search (default: 10)")
    parser.add_argument("--private", action="store_true", help="Include private channels/DMs (requires consent scope)")
    parser.add_argument("--content-types", default="messages", help="Comma-separated: messages,files (default: messages)")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of formatted text")
    parser.add_argument("--export", metavar="PATH", help="Export mode: walk the query across a long time range, writing incrementally to this JSON file")
    parser.add_argument("--start-days-ago", type=int, default=1825, help="Export mode: how far back to search, in days (default: 5 years)")
    parser.add_argument("--window-days", type=int, default=14, help="Export mode: days per search window (default: 14; narrow this if a window still hits page_limit_exceeded)")
    args = parser.parse_args()

    tool = "slack_search_public_and_private" if args.private else "slack_search_public"

    if args.export:
        asyncio.run(run_export(tool, args.query, Path(args.export).expanduser(), args.start_days_ago, args.window_days))
    else:
        asyncio.run(run_search(tool, args.query, args.limit, args.content_types, args.json))


if __name__ == "__main__":
    main()
