import json

from curl_cffi import requests

PRODUCT_VARIANT_CODE = "205108000026090"
STORES = ("Eindhoven", "Utrecht")


def fetch_stock_text():
    """Call the API and return the raw response text."""
    r = requests.post(
        "https://www.debijenkorf.nl/api/graphql",
        json={"query": 'query ProductStoreStock { productStoreStock(productVariantCode: "%s") { status stock store { id name } } }' % PRODUCT_VARIANT_CODE},
        impersonate="chrome",
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def parse_stock(text, stores=STORES):
    """Return {store_name: {"status": ..., "stock": ...}} for the given stores."""
    entries = json.loads(text)["data"]["productStoreStock"]
    by_name = {e["store"]["name"]: e for e in entries}
    return {
        name: {"status": by_name[name]["status"], "stock": by_name[name]["stock"]}
        if name in by_name
        else {"status": "NOT_LISTED", "stock": 0}
        for name in stores
    }


def get_stock(stores=STORES):
    return parse_stock(fetch_stock_text(), stores)


def format_stock(stock):
    icons = {"IN_STOCK": "✅", "OUT_OF_STOCK": "❌"}
    lines = ["Stock:"]
    for name, info in stock.items():
        icon = icons.get(info["status"], "❔")
        status = info["status"].replace("_", " ").lower()
        lines.append(f"{icon} {name}: {status} ({info['stock']})")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(format_stock(get_stock()))
