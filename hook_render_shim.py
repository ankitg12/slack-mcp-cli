#!/usr/bin/env python3
"""Stdin/stdout JSON shim used by the `slack-html-render` OMP extension hook.

Reads `{"tool": str, "results": str}` from stdin (the hosted Slack MCP
server's already-fetched `results` markdown block -- no MCP call is made
here, this only renders), writes an HTML report via render.py, and prints
`{"html_path": str, "count": int, "summary": str}` to stdout.

Kept separate from render.py itself so the hook's Node subprocess call has a
single, stable stdin/stdout contract independent of render.py's internal API.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import parse_records, write_html  # noqa: E402

_HTML_DIR = Path(tempfile.gettempdir()) / "slack-mcp-html"


def _summarize(records: list[dict], limit: int = 20) -> str:
    if not records:
        return "No messages found."
    lines = []
    for r in records[:limit]:
        snippet = " ".join(r["text"].strip().split())[:200]
        lines.append(f"- [{r['channel']} \u00b7 {r['sender']} \u00b7 {r['time']}]({r['permalink']}): {snippet}")
    if len(records) > limit:
        lines.append(f"... and {len(records) - limit} more (see html_path for all)")
    return "\n".join(lines)


def main() -> None:
    payload = json.loads(sys.stdin.read())
    tool = payload["tool"]
    results = payload["results"]

    records = parse_records(results)
    _HTML_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _HTML_DIR / f"{tool}-{int(time.time() * 1000)}.html"
    out = write_html(f"Slack ({tool})", results, out_path, open_browser=True)

    json.dump({"html_path": str(out), "count": len(records), "summary": _summarize(records)}, sys.stdout)


if __name__ == "__main__":
    main()
