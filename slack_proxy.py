"""slack_proxy — single source of truth for the local warm-proxy daemon.

Owns the proxy's address, a cheap liveness probe, and the self-healing detached
autostart. Imported by both the fast facade (`slack_call`) and the daemon
(`slackd`). **Stdlib only** — no fastmcp — so importing it stays cheap on the
CLI hot path.

The daemon (`slackd.py`) holds ONE persistent OAuth'd upstream connection to the
remote Slack MCP and re-serves it over loopback, so CLI calls avoid a fresh
TLS+OAuth+initialize handshake to mcp.slack.com each time.
"""
import configparser
import os
import pathlib
import socket
import subprocess
import sys
import time

_REPO = pathlib.Path(__file__).resolve().parent


def _conf() -> dict:
    """Minimal stdlib read of .slack.conf [slack] — proxy host/port only."""
    path = _REPO / ".slack.conf"
    if path.exists():
        parser = configparser.ConfigParser()
        parser.read(path)
        if parser.has_section("slack"):
            return dict(parser.items("slack"))
    return {}


_CONF = _conf()
HOST = os.environ.get("SLACK_PROXY_HOST") or _CONF.get("proxy_host") or "127.0.0.1"
PORT = int(os.environ.get("SLACK_PROXY_PORT") or _CONF.get("proxy_port") or "3119")
URL = f"http://{HOST}:{PORT}/mcp"


def is_up() -> bool:
    """Cheap loopback probe: is the proxy accepting connections?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex((HOST, PORT)) == 0


def start() -> None:
    """Spawn slackd.py as a DETACHED daemon (self-healing, survives this process).

    Mirrors ~/tools/shorten.py: the first caller that finds the port closed
    starts the daemon (no console window, detached), so it's "always running"
    thereafter and self-heals if it ever dies. Best-effort."""
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
        pass  # best-effort; direct fallback still works


def ensure_up(wait_boot: float = 14.0) -> bool:
    """True if the warm proxy is usable. Lazily boots the daemon (unless
    SLACK_NO_LOCAL_PROXY is set) and waits up to wait_boot seconds for it."""
    if os.environ.get("SLACK_NO_LOCAL_PROXY"):
        return False
    if is_up():
        return True
    start()
    deadline = time.time() + wait_boot
    while time.time() < deadline and not is_up():
        time.sleep(0.4)
    return is_up()
