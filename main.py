"""Watch de Bijenkorf store stock for a product and notify via Telegram.

The product page's "Bekijk winkelvoorraad" panel is backed by a public GraphQL
query (productStoreStock). We call it directly, compare the result for the
watched stores with the last saved state, and send a Telegram message when:

  * the stock status of a watched store changes (e.g. out of stock -> in stock)
  * the first watched store opens for the day   (daily "opening" summary)
  * the last watched store closes for the day   (daily "closing" summary)

Opening hours come from the same API, so holidays and koopavonden are handled.

Usage:
    python main.py                 # check every INTERVAL seconds, forever
    python main.py --once          # single check (GitHub Actions / Task Scheduler)
    python main.py --status        # print current stock for all stores, no notify
    python main.py --get-chat-id   # show chat ids that messaged your bot
    python main.py --test-telegram # send a test message
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime
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

TZ = ZoneInfo("Europe/Amsterdam")
GRAPHQL_URL = "https://www.debijenkorf.nl/api/graphql"
WATCHED_STORES = ("Eindhoven", "Utrecht")
DEFAULT_INTERVAL = 300  # seconds
ERROR_ALERT_AFTER = 6  # consecutive failed checks before a Telegram warning
RETRIES = 3  # attempts per check
RETRY_DELAY = 10  # seconds, multiplied by the attempt number

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0 Safari/537.36"
)

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


def now() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


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


# --------------------------------------------------------------------------- http


def post_json(url: str, payload: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def fetch_store_stock(variant_code: str) -> dict[str, dict]:
    """Return {store_name: {status, stock, description, hours}} for all stores.

    hours maps ISO date -> [from, until] ("HH:MM" strings).
    """
    # The site sits behind Cloudflare, which now and then answers with a
    # "Just a moment..." challenge (HTTP 403). Treat that as transient: wait and
    # try again a few times, then give up until the next scheduled check.
    for attempt in range(1, RETRIES + 1):
        try:
            data = post_json(GRAPHQL_URL, {"query": STOCK_QUERY % variant_code})
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429, 500, 502, 503, 504) or attempt == RETRIES:
                raise RuntimeError(f"HTTP {exc.code} from de Bijenkorf (attempt {attempt})") from None
            log(f"HTTP {exc.code} (attempt {attempt}), retrying in {RETRY_DELAY}s")
            time.sleep(RETRY_DELAY * attempt)
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


# --------------------------------------------------------------------------- telegram


def telegram_api(method: str, payload: dict | None = None) -> dict:
    token, _ = load_secrets()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set (env var or secrets.json)")
    return post_json(f"https://api.telegram.org/bot{token}/{method}", payload or {})


def send_telegram(text: str) -> bool:
    token, chat_id = load_secrets()
    if not token or not chat_id:
        log("Telegram not configured; message would have been:\n" + text)
        return False
    try:
        telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return True
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        # Never echo the URL: it contains the bot token.
        log(f"Telegram send failed: {type(exc).__name__}: {getattr(exc, 'reason', exc)}")
        return False


def print_chat_ids() -> None:
    updates = telegram_api("getUpdates").get("result", [])
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("No messages found. Send any message to your bot in Telegram, then retry.")
    for cid, name in chats.items():
        print(f"chat_id={cid}  ({name})")


# --------------------------------------------------------------------------- logic


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    # Only stable data goes in here, so the file (committed by GitHub Actions)
    # changes only when something meaningful happens.
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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


def todays_window(all_stores: dict, today: date) -> tuple[datetime, datetime] | None:
    """(first opening, last closing) today across the watched stores, or None if all closed."""
    opens, closes = [], []
    for name in WATCHED_STORES:
        hours = ((all_stores.get(name) or {}).get("hours") or {}).get(today.isoformat())
        if hours:
            opens.append(dtime.fromisoformat(hours[0]))
            closes.append(dtime.fromisoformat(hours[1]))
    if not opens:
        return None
    return (
        datetime.combine(today, min(opens), TZ),
        datetime.combine(today, max(closes), TZ),
    )


def hours_text(all_stores: dict, today: date) -> str:
    parts = []
    for name in WATCHED_STORES:
        h = ((all_stores.get(name) or {}).get("hours") or {}).get(today.isoformat())
        parts.append(f"{name} {h[0]}–{h[1]}" if h else f"{name} closed")
    return ", ".join(parts)


def check(variant_code: str, url: str, state: dict) -> None:
    """One check: fetch, diff against state, notify. Mutates state."""
    link = f'<a href="{url}">Open product page</a>'
    try:
        all_stores = fetch_store_stock(variant_code)
    except Exception as exc:  # network, JSON, API changes
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        log(f"Check failed ({state['consecutive_errors']}x): {exc}")
        if state["consecutive_errors"] == ERROR_ALERT_AFTER:
            send_telegram(
                f"⚠️ Stock checker failed {ERROR_ALERT_AFTER}x in a row.\n"
                f"Last error: {exc}"
            )
        return

    if state.get("consecutive_errors", 0) >= ERROR_ALERT_AFTER:
        send_telegram("✅ Stock checker is working again.")
    state["consecutive_errors"] = 0

    current = {
        name: ({k: v for k, v in info.items() if k != "hours"} if (info := all_stores.get(name)) else None)
        for name in WATCHED_STORES
    }
    previous = state.get("stores") or {}
    first_run = not previous
    log(", ".join(f"{n}: {(i or {}).get('description', 'n/a')}" for n, i in current.items()))

    # 1) Status change (or first run)
    changed = [
        n for n, i in current.items()
        if (previous.get(n) or {}).get("status") != (i or {}).get("status")
    ]
    if first_run:
        send_telegram("\n".join(
            ["\U0001f440 Now watching the Bijenkorf advent calendar. Current stock:", ""]
            + store_lines(current) + ["", link]
        ))
    elif changed:
        send_telegram("\n".join(
            ["\U0001f514 <b>Store stock changed!</b>", ""]
            + store_lines(current, previous, changed) + ["", link]
        ))
    state["stores"] = current

    # 2) Daily opening / closing summaries
    t = now()
    today = t.date()
    daily = state.get("daily") or {}
    if daily.get("date") != today.isoformat():
        daily = {"date": today.isoformat(), "opening": False, "closing": False}
    window = todays_window(all_stores, today)
    if window:
        opening, closing = window
        if first_run:  # the "now watching" message already covers today so far
            daily["opening"] = t >= opening
            daily["closing"] = t >= closing
        if t >= closing and not daily["closing"]:
            send_telegram("\n".join(
                ["\U0001f319 <b>Closing update</b> — stores are closed for today.", ""]
                + store_lines(current) + ["", link]
            ))
            daily["opening"] = daily["closing"] = True
        elif opening <= t < closing and not daily["opening"]:
            send_telegram("\n".join(
                ["☀️ <b>Opening update</b>", f"Today: {hours_text(all_stores, today)}", ""]
                + store_lines(current) + ["", link]
            ))
            daily["opening"] = True
    state["daily"] = daily


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="seconds between checks")
    parser.add_argument("--status", action="store_true", help="print stock for all stores and exit")
    parser.add_argument("--get-chat-id", action="store_true", help="list chats that messaged the bot")
    parser.add_argument("--test-telegram", action="store_true", help="send a test message")
    args = parser.parse_args()

    # Windows consoles default to cp1252, which can't print the emoji in messages.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.get_chat_id:
        print_chat_ids()
        return 0
    if args.test_telegram:
        ok = send_telegram("✅ Test message from the Bijenkorf stock checker.")
        print("Sent." if ok else "Failed (see log above).")
        return 0 if ok else 1

    url = read_product_url()
    variant_code = variant_code_from_url(url)

    if args.status:
        today = now().date().isoformat()
        for name, info in fetch_store_stock(variant_code).items():
            marker = "*" if name in WATCHED_STORES else " "
            h = info["hours"].get(today)
            hrs = f"{h[0]}-{h[1]}" if h else "closed"
            print(f"{marker} {name:<12} {info['status']:<14} {info['description']:<28} today {hrs}")
        return 0

    log(f"Watching variant {variant_code} in {', '.join(WATCHED_STORES)}")
    while True:
        state = load_state()
        check(variant_code, url, state)
        save_state(state)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
