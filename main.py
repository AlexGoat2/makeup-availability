import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests

from get_stock import fetch_stock_text, format_stock, parse_stock

HERE = Path(__file__).parent
SECRETS = json.loads((HERE / "secrets.json").read_text())
TOKEN = SECRETS["telegram_bot_token"]
CHAT_ID = str(SECRETS["telegram_chat_id"])
API = f"https://api.telegram.org/bot{TOKEN}"

CHECK_INTERVAL = 15 * 60  # seconds
STATE_FILE = HERE / "latest_stock.json"


# ---------- saved state ----------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)  # atomic, so /stock never reads a half-written file


CHECK_LOCK = threading.Lock()  # the 15-min loop and /stock_now must not save at the same time


def check_stock():
    """Fetch stock once and save the response text with a timestamp."""
    with CHECK_LOCK:
        _check_stock()


def _check_stock():
    state = load_state()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    old = parse_stock(state["response_text"]) if state.get("response_text") else None
    new = None
    try:
        text = fetch_stock_text()
        new = parse_stock(text)  # make sure the response is usable before saving it
        state.update(response_text=text, checked_at=now, last_error=None)
        print(f"[{now}] Stock checked OK")
    except Exception as e:
        # keep the last good result, just remember the failure
        state.update(last_error=str(e), failed_at=now)
        print(f"[{now}] Stock check failed: {e}")
    save_state(state)

    if old and new:
        notify_changes(old, new)


def describe(info):
    return f"{info['status'].replace('_', ' ').lower()} ({info['stock']})"


def notify_changes(old, new):
    """Message me when status or stock count changes for a watched store."""
    changes = [
        f"• {name}: {describe(old[name])} → {describe(info)}"
        for name, info in new.items()
        if name in old and info != old[name]
    ]
    if not changes:
        return
    text = "🔔 Stock changed!\n" + "\n".join(changes) + "\n\n" + format_stock(new)
    try:
        send_message(CHAT_ID, text)
    except Exception as e:
        print(f"Could not send change alert: {e}")


def checker_loop():
    while True:
        check_stock()
        time.sleep(CHECK_INTERVAL)


def ago(iso):
    minutes = int((datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h {minutes % 60} min ago"


def latest_stock_reply():
    state = load_state()
    if not state.get("response_text"):
        if state.get("last_error"):
            return f"⚠️ No successful check yet. Last error: {state['last_error']}"
        return "⏳ No stock check has run yet, try again in a moment."

    checked = datetime.fromisoformat(state["checked_at"])
    reply = format_stock(parse_stock(state["response_text"]))
    reply += f"\n\nLast checked: {checked:%Y-%m-%d %H:%M} ({ago(state['checked_at'])})"
    if state.get("last_error"):
        reply += f"\n⚠️ Latest check failed at {datetime.fromisoformat(state['failed_at']):%H:%M}: {state['last_error']}"
    return reply


# ---------- telegram ----------

def send_message(chat_id, text):
    print(text)
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)


def handle(message):
    chat_id = str(message["chat"]["id"])
    if chat_id != CHAT_ID:  # only answer your own chat
        return
    words = message.get("text", "").split()
    command = words[0].split("@")[0].lower() if words else ""
    if command not in ("/stock", "/stock_now", "/stock-now"):
        return
    try:
        if command != "/stock":
            check_stock()  # fresh check right now; also saves it for later /stock
        reply = latest_stock_reply()
    except Exception as e:
        reply = f"⚠️ Could not get stock: {e}"
    send_message(chat_id, reply)


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # so print() of emoji works on Windows
    threading.Thread(target=checker_loop, daemon=True).start()

    offset = None
    print(f"Bot running, checking stock every {CHECK_INTERVAL // 60} min. Send /stock in Telegram. Ctrl+C to stop.")
    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"timeout": 30, **({"offset": offset} if offset else {})},
                timeout=40,
            )
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    handle(update["message"])
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
