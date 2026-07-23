#!/usr/bin/env python3
"""Generic Slack MCP dispatcher — exposes ALL of the official Slack MCP server's
tools from the shell, so the CLI can fully stand in for the in-agent MCP.

The readable wrappers (`search.py`, `read_channel.py`, `read_thread.py`,
`read_message.py`) stay the go-to for the common read cases because they format
output and guarantee permalinks. This dispatcher covers everything else —
send/schedule/draft a message, reactions, canvases, user/channel/emoji search,
file + profile reads — without a bespoke wrapper per tool.

Usage:
    python slack.py list                       # list all tools + input schemas
    python slack.py call <tool> '<json-args>'  # invoke any tool with JSON args
    python slack.py call slack_add_reaction '{"channel_id":"C0..","message_ts":"1784..","emoji":"eyes"}'
    python slack.py call slack_search_users '{"query":"Sunil Akella"}'

Tools (18): slack_send_message, slack_schedule_message, slack_add_reaction,
slack_get_reactions, slack_create_canvas, slack_update_canvas, slack_read_canvas,
slack_send_message_draft, slack_search_public, slack_search_public_and_private,
slack_search_channels, slack_search_users, slack_search_emojis, slack_read_channel,
slack_read_thread, slack_read_user_profile, slack_list_channel_members, slack_read_file.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from slack_client import get_cli_client  # noqa: E402


async def run_list(as_json: bool) -> None:
    async with get_cli_client() as client:
        tools = await client.list_tools()
    if as_json:
        print(json.dumps([{"name": t.name, "description": t.description, "input_schema": t.inputSchema} for t in tools], indent=2))
        return
    for t in tools:
        params = ", ".join((t.inputSchema or {}).get("properties", {}).keys())
        print(f"{t.name}({params})\n    {(t.description or '').strip().splitlines()[0] if t.description else ''}")


async def run_call(tool: str, raw_args: str, as_json: bool) -> None:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as exc:
        sys.exit(f"Invalid JSON for tool args: {exc}")
    # Default to detailed responses (guarantees permalinks) unless caller overrode it.
    args.setdefault("response_format", "detailed")

    async with get_cli_client() as client:
        result = await client.call_tool(tool, args)

    raw_text = result.content[0].text if result.content else "{}"
    if as_json:
        try:
            print(json.dumps(json.loads(raw_text), indent=2))
        except json.JSONDecodeError:
            print(raw_text)
        return
    try:
        payload = json.loads(raw_text)
        print(payload.get("messages", payload.get("results", raw_text)))
    except json.JSONDecodeError:
        print(raw_text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic Slack MCP dispatcher — full tool surface from the shell")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List all available Slack MCP tools + schemas")

    call = sub.add_parser("call", help="Invoke any tool by name with JSON args")
    call.add_argument("tool", help="Tool name, e.g. slack_add_reaction")
    call.add_argument("args", nargs="?", default="", help="JSON object of tool arguments")

    args = parser.parse_args()

    if args.cmd == "list":
        asyncio.run(run_list(args.json))
    else:
        asyncio.run(run_call(args.tool, args.args, args.json))


if __name__ == "__main__":
    main()
