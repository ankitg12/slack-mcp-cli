"""raw_mcp — a tiny, stdlib-only MCP client over Streamable HTTP.

Purpose: talk to the local warm proxy daemon (`slackd.py`) with **zero heavy
imports**. The full FastMCP client is correct but expensive to import + construct
(~6s for a one-shot CLI process); every candidate off-the-shelf minimal client
(minimcp, mcp-streamablehttp-client, the official mcp SDK) sits on the same heavy
`mcp`/pydantic/anyio stack (2.5s+ import), reintroducing the exact tax we're
removing. So for the hot path — a one-shot CLI call against an already-warm
loopback proxy — we speak the wire protocol directly with `urllib`.

Wire protocol (MCP Streamable HTTP, 2025-03-26):
  POST <url> with Accept: application/json, text/event-stream
  1. initialize            -> 200, response as SSE `data:` frame, Mcp-Session-Id header
  2. notifications/initialized (no id)  -> 202, no body
  3. tools/call            -> 200, response as SSE `data:` frame
Responses come back as Server-Sent Events; we parse the last `data:` line as JSON.

This module NEVER imports fastmcp. It is only used when the local proxy is up;
callers fall back to the full client (slack_client.get_client) otherwise.
"""
import json
import urllib.error
import urllib.request

_PROTOCOL_VERSION = "2025-06-18"


class RawMCPError(RuntimeError):
    pass


def _parse_sse(body: str):
    """Return the JSON object from the last SSE `data:` frame, or None."""
    obj = None
    for line in body.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                obj = json.loads(payload)
    return obj


def _post(url: str, body: dict, session_id: str | None, timeout: float):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    out_sid = resp.getheader("Mcp-Session-Id")
    raw = resp.read().decode()
    return resp.status, out_sid, raw


def _content_text(result: dict) -> str:
    content = result.get("content") or []
    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(texts) if texts else json.dumps(result)


class RawSession:
    """One MCP session over the warm proxy: handshake once, call many times.

    Cheap over loopback; the expensive upstream session is held warm by the
    daemon. Use as a context manager. Raises RawMCPError on protocol/transport
    failure so callers can fall back to the full client."""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout
        self.sid: str | None = None

    def __enter__(self) -> "RawSession":
        try:
            _, sid, init_raw = _post(
                self.url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "raw_mcp", "version": "1"},
                    },
                },
                None,
                self.timeout,
            )
            init = _parse_sse(init_raw)
            if not init or "result" not in init:
                raise RawMCPError(f"initialize failed: {init_raw[:200]}")
            self.sid = sid
            _post(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, self.timeout)
        except urllib.error.URLError as exc:
            raise RawMCPError(f"transport error: {exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        # The daemon owns the warm upstream; nothing to tear down our side.
        return None

    def _request(self, method: str, params: dict, req_id: int) -> dict:
        try:
            _, _, raw = _post(
                self.url,
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                self.sid,
                self.timeout,
            )
        except urllib.error.URLError as exc:
            raise RawMCPError(f"transport error: {exc}") from exc
        res = _parse_sse(raw)
        if not res or "result" not in res:
            err = (res or {}).get("error")
            raise RawMCPError(f"{method} failed: {err or raw[:200]}")
        return res["result"]

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call one tool and return its joined text content."""
        return _content_text(self._request("tools/call", {"name": name, "arguments": arguments}, 2))

    def list_tools(self) -> list[dict]:
        """Return the server's tool list ({name, description, inputSchema} each)."""
        return self._request("tools/list", {}, 3).get("tools", [])


def call_tool(url: str, name: str, arguments: dict, timeout: float = 30.0) -> str:
    """Convenience: one-shot handshake + single tool call over the warm proxy."""
    with RawSession(url, timeout) as session:
        return session.call_tool(name, arguments)
