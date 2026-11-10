# Bijenkorf advent calendar stock checker

Checks the store stock of the product in `link.txt` at de Bijenkorf **Utrecht** and
**Eindhoven** and sends a Telegram message:

- when the stock status of either store changes (e.g. `Niet op voorraad` → `3 op voorraad`)
- once a day when the first of the two stores opens (opening update)
- once a day when the last of the two stores closes (closing update)

Send **`/stock`** to the bot to get the latest stock, with the time it was checked
and the time you asked. Running locally, the bot answers within seconds. On GitHub
Actions it answers on the next scheduled run (up to ~15 min, longer if GitHub is
slow, and not between 22:00 and 09:00). Only your own chat id gets answers.

Opening hours come from de Bijenkorf's own data, so Sundays, koopavonden and
holidays are handled automatically. It calls the same GraphQL query
(`productStoreStock`) that the "Bekijk winkelvoorraad" panel on the product page uses.

> The site is behind Cloudflare bot protection, which sometimes answers with a
> challenge page (HTTP 403). The script retries a few times and otherwise waits
> for the next check. It warns you on Telegram after 6 failed checks in a row.

## Telegram setup (one-time)

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the bot token.
2. Copy `secrets.example.json` to `secrets.json` and paste the token in.
3. **Send any message to your new bot**, then run `python main.py --get-chat-id`.
   Put the printed `chat_id` in `secrets.json`.
4. `python main.py --test-telegram` should send a test message to your phone.

`secrets.json` is gitignored. You can use env vars `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` instead.

## Running locally

```
pip install -r requirements.txt
python main.py --status        # show stock + today's hours for all stores
python main.py                 # check every 5 min (use --interval to change it)
python main.py --once          # one check, for Task Scheduler
```

## Running on GitHub Actions

`.github/workflows/stock-check.yml` runs `main.py --once` every 15 minutes from
09:07 to 21:52 (Amsterdam winter time), and commits `state.json` back to the repo
when it changes, so the next run knows the previous state.

1. Repo → Settings → Secrets and variables → Actions → add `TELEGRAM_BOT_TOKEN`
   and `TELEGRAM_CHAT_ID`.
2. Actions tab → "Stock check" → **Run workflow** to test it once.
