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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# Optional: set this to post into a specific topic in a Telegram forum
# group, instead of the group's General chat. See README.md for how
# to find a topic's ID.
TELEGRAM_MESSAGE_THREAD_ID = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
# Optional: your personal/private Telegram chat ID. When set, "no new
# items" status pings go here instead of the group, so the group only
# ever sees actual new-item alerts. If unset, status pings fall back
# to TELEGRAM_CHAT_ID like everything else.
TELEGRAM_PRIVATE_CHAT_ID = os.environ.get("TELEGRAM_PRIVATE_CHAT_ID", "").strip()

# Turn this on only after you've implemented attempt_purchase() yourself.
AUTO_BUY = False

# If True, also include "No Longer Available Items" (sold-out/ended
# listings) in results, not just Upcoming/Available. On the very first
# run after turning this on, expect a flood of "new item" alerts since
# ~94 previously-unseen sold-out items will suddenly appear.
INCLUDE_NO_LONGER_AVAILABLE = True

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

# Phrases that indicate the page is showing a maintenance notice rather
# than the actual product grid. Lowercased, substring match.
# The first few are exact wording confirmed from Premium Bandai's real
# maintenance page (Aug 2026): a page titled "MAINTENANCE" with the text
# "Our service is temporarily unavailable due to a system maintenance."
# and a "MAINTENANCE PERIOD" section listing timezones.
MAINTENANCE_KEYWORDS = [
    "temporarily unavailable due to a system maintenance",
    "maintenance period",
    "temporarily unavailable",
    "system maintenance",
    "under maintenance",
    "scheduled maintenance",
    "site maintenance",
    "maintenance in progress",
    "currently undergoing maintenance",
    "performing maintenance",
    "service is currently unavailable",
]

# If the page's <title> is exactly this (case-insensitive), treat it as
# maintenance even if body text extraction fails for some reason.
MAINTENANCE_TITLE_EXACT = "maintenance"

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


def _looks_like_maintenance(page, response) -> bool:
    """Best-effort check for a maintenance page instead of the real site.
    Checks the HTTP status and the rendered page's title/text for
    known maintenance phrasing."""
    if response is not None and response.status in (503, 502, 500):
        return True

    try:
        title = (page.title() or "").strip().lower()
    except Exception:
        title = ""

    if title == MAINTENANCE_TITLE_EXACT:
        return True

    try:
        body_text = (page.inner_text("body") or "").lower()
    except Exception:
        body_text = ""

    haystack = title + " " + body_text
    return any(kw in haystack for kw in MAINTENANCE_KEYWORDS)


MAX_PAGES = 20  # safety cap so a broken "next" click can't loop forever


def _extract_cards_into(page, region: str, items: dict, item_path_marker: str) -> int:
    """Extracts matching product cards currently in the DOM into `items`
    (mutated in place). Returns how many *new* items were added, which
    the pagination loop uses to detect "no more new items on this page"
    (a sign we've hit the end, or a stuck 'next' button)."""
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

    added = 0
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

        if item_id not in items:
            added += 1
        items[item_id] = {"name": name, "url": item_url}

    return added


def _go_to_next_page(page, region: str) -> bool:
    """Best-effort click of the pagination 'next' control. Returns True
    if a click succeeded and the grid appears to have changed, False if
    there's no next page (or it couldn't find/click the control)."""
    next_selectors = [
        "[aria-label='Next']",
        "[aria-label='Next page']",
        "a[rel='next']",
        ".pagination-next:not(.disabled)",
        ".pager-next:not(.disabled)",
    ]
    for sel in next_selectors:
        try:
            btn = page.query_selector(sel)
            if btn is None:
                continue
            # Skip if it looks disabled
            disabled = btn.get_attribute("disabled")
            aria_disabled = btn.get_attribute("aria-disabled")
            classes = (btn.get_attribute("class") or "").lower()
            if disabled is not None or aria_disabled == "true" or "disabled" in classes:
                return False
            btn.click(timeout=5000)
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            continue
    return False


def fetch_items_for_region(page, region: str, url: str):
    """Render one region's page with a headless browser and pull out product cards
    across all pages, excluding anything inside a recommendation/related-items widget.

    Returns (items, is_maintenance)."""
    items = {}
    item_path_marker = f"/{region.lower()}/item/"

    response = page.goto(url, wait_until="networkidle", timeout=60_000)

    if _looks_like_maintenance(page, response):
        return items, True

    # Dismiss cookie banner if present (best-effort, ignore failures)
    try:
        page.click("text=Allow All", timeout=3000)
    except Exception:
        pass

    if INCLUDE_NO_LONGER_AVAILABLE:
        # Tick the "No Longer Available Items" filter checkbox so the
        # results include sold-out/ended listings too, not just
        # Upcoming/Available. This is a JS filter toggle, not a URL
        # param, so we click it and wait for the grid to refresh.
        try:
            page.click("text=No Longer Available Items", timeout=5000)
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[{region}] Could not toggle 'No Longer Available "
                  f"Items' filter (page structure may differ): {e}")

    # Bump results-per-page to 40 (if available) so there are fewer
    # pages to click through. Best-effort — falls back to default if
    # this control isn't found.
    try:
        page.click("text=40", timeout=3000)
        page.wait_for_load_state("networkidle", timeout=15_000)
        page.wait_for_timeout(1500)
    except Exception:
        pass

    # Give any lazy-loaded content a moment to render
    page.wait_for_timeout(2000)

    # Walk through every page of results, merging cards as we go.
    for page_num in range(1, MAX_PAGES + 1):
        added = _extract_cards_into(page, region, items, item_path_marker)
        if DEBUG:
            print(f"[{region}] DEBUG page {page_num}: {added} new items "
                  f"(running total: {len(items)})")

        moved = _go_to_next_page(page, region)
        if not moved:
            break
    else:
        print(f"[{region}] Hit MAX_PAGES ({MAX_PAGES}) safety cap — there "
              f"may be more items than were collected. Raise MAX_PAGES in "
              f"bot.py if this region genuinely has more pages.")

    if not items:
        # Nothing matched — dump diagnostics so the log explains *why*
        # instead of just "no items found" (e.g. reveals maintenance
        # wording we don't recognize yet, a bot-block/CAPTCHA page, or
        # a genuine markup change).
        try:
            title = (page.title() or "").strip()
        except Exception:
            title = "(could not read title)"
        try:
            snippet = (page.inner_text("body") or "").strip().replace("\n", " ")[:300]
        except Exception:
            snippet = "(could not read body text)"
        print(f"[{region}] No product cards matched. Page title: {title!r}")
        print(f"[{region}] Body text snippet: {snippet!r}")

    return items, False


