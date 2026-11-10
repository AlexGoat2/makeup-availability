"""Watch de Bijenkorf store stock for a product and notify via Telegram.

How it works
------------
de Bijenkorf sits behind Cloudflare bot protection, so the stock is fetched by
a small Chrome extension (see ./extension) from inside a normal Bijenkorf tab
in your own browser, every 15 minutes. The extension posts the result to this
script on http://127.0.0.1:8765. This script then:

  * logs every check to stock.log
  * sends a Telegram message when the status of a watched store changes
  * sends a daily "opening" and "closing" update, based on the stores' hours
  * answers /stock in Telegram immediately with the latest check
  * warns you if the browser stops reporting during opening hours

Usage:
    python main.py                     # run the receiver + Telegram bot (keep it open)
    python main.py --status            # print the latest saved check
    python main.py --get-chat-id       # show chat ids that messaged your bot
    python main.py --test-telegram     # send a test message
    python main.py --register-commands # add /stock to the bot's menu
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

try:  # certifi gives a CA bundle that works on Windows too (Telegram's root CA
    import certifi  # is missing from the Windows store Python reads)

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent
LINK_FILE = ROOT / "link.txt"
STATE_FILE = ROOT / "state.json"
SECRETS_FILE = ROOT / "secrets.json"
LOG_FILE = ROOT / "stock.log"

TZ = ZoneInfo("Europe/Amsterdam")
HOST, PORT = "127.0.0.1", 8765
WATCHED_STORES = ("Eindhoven", "Utrecht")
CHECK_INTERVAL_MIN = 15  # how often the extension checks
STALE_AFTER = timedelta(minutes=40)  # no report for this long (while open) -> warning
ERROR_ALERT_AFTER = 4  # consecutive failed checks before a Telegram warning

STOCK_QUERY = """query ProductStoreStock {
  productStoreStock(productVariantCode: "%s") {
    status
    stock
    stockStatusDescription
    store {
      name
      openingHours { date timeRange { from until } }
    }
  }
}"""

STATUS_EMOJI = {
    "IN_STOCK": "\U0001f7e2",  # green circle
    "OUT_OF_STOCK": "\U0001f534",  # red circle
    "NOT_SOLD_IN_STORE": "⚪",  # white circle
    "UNKNOWN": "❔",  # grey question mark
}

log = logging.getLogger("stock")
state_lock = threading.Lock()


def now() -> datetime:
    return datetime.now(TZ)


def setup_logging() -> None:
    fmt = logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


# --------------------------------------------------------------------------- config


def load_secrets() -> tuple[str | None, str | None]:
    """Telegram credentials from env vars, falling back to secrets.json."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if SECRETS_FILE.exists():
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        token = token or data.get("telegram_bot_token")
        chat_id = chat_id or data.get("telegram_chat_id")
    return token, (str(chat_id) if chat_id else None)


def read_product_url() -> str:
    return LINK_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()


def variant_code_from_url(url: str) -> str:
    """The variant code is the trailing number of the product URL slug."""
    match = re.search(r"-(\d{10,})/?(?:[?#].*)?$", url)
    if not match:
        raise ValueError(f"Could not find a product variant code in URL: {url}")
    return match.group(1)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- telegram


def post_json(url: str, payload: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def telegram_api(method: str, payload: dict | None = None, http_timeout: int = 20) -> dict:
    token, _ = load_secrets()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set (env var or secrets.json)")
    return post_json(f"https://api.telegram.org/bot{token}/{method}", payload or {}, timeout=http_timeout)


def send_telegram(text: str) -> bool:
    token, chat_id = load_secrets()
    if not token or not chat_id:
        log.info("Telegram not configured; message would have been:\n%s", text)
        return False
    try:
        telegram_api("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
        })
        return True
    except (urllib.error.URLError, RuntimeError, ValueError, TimeoutError) as exc:
        # Never log the URL: it contains the bot token.
        log.info("Telegram send failed: %s: %s", type(exc).__name__, getattr(exc, "reason", exc))
        return False


