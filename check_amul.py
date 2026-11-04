"""
Amul whey stock checker -> phone alerts (ntfy, optional Telegram).

How it works:
  1. Opens shop.amul.com in a headless Chrome and enters your pincode
     (stock on Amul depends on the pincode / nearest store).
  2. Opens the Protein category and reads the stock data the site itself loads.
  3. Compares with state.json and sends a phone notification when a product
     goes from "out of stock" to "in stock".
  4. On a manual run (Run workflow button) it always sends a full summary.

Settings come from GitHub secrets: AMUL_PINCODE and NTFY_TOPIC
(optional: TELEGRAM_TOKEN + TELEGRAM_CHAT_ID for Telegram too).
"""

import json
import os
import sys
import time

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

# ---- Products to track (the last part of each product link) ----
PRODUCTS = {
    "amul-whey-protein-32-g-or-pack-of-60-sachets": "Whey Protein Plain - 60 sachets",
    "amul-whey-protein-32-g-or-pack-of-30-sachets": "Whey Protein Plain - 30 sachets",
    "amul-chocolate-whey-protein-34-g-or-pack-of-60-sachets": "Chocolate Whey - 60 sachets",
    "amul-chocolate-whey-protein-34-g-or-pack-of-30-sachets": "Chocolate Whey - 30 sachets",
}

STATE_FILE = "state.json"
BASE = "https://shop.amul.com/en"
FAIL_ALERT_AFTER = 8  # scheduled runs in a row that fail before you get one warning (~2 hours)

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
PINCODE = os.getenv("AMUL_PINCODE", "").strip()
MANUAL = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" or "--summary" in sys.argv


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def link(alias):
    return f"{BASE}/product/{alias}"


# ---------------- Notifications (ntfy and/or Telegram) ----------------
def send_ntfy(text, title, click=None, urgent=False):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": text,
               "priority": 5 if urgent else 3, "tags": ["tada"] if urgent else ["clipboard"]}
    if click:
        payload["click"] = click
    r = requests.post("https://ntfy.sh/", json=payload, timeout=20)
    log("ntfy sent." if r.ok else f"ntfy error {r.status_code}: {r.text[:200]}")


def notify(text, title="Amul stock bot", click=None, urgent=False):
    if NTFY_TOPIC:
        send_ntfy(text, title, click, urgent)
    if TOKEN and CHAT_ID:
        send_telegram(text)
    if not NTFY_TOPIC and not (TOKEN and CHAT_ID):
        log("No notification settings - message not sent:\n" + text)


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"},
        timeout=20,
    )
    if r.ok:
        log("Telegram message sent.")
    else:
        log(f"Telegram error {r.status_code}: {r.text[:300]}")


# ---------------- State ----------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        data.setdefault("products", {})
        data.setdefault("fail_count", 0)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"products": {}, "fail_count": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# ---------------- Amul site ----------------
def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Network.enable", {})
    return driver


def set_pincode(driver):
    wait = WebDriverWait(driver, 25)
    driver.get(f"{BASE}/")
    box = wait.until(
        ec.visibility_of_element_located((By.CSS_SELECTOR, 'input[placeholder="Enter Your Pincode"]'))
    )
    box.clear()
    box.send_keys(PINCODE)
    choice = wait.until(
        ec.element_to_be_clickable(
            (By.CSS_SELECTOR, "div.list-group-item.text-left.searchproduct-name a.searchitem-name")
        )
    )
    choice.click()
    wait.until(
        ec.invisibility_of_element_located((By.CSS_SELECTOR, 'input[placeholder="Enter Your Pincode"]'))
    )
    log(f"Pincode {PINCODE} set.")
    time.sleep(1)


def captured_products(driver):
    """Read every product list the page loaded from Amul's own API."""
    found = {}
    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            url = msg["params"]["response"].get("url", "")
            if "shop.amul.com/api/" not in url or "ms.products" not in url:
                continue
            body = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": msg["params"]["requestId"]}
            )
            for p in json.loads(body.get("body", "{}")).get("data", []) or []:
                if p.get("alias"):
                    found[p["alias"]] = p
        except Exception:
            continue
    return found


def fetch_stock():
    """Returns {alias: product_dict} for the tracked products that were found."""
    driver = make_driver()
    try:
        set_pincode(driver)
        driver.get(f"{BASE}/browse/protein")
        time.sleep(4)
        for _ in range(4):  # scroll so any lazy-loaded products also load
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
        found = captured_products(driver)

        # Anything not seen in the category list: open its own page.
        for alias in PRODUCTS:
            if alias not in found:
                log(f"Not in category list, opening product page: {alias}")
                driver.get(link(alias))
                time.sleep(5)
                found.update(captured_products(driver))
        return {a: found[a] for a in PRODUCTS if a in found}
    finally:
        driver.quit()


def is_in_stock(p):
    return bool(p.get("available"))


# ---------------- Main ----------------
def main():
    if not PINCODE:
        sys.exit("AMUL_PINCODE secret is missing.")

    state = load_state()
    old = state["products"]

    try:
        data = fetch_stock()
        error = None if data else "Could not read stock data from shop.amul.com."
    except Exception as e:  # site changed, timeout, etc.
        data, error = {}, f"{type(e).__name__}: {e}"

    if error:
        log("CHECK FAILED: " + error)
        state["fail_count"] = state.get("fail_count", 0) + 1
        if MANUAL:
            notify(f"Check failed for pincode {PINCODE}.\n{error[:500]}", "⚠️ Amul check failed")
        elif state["fail_count"] == FAIL_ALERT_AFTER:
            notify(
                f"Failed {FAIL_ALERT_AFTER} times in a row. "
                "The website may have changed. Check the Actions tab on GitHub.",
                "⚠️ Amul stock bot needs a look",
            )
        save_state(state)  # keep old stock status so a failure never causes a false alert
        sys.exit(1 if MANUAL else 0)  # scheduled failures stay quiet (no GitHub email spam)

    state["fail_count"] = 0
    new = dict(old)
    restocked, restocked_links, lines = [], [], []

    for alias, label in PRODUCTS.items():
        p = data.get(alias)
        if p is None:
            lines.append(f"❔ {label}: not found")
            continue
        now_in = is_in_stock(p)
        price = p.get("price")
        price_txt = f" - ₹{price}" if price else ""
        lines.append(f"{'✅' if now_in else '❌'} {label}: {'IN STOCK' if now_in else 'out of stock'}{price_txt}")
        if now_in and not old.get(alias, False):
            restocked.append(f"🟢 {label}{price_txt}\n{link(alias)}")
            restocked_links.append(link(alias))
        new[alias] = now_in
        log(f"{label}: {'IN STOCK' if now_in else 'out of stock'}")

    state["products"] = new
    save_state(state)

    if restocked:
        notify(
            "\n\n".join(restocked) + "\n\nOrder fast, it sells out quickly.",
            f"🎉 Amul whey back in stock ({PINCODE})!",
            click=restocked_links[0],
            urgent=True,
        )
    if MANUAL:
        notify("\n".join(lines), f"📋 Amul stock summary ({PINCODE})")


if __name__ == "__main__":
    main()
