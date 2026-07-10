#!/usr/bin/env python3
"""Deterministic Slack search CLI — wraps the official Slack MCP server.

Unlike an LLM agent calling the MCP tool directly (where permalink
inclusion depends on the model remembering to keep response_format
='detailed' and to carry the Permalink field into its answer), this script
ALWAYS fetches detailed results — which already embed a
`Permalink: [link](...)` line per message — and prints them verbatim. No
LLM in the loop, no risk of a summary dropping the link.

Usage:
    python search.py "deploy failure" --limit 10
    python search.py "budget" --private --limit 5
    python search.py "incident" --json
    python search.py "roadmap type:pdfs" --private --content-types files
    python search.py "roadmap type:canvases" --private --content-types files
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from slack_client import get_client  # noqa: E402


async def run(query: str, limit: int, private: bool, content_types: str, as_json: bool) -> None:
    tool = "slack_search_public_and_private" if private else "slack_search_public"
    args = {"query": query, "limit": limit, "response_format": "detailed"}
    if content_types:
        args["content_types"] = content_types
    async with get_client() as client:
        result = await client.call_tool(tool, args)

    raw_text = result.content[0].text if result.content else "{}"
    payload = json.loads(raw_text)

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    print(payload.get("results", raw_text))


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Slack search with guaranteed permalinks")
    parser.add_argument("query", help="Slack search query, supports Slack search modifiers (in:, from:, after:, type:pdfs, type:presentations, type:canvases, etc.)")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--private", action="store_true", help="Include private channels/DMs (requires consent scope)")
    parser.add_argument("--content-types", default="messages", help="Comma-separated: messages,files (default: messages)")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of formatted text")
    args = parser.parse_args()
    asyncio.run(run(args.query, args.limit, args.private, args.content_types, args.json))


if __name__ == "__main__":
    main()