def poll_updates(timeout: int) -> list[dict]:
    """Long-poll for bot messages and mark them as handled on Telegram's side."""
    try:
        updates = telegram_api("getUpdates", {"timeout": timeout}, http_timeout=timeout + 15).get("result", [])
        if updates:  # confirming an offset tells Telegram to drop everything before it
            telegram_api("getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0, "limit": 1})
        return updates
    except (urllib.error.URLError, RuntimeError, ValueError, TimeoutError, OSError) as exc:
        log.info("Reading Telegram messages failed: %s", type(exc).__name__)
        time.sleep(10)
        return []


def is_stock_request(update: dict, chat_id: str | None) -> bool:
    msg = update.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != chat_id:  # only answer the owner
        return False
    text = (msg.get("text") or "").strip().lower().split("@")[0]
    return text in ("/stock", "/start", "stock", "voorraad")


def print_chat_ids() -> None:
    chats = {}
    for u in telegram_api("getUpdates").get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("No messages found. Send any message to your bot in Telegram, then retry.")
    for cid, name in chats.items():
        print(f"chat_id={cid}  ({name})")


# --------------------------------------------------------------------------- formatting


def product_link() -> str:
    return f'<a href="{read_product_url()}">Open product page</a>'


def fmt_store(name: str, info: dict | None) -> str:
    if info is None:
        return f"❔ <b>{name}</b>: not listed"
    emoji = STATUS_EMOJI.get(info["status"], "❔")
    return f"{emoji} <b>{name}</b>: {info['description']}"


def store_lines(current: dict, previous: dict | None = None, changed=()) -> list[str]:
    lines = []
    for name, info in current.items():
        line = fmt_store(name, info)
        if previous is not None and name in changed:
            old = previous.get(name)
            line += f"  (was: {old['description'] if old else 'not listed'})"
        lines.append(line)
    return lines


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    t = datetime.fromisoformat(iso)
    mins = int((now() - t).total_seconds() // 60)
    when = f"{t:%H:%M}" if t.date() == now().date() else f"{t:%d-%m %H:%M}"
    return f"{when} ({mins} min ago)" if mins < 120 else when


# --------------------------------------------------------------------------- stock logic


def parse_stock(data: dict) -> dict[str, dict]:
    """GraphQL response -> {store: {status, stock, description, hours{date: [from, until]}}}."""
    if data.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in data["errors"]))
    entries = (data.get("data") or {}).get("productStoreStock") or []
    if not entries:
        raise RuntimeError("Empty productStoreStock response")
    result = {}
    for e in entries:
        hours = {}
        for oh in e["store"].get("openingHours") or []:
            tr = oh.get("timeRange") or {}
            if oh.get("date") and tr.get("from") and tr.get("until"):
                hours[oh["date"]] = [tr["from"], tr["until"]]
        result[e["store"]["name"]] = {
            "status": e["status"],
            "stock": e["stock"],
            "description": e["stockStatusDescription"],
            "hours": hours,
        }
    return result


def todays_window(hours_by_store: dict, today: date) -> tuple[datetime, datetime] | None:
    """(first opening, last closing) today across the watched stores, or None if all closed."""
    spans = [h for name in WATCHED_STORES if (h := (hours_by_store.get(name) or {}).get(today.isoformat()))]
    if not spans:
        return None
    return (
        datetime.combine(today, min(dtime.fromisoformat(s[0]) for s in spans), TZ),
        datetime.combine(today, max(dtime.fromisoformat(s[1]) for s in spans), TZ),
    )


def hours_text(hours_by_store: dict, today: date) -> str:
    parts = []
    for name in WATCHED_STORES:
        h = (hours_by_store.get(name) or {}).get(today.isoformat())
        parts.append(f"{name} {h[0]}–{h[1]}" if h else f"{name} closed")
    return ", ".join(parts)


def record_failure(state: dict, error: str) -> None:
    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
    state["last_report"] = now().isoformat(timespec="seconds")
    state["last_error"] = error
    log.info("Check failed (%dx): %s", state["consecutive_errors"], error)
    if state["consecutive_errors"] == ERROR_ALERT_AFTER:
        send_telegram(f"⚠️ The last {ERROR_ALERT_AFTER} stock checks failed.\nLast error: {error}")


def record_success(state: dict, all_stores: dict) -> None:
    t = now()
    if state.get("consecutive_errors", 0) >= ERROR_ALERT_AFTER:
        send_telegram("✅ Stock checks are working again.")
    state["consecutive_errors"] = 0
    state["last_report"] = state["last_check"] = t.isoformat(timespec="seconds")
    state.pop("last_error", None)

    current = {
        name: ({k: v for k, v in info.items() if k != "hours"} if (info := all_stores.get(name)) else None)
        for name in WATCHED_STORES
    }
    hours = {name: (all_stores.get(name) or {}).get("hours", {}) for name in WATCHED_STORES}
    previous = state.get("stores") or {}
    first_run = not previous
    log.info(", ".join(f"{n}: {(i or {}).get('description', 'n/a')}" for n, i in current.items()))

    # 1) Status change (or first run)
    changed = [n for n, i in current.items() if (previous.get(n) or {}).get("status") != (i or {}).get("status")]
    if first_run:
        send_telegram("\n".join(
            ["\U0001f440 Now watching the Bijenkorf advent calendar. Current stock:", ""]
            + store_lines(current) + ["", product_link()]
        ))
    elif changed:
        log.info("CHANGED: %s", ", ".join(changed))
        send_telegram("\n".join(
            ["\U0001f514 <b>Store stock changed!</b>", ""]
            + store_lines(current, previous, changed) + ["", product_link()]
        ))
    if first_run or changed:
        state["stores_changed_at"] = t.isoformat(timespec="seconds")
    state["stores"] = current
    state["hours"] = hours

    # 2) Daily opening / closing updates
    today = t.date()
    daily = state.get("daily") or {}
    if daily.get("date") != today.isoformat():
        daily = {"date": today.isoformat(), "opening": False, "closing": False}
    window = todays_window(hours, today)
    if window:
        opening, closing = window
        if first_run:  # the "now watching" message already covers today so far
            daily["opening"], daily["closing"] = t >= opening, t >= closing
        if t >= closing and not daily["closing"]:
            send_telegram("\n".join(
                ["\U0001f319 <b>Closing update</b> — stores are closed for today.", ""]
                + store_lines(current) + ["", product_link()]
            ))
            daily["opening"] = daily["closing"] = True
        elif opening <= t < closing and not daily["opening"]:
            send_telegram("\n".join(
                ["☀️ <b>Opening update</b>", f"Today: {hours_text(hours, today)}", ""]
                + store_lines(current) + ["", product_link()]
            ))
            daily["opening"] = True
    state["daily"] = daily


