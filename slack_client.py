"""Shared Slack MCP client helper.

Wraps the official hosted Slack MCP server (https://mcp.slack.com/mcp) via
FastMCP, reusing an already-approved public OAuth client_id (PKCE, no client
secret) — no separate Slack App to create or get admin-approved, as long as
some MCP client using that client_id (Claude Code, OMP, Claude Desktop,
etc.) is already installed and you've consented once.

Slack's OAuth app registration only allows a fixed, pre-registered
redirect_uri — a random localhost port fails with "redirect_uri did not
match any configured URIs". Set SLACK_MCP_CALLBACK_PORT to match whatever
port your MCP client already uses for this client_id (check its mcp.json
or equivalent config); defaults to 3118, a common value for this
integration.

Tokens are cached to an encrypted disk store so the browser consent is only
needed once. The Fernet encryption key itself is stored in the OS keyring
via the `keyring` package, never written to disk in plaintext.

Configuration — env vars take precedence, falling back to a `.slack.conf`
INI file in the repo root (`[slack]` section: `client_id`, `mcp_url`,
`callback_port`), consistent with the `~/.slack.conf` convention used by
other Slack tooling. `.slack.conf` is gitignored — never commit it.
    SLACK_MCP_CLIENT_ID      required (here or in .slack.conf) — the OAuth client_id (see above)
    SLACK_MCP_URL            optional — defaults to https://mcp.slack.com/mcp
    SLACK_MCP_CALLBACK_PORT  optional — defaults to 3118

Usage (see scripts/ for CLI entry points):
    import asyncio
    from slack_client import get_client

    async def main():
        async with get_client() as client:
            result = await client.call_tool("slack_search_public", {"query": "..."})
            print(result.content[0].text)

    asyncio.run(main())
"""
import configparser
import os
import pathlib
import sys

import keyring
from cryptography.fernet import Fernet
from fastmcp import Client
from fastmcp.client.auth import OAuth
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper


def _load_config() -> dict:
    """Read `.slack.conf` (INI, `[slack]` section) from the repo root, if present."""
    conf_path = pathlib.Path(__file__).resolve().parent / ".slack.conf"
    parser = configparser.ConfigParser()
    if conf_path.exists():
        parser.read(conf_path)
    return dict(parser["slack"]) if parser.has_section("slack") else {}


_config = _load_config()

SLACK_MCP_URL = os.environ.get("SLACK_MCP_URL") or _config.get("mcp_url") or "https://mcp.slack.com/mcp"

# Public PKCE client_id used by the official Slack MCP integration shipped
# with various MCP clients. Not hardcoded here since it may differ per
# integration/workspace — find yours in your MCP client's own config.
SLACK_CLIENT_ID = os.environ.get("SLACK_MCP_CLIENT_ID") or _config.get("client_id")
if not SLACK_CLIENT_ID:
    sys.exit(
        "No Slack MCP client_id configured. Find the client_id your MCP client "
        "(Claude Code, OMP, etc.) already uses for its Slack integration "
        "(check its mcp.json or equivalent config), then either:\n"
        "  export SLACK_MCP_CLIENT_ID=<your-client-id>\n"
        "or add it to .slack.conf:\n"
        "  [slack]\n"
        "  client_id = <your-client-id>"
    )

SLACK_CALLBACK_PORT = int(os.environ.get("SLACK_MCP_CALLBACK_PORT") or _config.get("callback_port") or "3118")  # must match the allowlisted redirect_uri

# Local warm-proxy daemon (slackd.py): holds ONE persistent OAuth'd upstream
# connection to the remote Slack MCP and re-serves it over loopback, so CLI
# calls avoid a fresh TLS+OAuth+initialize handshake to mcp.slack.com each time.
SLACK_PROXY_HOST = os.environ.get("SLACK_PROXY_HOST") or _config.get("proxy_host") or "127.0.0.1"
SLACK_PROXY_PORT = int(os.environ.get("SLACK_PROXY_PORT") or _config.get("proxy_port") or "3119")
SLACK_PROXY_URL = f"http://{SLACK_PROXY_HOST}:{SLACK_PROXY_PORT}/mcp"

_TOKEN_DIR = pathlib.Path.home() / ".fastmcp" / "oauth-tokens" / "slack"
_KEYRING_SERVICE = "fastmcp-slack-wrapper"
_KEYRING_USER = "oauth-encryption-key"


def _get_or_create_encryption_key() -> bytes:
    """Fetch the Fernet key from the OS keyring, minting one on first run."""
    existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    if existing:
        return existing.encode()
    key = Fernet.generate_key()
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key.decode())
    return key


def _token_storage() -> FernetEncryptionWrapper:
    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    return FernetEncryptionWrapper(
        key_value=DiskStore(directory=str(_TOKEN_DIR)),
        fernet=Fernet(_get_or_create_encryption_key()),
    )



def _make_oauth() -> OAuth:
    """Build the shared OAuth handler (persistent, Fernet-encrypted token cache)."""
    return OAuth(
        mcp_url=SLACK_MCP_URL,
        client_id=SLACK_CLIENT_ID,
        token_storage=_token_storage(),
        callback_port=SLACK_CALLBACK_PORT,
    )


def get_client() -> Client:
    """Return a FastMCP Client connected DIRECTLY to the remote Slack MCP with persistent OAuth.

    First call opens a browser for one-time consent; subsequent calls reuse
    the cached, encrypted token silently. Each `async with` opens a fresh
    remote connection (TLS + MCP initialize). For repeated CLI calls prefer
    `get_cli_client()`, which reuses a warm local proxy when the daemon is up.
    The proxy daemon (`slackd.py`) itself MUST use this direct client.
    """
    return Client(SLACK_MCP_URL, auth=_make_oauth())


def _local_proxy_up() -> bool:
    """Cheap loopback probe: is the local Slack MCP proxy accepting connections?"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex((SLACK_PROXY_HOST, SLACK_PROXY_PORT)) == 0


def _ensure_proxy_started() -> None:
    """Lazily spawn slackd.py as a DETACHED daemon if it isn't already up.

    Mirrors ~/tools/shorten.py's self-healing pattern: the first CLI call that
    finds the port closed starts the daemon (surviving this process's exit, no
    console window), so it's "always running" thereafter and self-heals if it
    ever dies. Non-blocking — we don't wait for boot; the spawning call itself
    goes direct and the daemon is warm for the next call."""
    import subprocess
    daemon = os.path.join(os.path.dirname(__file__), "scripts", "slackd.py")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [sys.executable, daemon],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # best-effort; direct fallback still works


def get_cli_client() -> Client:
    """Client for CLI wrappers. Prefers the warm local proxy (loopback, no TLS/OAuth
    per call); if the proxy daemon isn't running, lazily auto-starts it (detached,
    self-healing — like the URL-shortener server) and serves THIS call directly so
    there's no boot wait. Set SLACK_NO_LOCAL_PROXY=1 to force direct and skip autostart."""
    if os.environ.get("SLACK_NO_LOCAL_PROXY"):
        return get_client()
    if _local_proxy_up():
        return Client(SLACK_PROXY_URL)
    _ensure_proxy_started()
    return get_client()
