#!/usr/bin/env python3
"""slackd — local warm-proxy daemon for the Slack MCP.

Holds ONE persistent, OAuth'd upstream connection to the remote Slack MCP
(`https://mcp.slack.com/mcp`) and re-serves the identical tool surface over
loopback HTTP (`http://127.0.0.1:3119/mcp`). CLI wrappers (via `slack_call` /
`raw_mcp`) then connect to loopback — no per-call TLS handshake, no OAuth token
load, no WAN round-trip to Slack. The expensive setup happens once, here, warm.

Built on FastMCP's first-class proxy (`create_proxy`). We connect ONE client and
keep it entered for the daemon's lifetime; FastMCP's Client is reentrant, and
create_proxy() reuses an already-connected client's session across all requests.

Run (foreground):   python slackd.py
Run (via hub):      hub start name=slackd application=python args=[".../slackd.py"] ready.log="Uvicorn running" ready.port=3119
Health:             any CLI wrapper auto-detects it; or `python slack.py list`
Force direct (bypass): set SLACK_NO_LOCAL_PROXY=1 in the CLI's env.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncio  # noqa: E402
from fastmcp.server import create_proxy  # noqa: E402
import slack_proxy  # noqa: E402
from slack_client import get_client  # noqa: E402


async def main() -> None:
    # Connect ONE upstream client and keep it entered for the daemon's lifetime.
    # FastMCP's Client is reentrant (refcounted), so per-request `async with`
    # inside the proxy reuses this live session instead of re-handshaking.
    # create_proxy() inspects is_connected() AT CONSTRUCTION and, when already
    # connected, builds a "reuse existing session for all requests" factory —
    # exactly the warm-connection behavior we want. So we MUST enter the client
    # BEFORE calling create_proxy().
    client = get_client()
    async with client:
        proxy = create_proxy(client, name="slack-warm-proxy")
        print(
            f"slackd: warm Slack MCP proxy -> {slack_proxy.URL} "
            f"(one persistent upstream OAuth session, reused across calls; Ctrl+C to stop)",
            file=sys.stderr,
            flush=True,
        )
        await proxy.run_async(transport="http", host=slack_proxy.HOST, port=slack_proxy.PORT)


if __name__ == "__main__":
    asyncio.run(main())
