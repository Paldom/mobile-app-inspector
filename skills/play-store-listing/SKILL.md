---
name: play-store-listing
license: MIT
description: Fetches public Google Play listing data for any app by link or package - star rating, ratings count, star histogram, install range, updated date, what's-new, and paginated reviews. Use to find what users are saying about an app lately, or its Play Store rating, reviews, install count, histogram or changelog. Not for installing the app, on-device version/permissions, downloading APKs, or the official own-apps Developer API.
argument-hint: "<play-store-url | package-name>"
---

# play-store-listing

Reads what the **store** shows about an app — rating, installs, histogram,
"updated" date, "what's new", reviews — for any public listing. This is *market*
data. For what the **installed build** reports (exact version, signer,
permissions), use `android-package-diagnostics`; the two answer different
questions and the listing often can't give the exact version anyway.

`scripts/listing.py` wraps the community `google-play-scraper` library on
purpose. That library parses Google's private, undocumented Play endpoints, which
shift without notice — a hand-rolled stdlib scraper would inherit the same
breakage, later, with no upstream to fix it. So this is the one skill in the repo
with a real dependency; it's lazy-imported, so `--self-test` and `--help` work
without it.

## When NOT to use

- Getting the app onto a device → `play-store-app-install`.
- The installed version, signer, or permissions → `android-package-diagnostics`
  (and it's the *only* reliable source of the exact version — see gotchas).
- A full narrative review → `android-app-review`.
- Replying to reviews on *your own* app → the official Play Developer API
  (own-apps-only, last-week window); out of scope here.
- Downloading an APK → never from a Play scraper or mirror (ToS + provenance).

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install "google-play-scraper==1.2.7"       # pin it; layout drift is the risk
```

A global `pip install` is blocked by PEP 668 on Homebrew Python — use a venv or
pipx. Then run the script with that interpreter:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/listing.py" details org.wikipedia
```

## Workflow

1. **Details** — one call for the headline numbers:

   ```bash
   listing.py details "https://play.google.com/store/apps/details?id=org.wikipedia"
   listing.py details com.duolingo --country gb --lang en --json
   ```
   ```
   Wikipedia  (org.wikipedia)  by Wikimedia Foundation
     rating     4.317  from 693664 ratings
     stars      5★497720  4★73531  3★32301  2★24651  1★65452
     installs   50,000,000+
     updated    2026-07-22   listingVersion '50599-r-2026-07-20'
   ```

2. **Reviews** — paginated, throttled:

   ```bash
   listing.py reviews com.duolingo --count 100 --sort newest
   listing.py reviews com.duolingo --star 1 --count 50    # only 1-star
   ```

3. **Record the locale.** Rating, review set, and availability all vary by
   country/language; the output carries the `lang`/`country` used so a run is
   reproducible. `details` retries one alternate country before reporting "not
   found" — geo-unavailable is not the same as nonexistent.

## Output spec

- `details`: a stable JSON/text record with score, ratings, histogram (keyed by
  star), install range, dates, what's-new, flags, and the `lang`/`country` +
  `backend` version the data came from.
- `reviews`: score, text, date, thumbs-up, developer reply, and the app version
  the review was left on (`appVersion`), per review.
- A loud failure (not a null-filled record) when the scraper is broken by a Play
  layout change.

## Gotchas

- **The exact version is usually not on the listing.** App Bundle apps (most big
  ones) show `listingVersion: "Varies with device"`. The reliable build number
  comes from `android-package-diagnostics report <pkg>` on an installed copy —
  the script says so when it sees "Varies with device".
- **Fails loud on layout drift.** The library's dominant failure isn't an
  exception — it's `None`/empty fields when Google shifts an index. The script
  asserts a required set (title, url, installs, and a histogram when ratings
  exist) and errors with "scraper likely broken — update google-play-scraper"
  rather than reporting a hollow listing. If that fires, `pip install -U
  google-play-scraper` first.
- **Rate limits are the #1 operational failure.** Too many requests → 503 +
  CAPTCHA → the IP is banned for ~an hour. The script throttles between review
  pages; for thousands of reviews or many apps, cache results and consider a paid
  provider (SerpApi). Don't parallel-fan-out across packages in a loop.
- **Not on Play ≠ error.** F-Droid-only or unpublished apps have no listing; the
  script says so plainly instead of inventing data.
- **Reviews are user content with personal data** (author names, photos). This
  skill reports review *text* and dates for the run; do not archive or
  redistribute reviewer identities. Scraping public data is generally lawful
  (*hiQ v. LinkedIn*) but violates Play's ToS — best-effort, unofficial, not for
  bulk commercial redistribution. See `../../docs/authorized-use.md`.
- **The official Developer API is the wrong tool** for third-party apps: it
  returns only your *own* apps' reviews, comments-only, last-week-only.

## Files

- `scripts/listing.py` — `details`, `reviews`, `--self-test` (offline: link
  parsing, response shaping, drift detection — no network, no dependency).