def fetch_all_regions():
    """Returns (results, maintenance_regions):
    - results: {region: {item_id: {name, url}}} for every configured region
    - maintenance_regions: list of regions currently showing a maintenance page
    """
    results = {}
    maintenance_regions = []

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
                items, is_maintenance = fetch_items_for_region(page, region, url)
                results[region] = items
                if is_maintenance:
                    maintenance_regions.append(region)
                    print(f"[{region}] Site appears to be under maintenance — "
                          f"skipping this region for this run.")
            except Exception as e:
                print(f"[{region}] Failed to fetch: {e}")
                results[region] = {}

        browser.close()

    return results, maintenance_regions


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def load_seen():
    """Returns {region: {item_id: {name, url}}}, defaulting missing regions to {}."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            # Corrupted/mis-encoded state file — don't crash the whole run.
            # Worst case we re-notify on already-seen items once; that's
            # far better than the bot failing every run from here on.
            print(f"WARNING: could not read {STATE_FILE.name} ({e}). "
                  f"Treating as empty and starting fresh.")
            data = {}
    else:
        data = {}
    for region in REGIONS:
        data.setdefault(region, {})
    return data


def save_seen(items):
    STATE_FILE.write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_SAFE_CHUNK_LEN = 3500  # Telegram's hard cap is 4096; leave margin


def notify_telegram_lines(lines: list, chat_id: str = None, thread_id: str = None):
    """Sends a list of lines as one or more Telegram messages, splitting
    into multiple messages if the combined text would exceed Telegram's
    ~4096 character limit (e.g. a long list of new items). Splits on
    line boundaries so HTML tags never get cut mid-way."""
    if not lines:
        return

    chunks = []
    current, current_len = [], 0
    for line in lines:
        add_len = len(line) + 1  # +1 for the joining newline
        if current and current_len + add_len > TELEGRAM_SAFE_CHUNK_LEN:
            chunks.append(current)
            current, current_len = [], 0
        current.append(line)
        current_len += add_len
    if current:
        chunks.append(current)

    total = len(chunks)
    for idx, chunk_lines in enumerate(chunks, start=1):
        text = "\n".join(chunk_lines)
        if total > 1:
            text += f"\n\n(part {idx}/{total})"
        notify_telegram(text, chat_id=chat_id, thread_id=thread_id)


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
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        # Telegram's response body has the actual reason (e.g. "chat not
        # found", "message thread not found") — surface it instead of
        # just failing with a bare "400 Bad Request".
        print(f"Telegram API rejected the request (chat_id={chat_id}, "
              f"thread_id={thread_id or None}):")
        print(f"  {resp.status_code} {resp.text}")
        print("Common causes: chat_id is wrong, or (for a private chat) "
              "you haven't sent the bot a message yet — bots can't "
              "message a user first. See README.md.")
        raise


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
    current, maintenance_regions = fetch_all_regions()

    if maintenance_regions:
        maint_text = (
            f"🛠️ Premium Bandai maintenance detected: "
            f"{', '.join(maintenance_regions)} — skipping those region(s) "
            f"this run, will retry next scheduled check."
        )
        print(maint_text)
        if TELEGRAM_PRIVATE_CHAT_ID:
            # Private chats don't have topics, so no thread_id here.
            notify_telegram(maint_text, chat_id=TELEGRAM_PRIVATE_CHAT_ID, thread_id="")
        else:
            notify_telegram(maint_text)

    if not any(current.values()):
        if maintenance_regions:
            # Every region was in maintenance — nothing more to do this run.
            return
        print("No items found in any region — the page structure may have "
              "changed. Check PRODUCT_CARD_SELECTOR in bot.py.")
        sys.exit(1)

    seen = load_seen()
    any_new = False

    for region, items in current.items():
        if region in maintenance_regions:
            # Don't touch this region's state — we didn't get a real read
            # on it this run, so leave last-known state as-is.
            continue

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
            notify_telegram_lines(lines)

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
            region for region, items in current.items()
            if items and region not in maintenance_regions
        )
        if checked_regions:
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
