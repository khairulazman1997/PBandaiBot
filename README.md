# Premium Bandai SG + US — One Piece Watcher Bot

Checks the One Piece section of both
[Premium Bandai Singapore](https://p-bandai.com/sg/series/onepiece-series) and
[Premium Bandai USA](https://p-bandai.com/us/series/onepiece-series) once a
day and pings you on Telegram when a new item appears on either, tagged with
which region it's on.

## 1. Create a Telegram bot

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts.
   You'll get a **bot token** like `123456:ABC-def...`.
2. Send your new bot any message (e.g. "hi") so it can see your chat.
3. Get your **chat ID**: visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   right after step 2 and look for `"chat":{"id": ...}`.

### Posting to a specific topic in a group

If you're adding the bot to a Telegram **group with Topics enabled**
(a forum-style group) and want notifications to land in one specific
topic rather than the group's General chat:

1. Add the bot to the group, and give it permission to post messages
   (and to see topics — this is on by default for group members).
2. Send a message inside the target topic manually (any message), then
   check `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` — the
   chat ID will be the group's (usually a negative number, e.g.
   `-1001234567890`), and the field `message_thread_id` in that update
   is the topic's ID.
   - Alternatively, in Telegram Desktop/Web, open the topic and check
     the URL — it ends in `.../c/<chat_id>/<thread_id>`.
3. Set both:
   - `TELEGRAM_CHAT_ID` → the group's chat ID
   - `TELEGRAM_MESSAGE_THREAD_ID` → the topic ID
   If `TELEGRAM_MESSAGE_THREAD_ID` is left unset, messages go to the
   group's General topic as normal.

### Keeping "no new items" pings out of the group

If the bot is posting into a group/topic, you probably don't want a
"nothing new" message cluttering it up twice a day — only actual new
listings. To route those status pings to your own private chat with
the bot instead:

1. Message your bot privately (any message, e.g. "hi").
2. Check `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` — your
   private chat ID will be a positive number under `"chat":{"id": ...}`.
3. Set `TELEGRAM_PRIVATE_CHAT_ID` to that value.

With this set: new-item alerts still go to `TELEGRAM_CHAT_ID` (the
group/topic), but "no new items" status pings go to your private chat
instead. Leave it unset and status pings just go wherever
`TELEGRAM_CHAT_ID` points, same as new-item alerts.

**Important:** a bot can't message you first — Telegram only lets it
reply to chats you've already started. Make sure you've sent your bot
at least one direct message (step 1 above) before setting
`TELEGRAM_PRIVATE_CHAT_ID`, or you'll get a `400 Bad Request: chat not
found` error. If you do hit a Telegram API error, the bot now prints
the actual reason from Telegram's response (not just "400 Bad
Request") — check the run log for the specific message.

## 2. Try it locally (optional but recommended)

```bash
pip install -r requirements.txt
playwright install --with-deps chromium

export TELEGRAM_BOT_TOKEN="123456:ABC-def..."
export TELEGRAM_CHAT_ID="123456789"
export TELEGRAM_MESSAGE_THREAD_ID="42"   # omit if not using topics
export TELEGRAM_PRIVATE_CHAT_ID="987654321"   # omit if not needed

python bot.py
```

First run will notify you about *every* current listing (since nothing's
been "seen" yet) — that's expected. After that, only new items trigger a
message.

## 3. Deploy for free with GitHub Actions

This repo already includes `.github/workflows/check.yml`, which runs the
bot on a daily schedule using GitHub's free tier (2,000 free
minutes/month on public repos, more than enough for a script that runs
once a day for ~1 minute).

1. Push this folder to a new GitHub repository.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `TELEGRAM_MESSAGE_THREAD_ID` (optional — only if posting to a
     specific topic in a group, see above)
   - `TELEGRAM_PRIVATE_CHAT_ID` (optional — only if you want "no new
     items" pings sent to your private chat instead of the group, see
     above)
3. That's it. The workflow runs twice daily — 12:00 SGT (noon) and
   00:00 SGT (midnight) — edit the `cron` lines in `check.yml` to
   change the times (cron times are in UTC; SGT is UTC+8).
4. You can also trigger a run manually anytime from the **Actions** tab
   ("Run workflow").

## Once secrets are added — pre-flight checklist

1. **Allow the workflow to write back to the repo.** The workflow
   already declares `permissions: contents: write`, but if your repo
   or org has a stricter default, double-check under **Settings →
   Actions → General → Workflow permissions** that "Read and write
   permissions" is selected — otherwise the step that commits
   `seen_items.json` will fail with a 403.
2. **Do a manual test run first**, rather than waiting for the
   schedule: **Actions tab → Check Premium Bandai SG - One Piece →
   Run workflow**. Watch the run's logs to confirm it actually reached
   both regions and sent (or attempted to send) a Telegram message.
3. **Expect a flood of alerts on the very first run.** Since
   `seen_items.json` doesn't exist yet, everything currently listed
   counts as "new" — that's expected, not a bug. Every run after that
   will only alert on genuine changes.
4. **If Telegram messages fail**, check the run log — `bot.py` prints
   Telegram's actual rejection reason (e.g. "chat not found") rather
   than a bare error code.
5. Scheduled GitHub Actions workflows are **disabled automatically
   after 60 days of repo inactivity** (no pushes/commits) — a commit
   from `git-auto-commit-action` counts as activity, so as long as the
   bot is finding and committing state changes, this won't matter, but
   it's worth checking on the **Actions** tab occasionally to confirm
   the schedule is still enabled.

State (which items have already been seen/notified) is stored in
`seen_items.json` in the repo, keyed by region (`SG` / `US`), and the
workflow commits updates to it after each run — no external database
needed.

To add more regions later, just add an entry to the `REGIONS` dict at
the top of `bot.py`.

### Alternative free hosts
If you'd rather not use GitHub Actions:
- **Render.com** free cron jobs
- **Railway.app** free tier + their cron trigger
- A free-tier **Oracle Cloud** or **Google Cloud** VM with a `cron` entry
- Your own Raspberry Pi / always-on machine with `cron`

GitHub Actions is the simplest since it needs no server of your own and
handles scheduling + secret storage + state persistence in one place.

## 4. Expansion: auto-purchase

`bot.py` includes a stub function `attempt_purchase()` and an `AUTO_BUY`
flag (off by default) as a placeholder for future expansion. It is
**intentionally not implemented**, because:

- It requires scripting a logged-in session, cart, and checkout flow
  specific to Premium Bandai's site, which can change at any time.
- Card payments typically require 3-D Secure/OTP confirmation that
  can't be fully automated — realistically the bot could only get you
  to "item added to cart, complete payment yourself" rather than a
  fully hands-off purchase.
- Many storefronts, especially for limited-run collectibles, prohibit
  automated purchasing in their Terms of Service — worth checking
  Premium Bandai's ToS before building this out, since it could risk
  your account.

If you want to build it out yourself, the docstring in
`attempt_purchase()` sketches the steps (persisted login session via
Playwright's `storage_state`, navigating to the item, selecting
options, proceeding to checkout).

## Notes

- Selector used to find product cards: `a[href*='/item/']` in
  `bot.py`. If the site's markup changes and the bot stops finding
  items, open the page in a browser, inspect a product card (F12 →
  Elements), and update `PRODUCT_CARD_SELECTOR`.
- Runs headless Chromium via Playwright since the site's product grid
  is rendered client-side (a plain `requests` fetch only returns an
  empty shell).

## Fixing false positives (e.g. "Recommended for you" items)

Premium Bandai's series pages often show carousels like "Recommended",
"Related items", or "Ranking" alongside the main product grid. Since
these link to `/item/...` too, the bot filters them out by:

1. **Keyword filter (on by default):** skips any item whose HTML
   ancestor chain contains words like `recommend`, `ranking`,
   `related`, `pickup`, `campaign`, etc. (see `EXCLUDE_ANCESTOR_KEYWORDS`
   in `bot.py`). This catches most cases without any setup.
2. **Container scoping (more precise, optional):** if the keyword
   filter still lets some through, you can scope the search to just
   the main grid's container element by setting `CONTAINER_SELECTOR`
   in `bot.py`.

To find the right value for `CONTAINER_SELECTOR`:

```bash
DEBUG=1 python bot.py
```

This prints every matched item along with its full chain of parent
element classes/IDs, e.g.:

```
[SG] DEBUG match: /sg/item/N1234567/
    ancestors: p-series-list__item > p-series-list__items#series-item-list > ...
```

Look for a class/id that wraps *only* the main grid (not the whole
page) — in the example above that'd be `#series-item-list`. Set:

```python
CONTAINER_SELECTOR = "#series-item-list"
```

and re-run. With `CONTAINER_SELECTOR` set, the keyword filter is
skipped entirely since scoping to the right container is more
reliable than guessing at keywords.

