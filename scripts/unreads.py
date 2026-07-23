#!/usr/bin/env python3
"""
slack-unreads — read your current Slack unread badges from the terminal.

Why this exists: the hosted Slack MCP server (what ~/tools/slack wraps) exposes
search/read/send but *no* unread capability. The only way to get the live red-badge
state is the private `client.counts` endpoint that Slack's own UI uses — which needs a
browser/app *session* token (xoxc) + cookie (xoxd), not an OAuth/bot token.

How it works:
  1. Workspace tokens (xoxc, one per workspace) are read live from the Slack desktop
     app's Local Storage LevelDB (`localConfig_v2`) via the vendored ccl_leveldb reader.
  2. The `d` and `d-s` cookies (xoxd) are DPAPI-decrypted from the app's Cookies DB.
     The Cookies DB is exclusively locked while Slack runs, so cookies are extracted
     once (auto-closing Slack briefly) and cached to a mode-600 file; every later run
  3. `client.counts` per org returns has_unreads + mention_count per conversation.
     Counts-only is the DEFAULT and is safe (one call per org, same as the browser).
     Name resolution (--names) loops conversations.info/users.info and can trip
     Slack's anomaly detection into signing you out of the org — opt-in, use sparingly.
  All HTTP goes through curl_cffi impersonating Chrome's TLS/JA3 fingerprint, since
  Slack inspects the TLS ClientHello (not just User-Agent) to detect non-browser traffic.

Usage:
    python unreads.py                 # unread + mention counts across all workspaces
    python unreads.py --mentions      # only conversations where you're mentioned
    python unreads.py --names         # resolve channel/DM names (RISKY, see above)
    python unreads.py --json          # machine-readable
    python unreads.py --refresh       # re-extract cookie (needs Slack quit first)

Windows only (DPAPI). Requires: pywin32, pycryptodome, curl_cffi.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ccl_leveldb  # vendored: CCL Forensics Chromium LevelDB reader

APP_DIR = os.path.expandvars(r"%APPDATA%\Slack")
LEVELDB_DIR = os.path.join(APP_DIR, "Local Storage", "leveldb")
LOCAL_STATE = os.path.join(APP_DIR, "Local State")
COOKIES_DB = os.path.join(APP_DIR, "Network", "Cookies")
if not os.path.exists(COOKIES_DB):
    COOKIES_DB = os.path.join(APP_DIR, "Cookies")
SLACK_EXE = os.path.expandvars(r"%LOCALAPPDATA%\slack\slack.exe")
CREDS_CACHE = os.path.join(os.path.expanduser("~"), ".slack_unreads_creds.json")


# ------------------------------- tokens (live) -------------------------------
def _parse_localconfig(val: bytes) -> dict:
    """Decode a Chromium local-storage localConfig_v2 value to its JSON object."""
    # byte0 is the encoding marker (0=utf16le, 1=latin1)
    text = val[1:].decode("utf-16-le") if val[0] == 0 else val[1:].decode("latin-1", "ignore")
    s = text.find("{")
    depth, end = 0, -1
    for i in range(s, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(text[s:end])


def read_workspace_tokens() -> list[dict]:
    """Read every signed-in workspace's xoxc token from the app's LevelDB.

    LevelDB reads fine while Slack is running (shared-read .ldb files)."""
    db = ccl_leveldb.RawLevelDb(LEVELDB_DIR)
    try:
        candidates = [
            rec for rec in db.iterate_records_raw()
            if rec.user_key and b"localConfig_v2" in rec.user_key
            and rec.state == ccl_leveldb.KeyState.Live and rec.value
        ]
    finally:
        db.close()
    if not candidates:
        raise RuntimeError("localConfig_v2 not found — is the Slack desktop app installed/signed in?")
    # Pick the record yielding the MOST teams. A blindly-highest-seq pick can land
    # on a transient logged-out/in-progress snapshot (teams=1) written during a
    # login churn; the healthy full snapshot is what we want.
    best_cfg, best_n, best_seq = None, -1, -1
    for rec in candidates:
        try:
            cfg = _parse_localconfig(rec.value)
        except Exception:
            continue
        n = sum(1 for t in cfg.get("teams", {}).values() if t.get("token"))
        if n > best_n or (n == best_n and rec.seq > best_seq):
            best_cfg, best_n, best_seq = cfg, n, rec.seq
    if best_cfg is None:
        raise RuntimeError("could not parse any localConfig_v2 record")
    cfg = best_cfg
    out = []
    for tid, t in cfg.get("teams", {}).items():
        if t.get("token"):
            out.append({
                "id": tid,
                "name": t.get("name") or tid,
                "url": (t.get("url") or "").rstrip("/"),
                "domain": t.get("domain"),
                "token": t["token"],
                "enterprise_id": t.get("enterprise_id"),
                "enterprise_name": t.get("enterprise_name"),
                "enterprise_api_token": t.get("enterprise_api_token"),
            })
    return out


# ------------------------------- cookies -------------------------------------
def _dpapi_aes_key() -> bytes:
    import win32crypt
    with open(LOCAL_STATE, encoding="utf-8") as f:
        enc = base64.b64decode(json.load(f)["os_crypt"]["encrypted_key"])[5:]  # strip 'DPAPI'
    return win32crypt.CryptUnprotectData(enc, None, None, None, 0)[1]


def _decrypt_cookie(buf: bytes, key: bytes) -> str:
    from Crypto.Cipher import AES
    import win32crypt
    if buf[:3] in (b"v10", b"v11"):
        nonce, ct, tag = buf[3:15], buf[15:-16], buf[-16:]
        pt = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
        # Chromium prepends a 32-byte SHA256 domain hash before the value
        return (pt[32:] if len(pt) > 32 else pt).decode("utf-8", "replace")
    return win32crypt.CryptUnprotectData(buf, None, None, None, 0)[1].decode("utf-8", "replace")


def _slack_running() -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq slack.exe", "/NH"],
        capture_output=True, text=True).stdout.lower()
    return "slack.exe" in out


def _stop_slack_graceful() -> bool:
    """Ask Slack to close via WM_CLOSE (no /F). Returns True if it actually exited.

    NOTE: Slack usually minimizes to tray on WM_CLOSE rather than exiting, so this
    often returns False. We NEVER force-kill (/F) — a forced kill mid-write can log
    the user out of all workspaces (learned the hard way)."""
    subprocess.run(["taskkill", "/IM", "slack.exe"], capture_output=True, text=True)
    for _ in range(8):
        if not _slack_running():
            return True
        time.sleep(0.5)
    return False


def _read_cookies_from_db() -> dict:
    """Copy the (unlocked) Cookies DB and decrypt the d / d-s cookies."""
    key = _dpapi_aes_key()
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "Cookies")
    shutil.copy2(COOKIES_DB, dst)
    con = sqlite3.connect(dst)
    try:
        rows = con.execute(
            "SELECT name, encrypted_value FROM cookies WHERE name IN ('d','d-s')"
        ).fetchall()
    finally:
        con.close()
    cookies = {name: _decrypt_cookie(ev, key) for name, ev in rows}
    if "d" not in cookies:
        raise RuntimeError("d cookie not found in Slack Cookies DB")
    return cookies


def _load_cache() -> dict | None:
    if not os.path.exists(CREDS_CACHE):
        return None
    try:
        with open(CREDS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(cookies: dict):
    data = {"cookies": cookies, "extracted_at": time.time()}
    with open(CREDS_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(CREDS_CACHE, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        pass


class SlackRunning(RuntimeError):
    """Raised when cookies must be extracted but Slack is holding the DB open."""


def get_cookies(refresh: bool = False, force_close: bool = False,
                quiet: bool = False) -> dict:
    """Return {d: ...} cookies. Uses the mode-600 cache when present.

    Extraction requires the Cookies DB to be unlocked, i.e. Slack fully quit.
    We do NOT force-kill Slack by default (that can log you out). If Slack is
    running and no cache exists, we ask the user to Quit it (Ctrl+Q). Passing
    force_close=True attempts a graceful (non-/F) close, which usually fails
    because Slack minimizes to tray."""
    if not refresh:
        cache = _load_cache()
        if cache and cache.get("cookies", {}).get("d"):
            return cache["cookies"]
    if _slack_running():
        if force_close and _stop_slack_graceful():
            pass  # Slack actually exited; DB is now readable
        else:
            raise SlackRunning(
                "Slack is running, so its Cookies DB is locked. This one-time cookie "
                "grab needs Slack fully quit.\n"
                "  → Right-click the Slack tray icon (or Ctrl+Q in Slack) to Quit, "
                "then rerun this command.\n"
                "  (The cookie is cached afterwards; you won't need to quit again.)")
    cookies = _read_cookies_from_db()
    _save_cache(cookies)
    return cookies


# ------------------------------- Slack API -----------------------------------
def _session(cookies: dict):
    # curl_cffi impersonates a real Chrome TLS/JA3 fingerprint + header order.
    # Slack's anomaly detection inspects the TLS ClientHello, not just the
    # User-Agent, so plain `requests` traffic reads as scraping and can sign you
    # out of the whole Enterprise-Grid org (korotovsky/slack-mcp-server#86).
    from curl_cffi import requests as cffi
    s = cffi.Session(impersonate="chrome")
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".slack.com")
    return s


def _api(sess, url: str, method: str, token: str, **data):
    data["token"] = token
    r = sess.post(f"{url}/api/{method}", data=data, timeout=25)
    return r.json()


def resolve_name(sess, url, token, conv_id, kind, _user_cache):
    """Resolve a conversation id to a human label; best-effort."""
    try:
        info = _api(sess, url, "conversations.info", token, channel=conv_id)
        if not info.get("ok"):
            return conv_id
        ch = info["channel"]
        if ch.get("is_im"):
            uid = ch.get("user")
            if uid and uid not in _user_cache:
                u = _api(sess, url, "users.info", token, user=uid)
                _user_cache[uid] = (u.get("user", {}).get("real_name")
                                    or u.get("user", {}).get("name") or uid) if u.get("ok") else uid
            return "@" + str(_user_cache.get(uid, uid))
        name = ch.get("name") or conv_id
        if ch.get("is_mpim"):
            return name  # already like "mpdm-a--b--c"
        return "#" + name
    except Exception:
        return conv_id


def build_targets(workspaces, sess):
    """Collapse workspaces into unique query targets, deduped by org URL.

    Enterprise-Grid member workspaces reject client.counts on their own URL
    (team_is_restricted); their unreads are aggregated at the org URL. The org
    itself usually appears as its own signed-in workspace (e.g. amd.enterprise
    .slack.com) whose team token both works AND resolves names well — so we
    prefer it. We therefore:
      1. add every standalone workspace (no enterprise_id) keyed by its URL, and
      2. for each enterprise, add ONE target via enterprise_api_token only if its
         org URL wasn't already covered in step 1.
    Member-workspace URLs are never queried directly."""
    by_url = {}           # url -> target dict
    ent_url_cache = {}    # enterprise_id -> (url, token)
    # pass 1: standalone workspaces
    for ws in workspaces:
        if ws.get("enterprise_id"):
            continue
        url = ws["url"]
        if url and url not in by_url:
            by_url[url] = {"name": ws["name"], "url": url,
                           "token": ws["token"], "domain": ws.get("domain")}
    # pass 2: enterprises not already covered
    for ws in workspaces:
        eid = ws.get("enterprise_id")
        etok = ws.get("enterprise_api_token")
        if not (eid and etok) or eid in ent_url_cache:
            continue
        r = sess.post("https://slack.com/api/auth.test",
                      data={"token": etok}, timeout=20).json()
        url = (r.get("url") or "").rstrip("/")
        ent_url_cache[eid] = (url, etok)
        if url and url not in by_url:
            by_url[url] = {"name": ws.get("enterprise_name") or ws["name"],
                           "url": url, "token": etok,
                           "domain": url.replace("https://", "").rstrip("/")}
    return list(by_url.values())


def gather(workspaces, cookies, want_names=True):
    sess = _session(cookies)
    results = []
    for tgt in build_targets(workspaces, sess):
        r = _api(sess, tgt["url"], "client.counts", tgt["token"])
        if not r.get("ok"):
            results.append({**tgt, "error": r.get("error")})
            continue
        convs = []
        for kind in ("channels", "mpims", "ims"):
            for c in r.get(kind, []):
                if c.get("has_unreads") or c.get("mention_count"):
                    convs.append({"id": c["id"], "kind": kind,
                                  "mentions": c.get("mention_count", 0)})
        user_cache = {}
        if want_names:
            for c in convs:
                c["name"] = resolve_name(sess, tgt["url"], tgt["token"], c["id"],
                                         c["kind"], user_cache)
        convs.sort(key=lambda c: (-c["mentions"], c.get("name", c["id"])))
        results.append({**tgt, "unreads": convs,
                        "total_mentions": sum(c["mentions"] for c in convs)})
    return results


# ------------------------------- CLI -----------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Read Slack unread badges from the terminal.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--mentions", action="store_true",
                    help="only show conversations where you're mentioned")
    ap.add_argument("--refresh", action="store_true",
                    help="force cookie re-extraction (needs Slack quit)")
    ap.add_argument("--force-close", action="store_true",
                    help="attempt a graceful (non-forced) Slack close for extraction")
    ap.add_argument("--names", action="store_true",
                    help="resolve channel/DM names (RISKY: the per-conversation "
                         "lookup loop can trip Slack anomaly detection and sign you "
                         "out of the org — use sparingly)")
    args = ap.parse_args()

    workspaces = read_workspace_tokens()
    try:
        cookies = get_cookies(refresh=args.refresh, force_close=args.force_close,
                              quiet=args.json)
    except SlackRunning as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(2)
    results = gather(workspaces, cookies, want_names=args.names)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    total_unread = total_ment = 0
    for ws in results:
        if ws.get("error"):
            print(f"\n\033[90m{ws['name']}  —  ({ws['error']})\033[0m")
            continue
        convs = ws["unreads"]
        if args.mentions:
            convs = [c for c in convs if c["mentions"] > 0]
        if not convs:
            continue
        n_ment = sum(c["mentions"] for c in convs)
        total_unread += len(convs)
        total_ment += n_ment
        head = f"\n\033[1m{ws['name']}\033[0m  ({ws['domain'] or ''})"
        head += f"  —  {len(convs)} unread"
        if n_ment:
            head += f", \033[31m\u2691 {n_ment} mentions\033[0m"
        print(head)
        for c in convs:
            label = c.get("name", c["id"])
            if c["mentions"]:
                print(f"  \033[31m\u2691{c['mentions']:>3}\033[0m  {label}")
            else:
                print(f"   \033[90m\u2022\033[0m    {label}")

    if total_unread == 0:
        print("\n\u2728 No unreads. Inbox zero." if not args.mentions
              else "\n\u2728 No mentions.")
    else:
        print(f"\n\033[1mTotal:\033[0m {total_unread} unread conversations, "
              f"\033[31m{total_ment} mentions\033[0m")


if __name__ == "__main__":
    main()
