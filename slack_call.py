"""slack_call — fast synchronous facade for CLI wrappers.

Hot path (proxy up): speak raw MCP-over-HTTP to the local warm proxy via
`raw_mcp` — **stdlib only, no fastmcp import** — so a one-shot CLI call is ~1.3s
instead of ~6s. Cold path (proxy down): lazily start the daemon; if it comes up,
use the raw path; otherwise fall back to the full FastMCP client (direct remote)
so the CLI ALWAYS works, daemon or not.

Only stdlib + `raw_mcp` are imported at module load. `fastmcp` (via slack_client)
is imported lazily, and only on the fallback branch — keeping the common case fast.
"""
import configparser
import os
import pathlib
import socket
import subprocess
import sys
import time

import raw_mcp

_REPO = pathlib.Path(__file__).resolve().parent


def _conf() -> dict:
    """Minimal stdlib read of .slack.conf [slack] — proxy host/port only.

    (The full config authority lives in slack_client; we duplicate just the two
    proxy values here to avoid importing fastmcp on the hot path.)"""
    cfg = {}
    path = _REPO / ".slack.conf"
    if path.exists():
        parser = configparser.ConfigParser()
        parser.read(path)
        if parser.has_section("slack"):
            cfg = dict(parser.items("slack"))
    return cfg


_CONF = _conf()
PROXY_HOST = os.environ.get("SLACK_PROXY_HOST") or _CONF.get("proxy_host") or "127.0.0.1"
PROXY_PORT = int(os.environ.get("SLACK_PROXY_PORT") or _CONF.get("proxy_port") or "3119")
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}/mcp"


def _proxy_up() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex((PROXY_HOST, PROXY_PORT)) == 0


def _start_proxy() -> None:
    """Spawn slackd.py detached (self-healing, survives this process)."""
    daemon = _REPO / "scripts" / "slackd.py"
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [sys.executable, str(daemon)],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _fallback_full_client(name: str, arguments: dict) -> str:
    """Full FastMCP client path (lazy import). Used only when the proxy is
    unavailable. Reuses slack_client.get_cli_client (proxy-preferring + direct)."""
    import asyncio
    from slack_client import get_cli_client

    async def _run() -> str:
        async with get_cli_client() as client:
            result = await client.call_tool(name, arguments)
        return result.content[0].text if result.content else "{}"

    return asyncio.run(_run())


def call_tool(name: str, arguments: dict, *, timeout: float = 30.0, wait_boot: float = 14.0) -> str:
    """Call a Slack MCP tool and return its text content.

    Prefers the raw stdlib path over the warm proxy; lazily boots the daemon if
    down; falls back to the full FastMCP client if the daemon can't be reached.
    `response_format` defaults to detailed (guarantees permalinks) if unset."""
    arguments = {"response_format": "detailed", **arguments}

    if os.environ.get("SLACK_NO_LOCAL_PROXY"):
        return _fallback_full_client(name, arguments)

    if not _proxy_up():
        _start_proxy()
        deadline = time.time() + wait_boot
        while time.time() < deadline and not _proxy_up():
            time.sleep(0.4)

    if _proxy_up():
        try:
            return raw_mcp.call_tool(PROXY_URL, name, arguments, timeout=timeout)
        except raw_mcp.RawMCPError:
            pass  # proxy misbehaved — fall through to full client

    return _fallback_full_client(name, arguments)
