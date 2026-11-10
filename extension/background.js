// Bijenkorf Stock Checker: runs the store-stock query from inside a normal
// de Bijenkorf tab (so it is an ordinary visitor request) and posts the result
// to main.py on this computer. main.py does the logging and Telegram messages.
//
// Click the extension icon to check right away. Hover it to see the last result.
// Badge: (empty) = OK, "!" = check failed, "off" = main.py not running.

const SERVER = "http://127.0.0.1:8765";
const HEADERS = { "X-Stock-Checker": "1" };
const ALARM = "stock-check";
const DEFAULT_INTERVAL = 15; // minutes; main.py can override via /config

chrome.runtime.onInstalled.addListener(() => schedule(DEFAULT_INTERVAL, 0.1));
chrome.runtime.onStartup.addListener(() => schedule(DEFAULT_INTERVAL, 0.5));
chrome.action.onClicked.addListener(() => runCheck("manual"));
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) runCheck("scheduled");
});

async function schedule(periodInMinutes, delayInMinutes = periodInMinutes) {
  await chrome.alarms.create(ALARM, { periodInMinutes, delayInMinutes });
}

let running = false;

async function runCheck(reason) {
  if (running) return;
  running = true;
  try {
    let config;
    try {
      config = await fetchJson(`${SERVER}/config`);
    } catch (e) {
      setStatus("off", "#777", "main.py is not running (start it with: python main.py)");
      console.warn("main.py unreachable:", e);
      return;
    }
    const alarm = await chrome.alarms.get(ALARM);
    if (!alarm || alarm.periodInMinutes !== config.interval_minutes) {
      await schedule(config.interval_minutes);
    }

    let result;
    try {
      const tab = await getBijenkorfTab(config.product_url);
      result = await queryInTab(tab.id, config.query);
      if (!result.ok && result.challenge) {
        // Cloudflare wants to check the browser. Load the page normally so
        // Chrome can show/complete that check like for any visitor, then retry.
        console.info("Cloudflare check page; reloading the tab and retrying once");
        await chrome.tabs.reload(tab.id);
        await waitForLoad(tab.id);
        await sleep(15000);
        result = await queryInTab(tab.id, config.query);
      }
    } catch (e) {
      result = { ok: false, error: `Extension error: ${e.message || e}` };
    }
    result.reason = reason;

    const { summary } = await fetchJson(`${SERVER}/report`, {
      method: "POST",
      headers: { ...HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(result),
    });
    const time = new Date().toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
    if (result.ok) setStatus("", "#2e7d32", `Last check ${time}: ${summary}`);
    else setStatus("!", "#c62828", `Last check ${time} failed: ${result.error}`);
    console.info(`[${time}] ${summary}`);
  } catch (e) {
    setStatus("!", "#c62828", `Error: ${e.message || e}`);
    console.error(e);
  } finally {
    running = false;
  }
}

async function fetchJson(url, options = {}) {
  const resp = await fetch(url, { ...options, headers: { ...HEADERS, ...(options.headers || {}) } });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} from ${url}`);
  return resp.json();
}

async function getBijenkorfTab(productUrl) {
  const tabs = await chrome.tabs.query({ url: "https://www.debijenkorf.nl/*" });
  let tab = tabs.find((t) => t.url && t.url.startsWith(productUrl)) || tabs[0];
  if (!tab) {
    // No Bijenkorf tab open: open the product page as a pinned background tab.
    tab = await chrome.tabs.create({ url: productUrl, pinned: true, active: false });
    await waitForLoad(tab.id);
  } else if (tab.discarded || tab.status !== "complete") {
    // Chrome's Memory Saver may have unloaded it.
    await chrome.tabs.reload(tab.id);
    await waitForLoad(tab.id);
  }
  return tab;
}

function waitForLoad(tabId, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Bijenkorf tab did not finish loading"));
    }, timeoutMs);
    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((t) => {
      if (t.status === "complete" && !t.discarded) listener(tabId, { status: "complete" });
    });
  });
}

async function queryInTab(tabId, query) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [query],
    // Runs inside the Bijenkorf page: the same request the "Bekijk winkelvoorraad" button makes.
    func: async (q) => {
      try {
        const r = await fetch(`${location.origin}/api/graphql`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ query: q }),
          credentials: "include",
        });
        const text = await r.text();
        if (!r.ok) {
          const challenge = r.status === 403 && /just a moment|challenge/i.test(text);
          return { ok: false, challenge, error: `HTTP ${r.status}${challenge ? " (Cloudflare check)" : ""}` };
        }
        try {
          return { ok: true, data: JSON.parse(text) };
        } catch {
          return { ok: false, error: "Response was not JSON" };
        }
      } catch (e) {
        return { ok: false, error: `Fetch failed: ${e.message || e}` };
      }
    },
  });
  return result;
}

function setStatus(text, color, title) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title: `Bijenkorf Stock Checker\n${title}\n(click to check now)` });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
