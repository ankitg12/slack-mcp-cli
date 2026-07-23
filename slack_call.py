"""slack_call — fast synchronous facade for CLI wrappers.

Hot path (proxy up): speak raw MCP-over-HTTP to the local warm proxy via
`raw_mcp` — **stdlib only, no fastmcp import** — so a one-shot CLI call is ~1.3s
instead of ~6s. Cold path (proxy down): lazily start the daemon; if it comes up,
use the raw path; otherwise fall back to the full FastMCP client (direct remote)
so the CLI ALWAYS works, daemon or not.

Only stdlib + `raw_mcp` are imported at module load. `fastmcp` (via slack_client)
is imported lazily, and only on the fallback branch — keeping the common case fast.
"""
import contextlib

import raw_mcp
import slack_proxy


def _fallback_full_client(name: str, arguments: dict) -> str:
    """Full FastMCP client path (lazy import). Used only when the proxy is
    unavailable — connects DIRECTLY to the remote Slack MCP with persisted OAuth."""
    import asyncio
    from slack_client import get_client

    async def _run() -> str:
        async with get_client() as client:
            result = await client.call_tool(name, arguments)
        return result.content[0].text if result.content else "{}"

    return asyncio.run(_run())


def call_tool(name: str, arguments: dict, *, timeout: float = 30.0, wait_boot: float = 14.0) -> str:
    """Call a Slack MCP tool and return its text content.

    Prefers the raw stdlib path over the warm proxy; lazily boots the daemon if
    down; falls back to the full FastMCP client if the daemon can't be reached.
    `response_format` defaults to detailed (guarantees permalinks) if unset."""
    arguments = {"response_format": "detailed", **arguments}
    if slack_proxy.ensure_up(wait_boot):
        try:
            return raw_mcp.call_tool(slack_proxy.URL, name, arguments, timeout=timeout)
        except raw_mcp.RawMCPError:
            pass  # proxy misbehaved — fall through to full client
    return _fallback_full_client(name, arguments)


@contextlib.contextmanager
def caller(*, timeout: float = 30.0, wait_boot: float = 14.0):
    """Yield a sync `call(name, arguments) -> str` that reuses ONE session across
    many calls — the right shape for paged/batch work (e.g. search). Uses the raw
    warm-proxy session when available; otherwise yields a per-call full-client
    fallback. `response_format` defaults to detailed unless overridden."""
    def _with_default(arguments: dict) -> dict:
        return {"response_format": "detailed", **arguments}

    if slack_proxy.ensure_up(wait_boot):
        try:
            with raw_mcp.RawSession(slack_proxy.URL, timeout) as session:
                yield lambda name, arguments: session.call_tool(name, _with_default(arguments))
                return
        except raw_mcp.RawMCPError:
            pass  # handshake failed — fall through to full client
    yield lambda name, arguments: _fallback_full_client(name, _with_default(arguments))


def list_tools(*, wait_boot: float = 14.0) -> list[dict]:
    """Return the server's tool list as [{name, description, inputSchema}].
    Raw path when the proxy is up; full-client fallback otherwise."""
    if slack_proxy.ensure_up(wait_boot):
        try:
            with raw_mcp.RawSession(slack_proxy.URL) as session:
                return session.list_tools()
        except raw_mcp.RawMCPError:
            pass
    import asyncio
    from slack_client import get_client

    async def _run() -> list[dict]:
        async with get_client() as client:
            tools = await client.list_tools()
        return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]

    return asyncio.run(_run())
