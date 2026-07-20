"""Render Slack MCP 'detailed' markdown results as a self-contained, clickable HTML page.

The Slack MCP server's `detailed` response_format returns a pre-formatted
markdown block (see slack_client.py / README "How results are shaped").
That text is perfectly parseable by an LLM but painful for a *human* to
read in a terminal: raw `[link](url)` markdown doesn't render, walls of
`\n`-escaped text run together, and there's no visual separation between
messages. This module turns that same text into an HTML page a human can
open in a browser — real clickable links, one card per message/reply,
channel/sender/time as a header line, monospace-safe text body.

Used by search.py / read_channel.py / read_thread.py via `--html PATH`.
"""
from __future__ import annotations

import html
import re
import webbrowser
from pathlib import Path

# Mirrors search.py's _RESULT_RE but tolerant of the optional lines that
# read_channel/read_thread results include (Participants, Reply count,
# Reactions) in different combinations.
_RESULT_RE = re.compile(
    r"Channel: (?P<channel>.+?)\n"
    r"(?:Participants: .+?\n)?"
    r"From: (?P<sender>.+?) <(?P<sender_email>.*?)> \(ID: (?P<sender_id>\S+)\)\s*\n"
    r"Time: (?P<time>.+?)\n"
    r"Message_ts: (?P<ts>[\d.]+)\n"
    r"(?:Thread_ts: (?P<thread_ts>[\d.]+)\n)?"
    r"(?:Reply count: \d+\n)?"
    r"Permalink: \[link\]\((?P<permalink>.+?)\)\n"
    r"(?:Reactions: .+?\n)?"
    r"Text: \n(?P<text>.*?)(?=\n---\n|\Z)",
    re.DOTALL,
)

# Three link shapes seen in Slack MCP text, matched together on RAW text so
# none is linkified twice or split across an HTML-escaped delimiter:
#   1. markdown `[label](url)`
#   2. Slack-native `<url|label>` (angle brackets, pipe-separated label)
#   3. a bare `http(s)://...` URL, optionally wrapped in bare `<url>`
_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<url>[^)\s]+)\)"
    r"|<(?P<slack_url>https?://[^\s<>|]+)\|(?P<slack_label>[^<>]+)>"
    r"|(?P<bare><?(?P<bare_url>https?://[^\s()<>|\]]+)>?)"
)


def _linkify(text: str) -> str:
    """Turn markdown/Slack-native links + bare URLs into <a> tags, HTML-escaping the rest.

    Must run on raw (unescaped) text: matching against already-escaped text
    would let a `<url>`-wrapped link absorb the escaped `&gt;` into the href.
    Slack messages sometimes already contain literal HTML entities (e.g. a
    pasted console log with `&gt;`) — unescape those first so they resolve
    to real characters instead of leaking a 4-char `&gt;` into a URL.
    """
    text = html.unescape(text)
    out = []
    pos = 0
    for m in _LINK_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        if m.group("slack_url"):
            url = html.escape(m.group("slack_url"), quote=True)
            label = html.escape(m.group("slack_label"))
            out.append(f'<a href="{url}" target="_blank" rel="noopener">{label}</a>')
        elif m.group("bare_url"):
            url = html.escape(m.group("bare_url"), quote=True)
            out.append(f'<a href="{url}" target="_blank" rel="noopener">{url}</a>')
        else:
            label = html.escape(m.group("label"))
            url = html.escape(m.group("url"), quote=True)
            out.append(f'<a href="{url}" target="_blank" rel="noopener">{label}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def parse_records(markdown: str) -> list[dict]:
    """Parse the MCP 'detailed' markdown block into a list of message records."""
    records = []
    for m in _RESULT_RE.finditer(markdown):
        d = m.groupdict()
        records.append(d)
    return records


_CSS = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 900px;
       margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
h1 { font-size: 1.3rem; }
.meta { color: #767676; font-size: 0.85rem; margin-bottom: 1.5rem; }
.card { border: 1px solid #d0d0d0; border-radius: 8px; padding: 0.9rem 1.1rem;
        margin-bottom: 1rem; background: canvas; }
.card-header { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; align-items: baseline;
               margin-bottom: 0.5rem; }
.channel { font-weight: 600; color: #1264a3; }
.sender { font-weight: 600; }
.time { color: #767676; font-size: 0.85rem; }
.permalink { margin-left: auto; font-size: 0.85rem; }
.text { white-space: pre-wrap; word-wrap: break-word; }
.raw-fallback { white-space: pre-wrap; word-wrap: break-word; background: canvas;
                border: 1px solid #d0d0d0; border-radius: 8px; padding: 1rem; }
a { color: #1264a3; }
.count { color: #767676; }
"""


def render_html(title: str, raw_text: str) -> str:
    """Build a full HTML document from a Slack MCP 'detailed' results block."""
    records = parse_records(raw_text)

    if not records:
        # Unparseable / unexpected shape (e.g. plain "no results" message) —
        # still linkify what's there rather than showing raw markdown.
        body = f'<div class="raw-fallback">{_linkify(raw_text)}</div>'
        count_line = "0 messages parsed (raw output shown below)"
    else:
        cards = []
        for r in records:
            header_bits = [
                f'<span class="channel">{html.escape(r["channel"])}</span>',
                f'<span class="sender">{html.escape(r["sender"])}</span>',
                f'<span class="time">{html.escape(r["time"])}</span>',
            ]
            if r.get("permalink"):
                web_url = html.escape(r["permalink"], quote=True)
                header_bits.append(
                    f'<span class="permalink"><a href="{web_url}" target="_blank" rel="noopener">open in Slack ↗</a></span>'
                )
            header = f'<div class="card-header">{"".join(header_bits)}</div>'
            text = f'<div class="text">{_linkify(r["text"].strip())}</div>'
            cards.append(f'<div class="card">{header}{text}</div>')
        body = "\n".join(cards)
        count_line = f"{len(records)} message(s)"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="meta count">{count_line}</div>
{body}
</body>
</html>
"""


def write_html(title: str, raw_text: str, out_path: Path, open_browser: bool = True) -> Path:
    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(title, raw_text), encoding="utf-8")
    if open_browser:
        webbrowser.open(out_path.as_uri())
    return out_path