def handle_report(report: dict) -> str:
    """Process one result posted by the extension. Returns a short summary."""
    with state_lock:
        state = load_state()
        try:
            if not report.get("ok"):
                raise RuntimeError(report.get("error") or "unknown error from extension")
            record_success(state, parse_stock(report["data"]))
            summary = ", ".join(f"{n}: {(i or {}).get('description', 'n/a')}" for n, i in state["stores"].items())
        except Exception as exc:
            record_failure(state, str(exc))
            summary = f"failed: {exc}"
        if state.pop("stale_alerted", False):
            send_telegram("✅ The browser is reporting again.")
        save_state(state)
        return summary


def stock_reply() -> str:
    with state_lock:
        state = load_state()
    stores = state.get("stores")
    if not stores:
        return "No stock check has come in yet. Is Chrome open with the extension loaded?"
    lines = ["\U0001f4e6 <b>Latest stock</b>", ""] + store_lines(stores) + [
        "",
        f"Checked: {ago(state.get('last_check'))}",
        f"Status unchanged since: {ago(state.get('stores_changed_at'))}",
    ]
    if state.get("last_error"):
        lines.append(f"⚠️ Last attempt ({ago(state.get('last_report'))}) failed: {state['last_error']}")
    return "\n".join(lines + [product_link()])


def check_stale() -> None:
    """Warn once if the browser stopped reporting while the stores are open."""
    with state_lock:
        state = load_state()
        if state.get("stale_alerted") or not state.get("last_report"):
            return
        t = now()
        window = todays_window(state.get("hours") or {}, t.date())
        last = datetime.fromisoformat(state["last_report"])
        if window and window[0] <= t <= window[1] and t - last > STALE_AFTER:
            send_telegram(
                f"⚠️ No stock check since {ago(state['last_report'])}.\n"
                "Is Chrome open with the extension, and is main.py running?"
            )
            state["stale_alerted"] = True
            save_state(state)


# --------------------------------------------------------------------------- local server


class Handler(BaseHTTPRequestHandler):
    """Tiny API for the Chrome extension. Only listens on 127.0.0.1."""

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _from_extension(self) -> bool:
        # Websites can't send this custom header to localhost without a CORS
        # preflight, which this server never approves. So only the extension
        # (which has host permission for 127.0.0.1) gets through.
        return self.headers.get("X-Stock-Checker") == "1"

    def do_GET(self) -> None:
        if not self._from_extension():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/config":
            url = read_product_url()
            return self._send(200, {
                "product_url": url,
                "query": STOCK_QUERY % variant_code_from_url(url),
                "interval_minutes": CHECK_INTERVAL_MIN,
            })
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._from_extension() or self.path != "/report":
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            report = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad json"})
        self._send(200, {"summary": handle_report(report)})

    def log_message(self, *args) -> None:  # keep the console clean
        pass


def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Listening for the Chrome extension on http://%s:%d", HOST, PORT)

    token, chat_id = load_secrets()
    if not token:
        log.info("Telegram not configured; /stock won't work. Running receiver only.")
    while True:
        if not token:
            time.sleep(60)
            continue
        for update in poll_updates(timeout=50):
            if is_stock_request(update, chat_id):
                log.info("/stock requested")
                send_telegram(stock_reply())
        check_stale()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="print the latest saved check")
    parser.add_argument("--get-chat-id", action="store_true", help="list chats that messaged the bot")
    parser.add_argument("--test-telegram", action="store_true", help="send a test message")
    parser.add_argument("--register-commands", action="store_true", help="add /stock to the bot's menu")
    args = parser.parse_args()

    # Windows consoles default to cp1252, which can't print the emoji in messages.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.get_chat_id:
        print_chat_ids()
    elif args.register_commands:
        telegram_api("setMyCommands", {"commands": [
            {"command": "stock", "description": "Latest stock in Utrecht & Eindhoven"},
        ]})
        print("Registered /stock in the bot's command menu.")
    elif args.test_telegram:
        ok = send_telegram("✅ Test message from the Bijenkorf stock checker.")
        print("Sent." if ok else "Failed (see above).")
        return 0 if ok else 1
    elif args.status:
        print(re.sub(r"<[^>]+>", "", stock_reply()))
    else:
        setup_logging()
        run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
