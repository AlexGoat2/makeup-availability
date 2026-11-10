# Bijenkorf advent calendar stock checker

Watches the store stock of the product in `link.txt` at de Bijenkorf **Utrecht** and
**Eindhoven** and messages you on Telegram:

- when the stock status of either store changes (e.g. `Niet op voorraad` → `3 op voorraad`)
- once a day when the first of the two stores opens, and once when the last one closes
- when you send **`/stock`** to the bot: it replies right away with the latest check
- if the checks stop or keep failing while the stores are open

## How it works

de Bijenkorf is behind Cloudflare bot protection, which blocks scripts. So the stock
is checked by a small Chrome extension (`extension/`). Every 15 minutes it runs the
same request the "Bekijk winkelvoorraad" button makes, from inside a normal Bijenkorf
tab in your own Chrome. It sends the result to `main.py` on your PC
(`http://127.0.0.1:8765`, only reachable from your own computer). `main.py` logs each
check to `stock.log`, keeps the latest state in `state.json`, and handles Telegram.

If no Bijenkorf tab is open, the extension opens the product page as a pinned
background tab. If Cloudflare shows its "Just a moment…" check, the extension reloads
the tab once so Chrome can complete it. If Cloudflare asks you to tick a box, do that
in the tab.

## Setup (one-time)

1. **Python packages:** `pip install -r requirements.txt`
2. **Telegram bot:**
   1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
   2. Copy `secrets.example.json` to `secrets.json` and paste the token in.
   3. Send any message to your bot, run `python main.py --get-chat-id`, and put the
      chat id in `secrets.json`.
   4. Run `python main.py --register-commands` to add `/stock` to the bot's menu.
3. **Chrome extension:**
   1. Go to `chrome://extensions` and turn on **Developer mode** (top right).
   2. Click **Load unpacked** and choose the `extension` folder.
   3. Pin the extension icon. Hover it to see the last result, or click it to check now.

## Running

```
python main.py          # or double-click start.bat; keep the window open
```

Chrome must be open for checks to happen. The extension icon badge shows the status:
empty = OK, `!` = last check failed, `off` = main.py isn't running.

Other commands:

```
python main.py --status          # print the latest check
python main.py --test-telegram   # send a test message
```
