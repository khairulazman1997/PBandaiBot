"""
Premium Bandai (SG + US) — One Piece section watcher
------------------------------------------------------
Checks the One Piece section on both:
  - https://p-bandai.com/sg/series/onepiece-series  (Singapore)
  - https://p-bandai.com/us/series/onepiece-series  (USA)
for new/changed listings and sends a Telegram notification when
something changes, tagged with which region it appeared on.

The product grid is rendered client-side (JS), so we use Playwright
(headless Chromium) instead of a plain requests/BeautifulSoup scrape.

State (the list of items we've already seen) is stored in a JSON file
so we only notify on genuinely NEW items, not on every run.

--------------------------------------------------------------------
EXPANSION POINT (auto-purchase):
See `attempt_purchase()` near the bottom. It's a deliberately-empty
stub — wiring it up requires you to record your own site's login +
checkout flow (see the comments there and in README.md). It is OFF
by default (AUTO_BUY = False) and should stay off until you've
implemented and tested it yourself.
--------------------------------------------------------------------
"""

import json
import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REGIONS = {
    "SG": "https://p-bandai.com/sg/series/onepiece-series",
    "US": "https://p-bandai.com/us/series/onepiece-series",
}

STATE_FILE = Path(__file__).parent / "seen_items.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Optional: set this to post into a specific topic in a Telegram forum
# group, instead of the group's General chat. See README.md for how
# to find a topic's ID.
TELEGRAM_MESSAGE_THREAD_ID = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
# Optional: your personal/private Telegram chat ID. When set, "no new
# items" status pings go here instead of the group, so the group only
# ever sees actual new-item alerts. If unset, status pings fall back
# to TELEGRAM_CHAT_ID like everything else.
TELEGRAM_PRIVATE_CHAT_ID = os.environ.get("TELEGRAM_PRIVATE_CHAT_ID", "")

# Turn this on only after you've implemented attempt_purchase() yourself.
AUTO_BUY = False

# Selector guess — Premium Bandai's markup can change without notice.
# If the bot stops finding items on a region, inspect that region's page
# (F12 → Elements) and update this. It matches both /sg/item/ and
# /us/item/ links since the region marker is filtered separately.
PRODUCT_CARD_SELECTOR = "a[href*='/item/']"

# Optional: CSS selector for the container that holds ONLY the main
# One Piece series grid (not recommendation/related/ranking carousels
# elsewhere on the page). Leave as None until you've identified it —
# run with DEBUG=1 to print each matched item's ancestor containers,
# then set this to whichever one wraps just the main grid, e.g.
# CONTAINER_SELECTOR = "#series-item-list" or ".p-series-list__items"
CONTAINER_SELECTOR = None

# Cards whose ancestor chain has a class/id containing any of these
# (case-insensitive) are skipped even without CONTAINER_SELECTOR set —
# catches common "recommended for you" / related-item widget names.
EXCLUDE_ANCESTOR_KEYWORDS = [
    "recommend", "osusume", "ranking", "related", "relate",
    "you-may-like", "youmaylike", "pickup", "also-", "similar",
    "campaign", "banner", "carousel-reco",
]


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

DEBUG = os.environ.get("DEBUG", "") == "1"


def _get_ancestor_signature(card):
    """Returns a lowercase string of all class/id names from this element
    up to <body>, used both for the exclude-keyword filter and for
    DEBUG output so you can identify the right CONTAINER_SELECTOR."""
    return card.evaluate(
        """
        (el) => {
            const parts = [];
            let node = el;
            while (node && node.tagName && node.tagName.toLowerCase() !== 'body') {
                if (node.className && typeof node.className === 'string') {
                    parts.push(node.className);
                }
                if (node.id) {
                    parts.push('#' + node.id);
                }
                node = node.parentElement;
            }
            return parts.join(' > ');
        }
        """
    ).lower()


def fetch_items_for_region(page, region: str, url: str):
    """Render one region's page with a headless browser and pull out product cards,
    excluding anything inside a recommendation/related-items widget."""
    items = {}
    item_path_marker = f"/{region.lower()}/item/"

    page.goto(url, wait_until="networkidle", timeout=60_000)

    # Dismiss cookie banner if present (best-effort, ignore failures)
    try:
        page.click("text=Allow All", timeout=3000)
    except Exception:
        pass

    # Give any lazy-loaded content a moment to render
    page.wait_for_timeout(2000)

    if CONTAINER_SELECTOR:
        scope = page.query_selector(CONTAINER_SELECTOR)
        if scope is None:
            print(f"[{region}] CONTAINER_SELECTOR '{CONTAINER_SELECTOR}' not "
                  f"found on page — falling back to whole-page search + "
                  f"keyword filtering for this run.")
            cards = page.query_selector_all(PRODUCT_CARD_SELECTOR)
        else:
            cards = scope.query_selector_all(PRODUCT_CARD_SELECTOR)
    else:
        cards = page.query_selector_all(PRODUCT_CARD_SELECTOR)

    for card in cards:
        href = card.get_attribute("href") or ""
        if item_path_marker not in href:
            continue

        signature = _get_ancestor_signature(card)

        if DEBUG:
            print(f"[{region}] DEBUG match: {href}\n    ancestors: {signature}\n")

        # Skip if not scoped to a container AND it looks like a
        # recommendation/related-item widget.
        if not CONTAINER_SELECTOR and any(kw in signature for kw in EXCLUDE_ANCESTOR_KEYWORDS):
            if DEBUG:
                print(f"[{region}] DEBUG skipped (matched exclude keyword): {href}\n")
            continue

        item_url = href if href.startswith("http") else f"https://p-bandai.com{href}"
        item_id = href.rstrip("/").split("/")[-1]
        name = (card.inner_text() or "").strip().split("\n")[0] or item_id

        items[item_id] = {"name": name, "url": item_url}

    return items


