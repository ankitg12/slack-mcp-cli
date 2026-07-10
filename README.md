# slack-mcp-cli

A deterministic Python CLI wrapper around the official hosted [Slack MCP server](https://mcp.slack.com/mcp), built on [FastMCP](https://gofastmcp.com).

## Why

Calling Slack's MCP tools through an LLM agent works, but the output is only as reliable as the model's prompt-following: whether it uses `response_format='detailed'`, and whether it keeps the `Permalink` field when summarizing results, both depend on the model remembering to do so. This is a thin CLI that always requests detailed output and always prints it verbatim — no LLM in the loop, so a link is never silently dropped from a summary and a search always behaves the same way twice.

It reuses the same public OAuth `client_id` that MCP-integrated coding agents (Claude Code, OMP, etc.) already use for their bundled Slack plugin — no new Slack App to register, no admin approval needed, just one browser consent click the first time you run it.

## Install

```bash
pip install fastmcp keyring cryptography
```

(`fastmcp` pulls in `key-value` for the token cache backend.)

## Setup (one-time)

Configure the OAuth `client_id` you want to authenticate as — reuse the one an existing MCP client (Claude Code, OMP, etc.) already uses for its Slack integration (check its `mcp.json` or equivalent config), since that client_id is likely already consented on your workspace. Either an env var:

```bash
export SLACK_MCP_CLIENT_ID=<your-client-id>
```

or a `.slack.conf` file in the repo root (gitignored, never commit it):

```ini
[slack]
client_id = <your-client-id>
```

The first run then opens a browser for OAuth consent against that client_id. After that, the token is cached to an encrypted disk store (`~/.fastmcp/oauth-tokens/slack/`, encryption key held in your OS keyring under the service `fastmcp-slack-wrapper`) — every later run is silent.

**Gotcha:** Slack's OAuth app registration only allows a fixed, pre-registered `redirect_uri`. This defaults to `callback_port=3118` — a mismatched port fails with `redirect_uri did not match any configured URIs`. If your MCP client uses a different callback port for this client_id, set `SLACK_MCP_CALLBACK_PORT` (env var or `callback_port` in `.slack.conf`) to match it.

## Usage

```bash
# Search public channels
python scripts/search.py "deploy failure" --limit 10

# Include private channels/DMs (requires consent scope)
python scripts/search.py "budget" --private --limit 5

# Raw JSON instead of formatted text
python scripts/search.py "incident" --json

# Search files (PDFs, presentations, canvases, images, ...)
python scripts/search.py "roadmap type:pdfs" --private --content-types files

# Read channel history
python scripts/read_channel.py C0123456789 --limit 20
python scripts/read_channel.py C0123456789 --oldest 1783440000.000000 --json

# Read a thread
python scripts/read_thread.py C0123456789 1783440000.000000

# Export mode: walk any query across a long time range, writing incrementally to JSON
python scripts/search.py "from:me" --private --export messages.json --start-days-ago 365
python scripts/search.py "in:#general" --export general.json --window-days 7
```

All of Slack's [search modifiers](https://slack.com/help/articles/202528808) work in the query string: `in:`, `from:`, `after:`, `before:`, `is:thread`, `has:pin`, `type:pdfs`, etc.

### Export mode

There's no official Slack bulk-export API available to a non-admin workspace member — full self-serve export is Enterprise-org-owner-only. `--export` works around this by re-running your query across fixed-size date windows (`before`/`after` on the message timestamp) instead of one continuous search — necessary because the Slack MCP server itself caps cursor pagination at 20 pages (~400 messages) per continuous search session before raising `page_limit_exceeded`. Each window gets its own fresh cursor walk, and results are merged/deduplicated across windows.

The output file is written after **every** window, not just at the end (`"complete": false` while running, `true` once finished) — safe to interrupt at any point without losing already-fetched data.

## How results are shaped

`call_tool(...)` on this server returns a pre-formatted markdown block in `result.content[0].text` (a JSON string of shape `{"results": "..."}`), not per-field structured data — Slack's own "detailed" formatting already embeds the permalink per message, which is exactly what these scripts rely on rather than re-deriving it.

## License

MIT