def fetch_all_regions():
    """Returns {region: {item_id: {name, url}}} for every configured region."""
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )

        for region, url in REGIONS.items():
            try:
                results[region] = fetch_items_for_region(page, region, url)
            except Exception as e:
                print(f"[{region}] Failed to fetch: {e}")
                results[region] = {}

        browser.close()

    return results


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def load_seen():
    """Returns {region: {item_id: {name, url}}}, defaulting missing regions to {}."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
    else:
        data = {}
    for region in REGIONS:
        data.setdefault(region, {})
    return data


def save_seen(items):
    STATE_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def notify_telegram(text: str, chat_id: str = None, thread_id: str = None):
    """Sends a Telegram message. Defaults to the group chat + topic
    (TELEGRAM_CHAT_ID / TELEGRAM_MESSAGE_THREAD_ID) unless overridden —
    used to route "no new items" pings to a private chat instead."""
    chat_id = chat_id or TELEGRAM_CHAT_ID
    thread_id = thread_id if thread_id is not None else TELEGRAM_MESSAGE_THREAD_ID

    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("Telegram credentials not set — skipping notification.")
        print(text)
        return

    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if thread_id:
        data["message_thread_id"] = thread_id

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=data,
        timeout=15,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Expansion point: auto-purchase
# ---------------------------------------------------------------------------

def attempt_purchase(item: dict):
    """
    STUB — not implemented.

    To wire this up you would need to, at minimum:
      1. Log in to the relevant Premium Bandai region site with a
         persisted session (Playwright supports storage_state to reuse
         cookies so you're not scripting the login form itself each
         run). SG and US are separate accounts/logins.
      2. Navigate to item['url'], select any required options
         (e.g. quantity), and click through to checkout.
      3. Complete payment — most storefronts require 3-D Secure /
         OTP confirmation on card payments, which cannot be fully
         automated, so at best this becomes "add to cart and stop
         right before payment, then alert you to finish it."

    Before building this out, check Premium Bandai's Terms of Service —
    many storefronts (especially for limited-run collectibles) explicitly
    prohibit automated purchasing, and violating that can get your
    account suspended. This stub is left here only as a structural
    placeholder for you to fill in deliberately, not as something to
    enable blindly.
    """
    raise NotImplementedError("Auto-purchase is not implemented. See docstring.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    current = fetch_all_regions()

    if not any(current.values()):
        print("No items found in any region — the page structure may have "
              "changed. Check PRODUCT_CARD_SELECTOR in bot.py.")
        sys.exit(1)

    seen = load_seen()
    any_new = False

    for region, items in current.items():
        if not items:
            print(f"[{region}] No items found this run — skipping "
                  f"(leaving previous state untouched for this region).")
            continue

        region_seen = seen.get(region, {})
        new_ids = [i for i in items if i not in region_seen]

        if new_ids:
            any_new = True
            lines = [f"🏴‍☠️ <b>New Premium Bandai {region} — One Piece listing(s)!</b>"]
            for item_id in new_ids:
                item = items[item_id]
                lines.append(f"\n• <a href=\"{item['url']}\">{item['name']}</a>")
            notify_telegram("\n".join(lines))

            if AUTO_BUY:
                for item_id in new_ids:
                    try:
                        attempt_purchase(items[item_id])
                    except NotImplementedError:
                        pass
        else:
            print(f"[{region}] No new items since last check.")

        # Only overwrite this region's state if we actually got results,
        # so a transient fetch failure doesn't wipe out known items.
        seen[region] = items

    if not any_new:
        print("Nothing new across any region.")
        checked_regions = ", ".join(
            region for region, items in current.items() if items
        )
        status_text = (
            f"✅ Checked Premium Bandai ({checked_regions}) — no new "
            f"One Piece items since last check."
        )
        if TELEGRAM_PRIVATE_CHAT_ID:
            # Private chats don't have topics, so no thread_id here.
            notify_telegram(status_text, chat_id=TELEGRAM_PRIVATE_CHAT_ID, thread_id="")
        else:
            notify_telegram(status_text)

    save_seen(seen)


if __name__ == "__main__":
    main()
