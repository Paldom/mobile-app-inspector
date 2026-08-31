#!/usr/bin/env python3
"""Fetch PUBLIC Google Play listing data for any app: rating, install range,
star histogram, "updated" date, "what's new", and paginated reviews.

This is MARKET data (what the store shows the world), distinct from what the app
reports on-device — for the installed version/signer/permissions use the
android-package-diagnostics skill.

It wraps the community `google-play-scraper` library on purpose rather than
reimplementing it: that library parses Google's private, undocumented Play
endpoints, which change without notice. A hand-rolled stdlib scraper would break
faster than the library the community keeps patched. So the ONE dependency here
is deliberate; everything else is standard library.

    pip install google-play-scraper      # in a venv — see the skill body

Exit codes: 0 ok | 1 not found / rate-limited / dependency missing | 2 usage.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
THROTTLE = 1.5  # seconds between review pages; the library 503s + CAPTCHA-bans a hot IP


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_package(ref: str) -> str:
    """Play URL / market:// URI / bare package -> package name. Shared shape with
    the install skill so a link works identically everywhere."""
    ref = ref.strip().strip("\"'")
    if PKG_RE.match(ref):
        return ref
    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https", "market"):
        for key in ("id", "package"):
            vals = parse_qs(parsed.query).get(key)
            if vals and PKG_RE.match(vals[0]):
                return vals[0]
    die(
        f"could not find a package name in {ref!r}. Expected e.g. "
        f"https://play.google.com/store/apps/details?id=com.example.app or a bare "
        f"com.example.app."
    )


def load_lib():
    """Import lazily so --self-test / --help work without the dependency."""
    try:
        import google_play_scraper

        return google_play_scraper
    except ImportError:
        die(
            "the google-play-scraper library is required for live fetches:\n"
            "    python3 -m venv .venv && . .venv/bin/activate\n"
            "    pip install google-play-scraper\n"
            "(a global `pip install` is blocked by PEP 668 on Homebrew Python — "
            "use a venv or pipx.)",
            1,
        )


def guard_not_found(exc, pkg):
    name = type(exc).__name__
    if "NotFound" in name:
        die(
            f"{pkg} has no public Play listing. It may be unpublished, "
            f"country-restricted, or not distributed through Google Play (e.g. "
            f"F-Droid-only apps).",
            1,
        )
    if "TooManyRequests" in name or "429" in str(exc) or "503" in str(exc):
        die(
            "Google rate-limited this IP (503/CAPTCHA). Wait ~an hour, lower the "
            "page count, or use a paid provider (SerpApi) for volume.",
            1,
        )
    die(f"{name}: {exc}", 1)


# ---------------------------------------------------------------- shaping


def shape_details(d: dict) -> dict:
    """Keep the fields that matter, name them stably, and drop the huge blobs
    (descriptionHTML, screenshots) an agent does not need."""
    return {
        "package": d.get("appId"),
        "title": d.get("title"),
        "developer": d.get("developer"),
        "score": round(d["score"], 3) if d.get("score") is not None else None,
        "ratings": d.get("ratings"),
        "reviewsCount": d.get("reviews"),
        "histogram": normalize_histogram(d.get("histogram")),
        "installs": d.get("installs"),
        "minInstalls": d.get("minInstalls"),
        "free": d.get("free"),
        "price": d.get("price"),
        "offersIAP": d.get("offersIAP"),
        "adSupported": d.get("adSupported"),
        "contentRating": d.get("contentRating"),
        "genre": d.get("genre"),
        "released": d.get("released"),
        "updated": iso_date(d.get("updated")),
        "updatedEpoch": d.get("updated"),
        "listingVersion": d.get("version"),  # often "Varies with device"
        "whatsNew": d.get("recentChanges") or None,
        "url": d.get("url"),
    }


def iso_date(ts):
    """Play returns `updated` as a unix timestamp; a date is what a human reads."""
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
    return ts


def normalize_histogram(h) -> dict | None:
    """The library returns a list [1★,2★,3★,4★,5★]; expose it keyed by star."""
    if isinstance(h, list) and len(h) == 5:
        return {str(i + 1): h[i] for i in range(5)}
    if isinstance(h, dict):
        return {str(k): v for k, v in h.items()}
    return None


def shape_review(r: dict) -> dict:
    # `appVersion`/`reviewCreatedVersion` are the app build the review was left on
    # (verified present in google-play-scraper 1.2.7). `at` is a datetime.
    at = r.get("at")
    return {
        "score": r.get("score"),
        "text": r.get("content"),
        "thumbsUp": r.get("thumbsUpCount"),
        "at": at.isoformat() if hasattr(at, "isoformat") else (str(at) if at else None),
        "replyText": r.get("replyContent"),
        "appVersion": r.get("appVersion") or r.get("reviewCreatedVersion"),
    }


# ---------------------------------------------------------------- commands


def check_shape(s: dict) -> None:
    """The scraper's dominant failure is not an exception — it is None/empty
    fields when Play shifts an index. Fail LOUD so an agent never reports a
    null-filled listing as fact. Careful not to false-positive on a genuinely
    unrated brand-new app (score None + 0 ratings is legitimate)."""
    if not s.get("title") or not s.get("url") or not (s.get("installs") or s.get("minInstalls")):
        die(
            "the listing came back without title/url/installs — google-play-scraper "
            "is very likely broken by a Play layout change. Update it "
            "(pip install -U google-play-scraper) or use a paid provider.",
            1,
        )
    if s.get("score") is not None and s.get("ratings") and not s.get("histogram"):
        die(
            "ratings present but the star histogram is empty — a Play layout "
            "drift the scraper has not caught up to. Update google-play-scraper.",
            1,
        )


def cmd_details(a) -> int:
    g = load_lib()
    pkg = resolve_package(a.ref)
    requested = a.country
    d, resolved = None, requested
    countries = [a.country] + [c for c in ("us", "gb") if c != a.country]
    for i, country in enumerate(countries):
        try:
            d = g.app(pkg, lang=a.lang, country=country)
            resolved = country
            break
        except Exception as e:
            if "NotFound" in type(e).__name__ and i + 1 < len(countries):
                continue  # geo-unavailable != nonexistent; try one alternate country
            guard_not_found(e, pkg)
    a.country = resolved
    shaped = shape_details(d)
    shaped["lang"] = a.lang
    shaped["requestedCountry"] = requested
    shaped["resolvedCountry"] = resolved
    shaped["availableInRequestedCountry"] = resolved == requested  # finding 5
    try:
        from importlib.metadata import version

        shaped["backend"] = "google-play-scraper " + version("google-play-scraper")
    except Exception:
        shaped["backend"] = "google-play-scraper"
    check_shape(shaped)
    if a.json:
        print(json.dumps(shaped, indent=1, ensure_ascii=False))
        return 0
    if not shaped["availableInRequestedCountry"]:
        print(f"note: not listed in {requested!r}; showing {resolved!r}.", file=sys.stderr)
    print(f"{shaped['title']}  ({pkg})  by {shaped['developer']}")
    print(f"  rating     {shaped['score']}  from {shaped['ratings']} ratings")
    if shaped["histogram"]:
        h = shaped["histogram"]
        print(f"  stars      5★{h['5']}  4★{h['4']}  3★{h['3']}  2★{h['2']}  1★{h['1']}")
    print(f"  installs   {shaped['installs']}")
    print(f"  updated    {shaped['updated']}   listingVersion {shaped['listingVersion']!r}")
    print(
        f"  flags      {'free' if shaped['free'] else 'paid ' + str(shaped['price'])}"
        f"{'  IAP' if shaped['offersIAP'] else ''}"
        f"{'  ads' if shaped['adSupported'] else ''}  {shaped['contentRating']}"
    )
    if shaped["whatsNew"]:
        print(f"  what's new {shaped['whatsNew'][:200]}")
    if shaped["listingVersion"] in (None, "", "Varies with device", "VARY"):
        print(
            "  note: the listing hides the exact version (App Bundle). For the "
            "real build, use android-package-diagnostics on an installed copy."
        )
    return 0


def cmd_reviews(a) -> int:
    g = load_lib()
    from google_play_scraper import Sort

    pkg = resolve_package(a.ref)
    order = {"newest": Sort.NEWEST, "relevant": Sort.MOST_RELEVANT}[a.sort]
    collected, token, prev_token = [], None, object()
    pages = max(1, -(-a.count // 100))  # ceil to 100-per-page
    try:
        for _ in range(pages):
            batch, token = g.reviews(
                pkg,
                lang=a.lang,
                country=a.country,
                sort=order,
                count=min(100, a.count - len(collected)),
                continuation_token=token,
                filter_score_with=a.star,
            )
            collected.extend(batch)
            if token == prev_token:
                print(
                    "note: Play returned a repeating page token (often a soft "
                    "rate-limit); stopping.",
                    file=sys.stderr,
                )
                break
            prev_token = token
            if not token or len(collected) >= a.count:  # None token = real end of data
                break
            time.sleep(THROTTLE)  # do not hammer — a hot IP gets a ~1hr ban
    except Exception as e:
        guard_not_found(e, pkg)
    shaped = [shape_review(r) for r in collected[: a.count]]
    if a.json:
        print(json.dumps(shaped, indent=1, ensure_ascii=False))
        return 0
    if not shaped:
        print(
            f"no reviews for {pkg} ({a.lang}/{a.country}" + (f", {a.star}★" if a.star else "") + ")"
        )
        return 0
    print(f"{len(shaped)} reviews for {pkg} ({a.sort}, {a.lang}/{a.country}):")
    for r in shaped:
        ver = f" v{r['appVersion']}" if r.get("appVersion") else ""
        head = f"  {r['score']}★ {r['at'][:10] if r['at'] else '?'}{ver}"
        print(f"{head}  {(r['text'] or '').strip()[:140]}")
        if r["replyText"]:
            print(f"      ↳ dev: {(r['replyText']).strip()[:120]}")
    return 0


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    """Offline: link parsing + response shaping. No network, no dependency."""
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    check(
        "play url resolves",
        resolve_package("https://play.google.com/store/apps/details?id=com.x.y&hl=en") == "com.x.y",
    )
    check("market uri resolves", resolve_package("market://details?id=com.a.b") == "com.a.b")
    check("bare package resolves", resolve_package("org.wikipedia") == "org.wikipedia")

    for bad in ("https://example.com/app", "notapackage", ""):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                resolve_package(bad)
            check(f"reject {bad!r}", False)
        except SystemExit:
            check(f"reject {bad!r}", True)

    fake = {
        "appId": "com.x",
        "title": "X",
        "developer": "Dev",
        "score": 4.31749,
        "ratings": 100,
        "reviews": 40,
        "histogram": [1, 2, 3, 4, 90],
        "installs": "1,000+",
        "minInstalls": 1000,
        "free": True,
        "price": 0,
        "offersIAP": False,
        "adSupported": False,
        "contentRating": "Everyone",
        "genre": "Tools",
        "released": "Jan 1, 2020",
        "updated": 1700000000,
        "version": "Varies with device",
        "recentChanges": "Bug fixes",
        "url": "http://x",
    }
    s = shape_details(fake)
    check("score rounded to 3dp", s["score"] == 4.317)
    check("histogram keyed by star", s["histogram"] == {"1": 1, "2": 2, "3": 3, "4": 4, "5": 90})
    check("whatsNew mapped from recentChanges", s["whatsNew"] == "Bug fixes")
    check("listingVersion preserved verbatim", s["listingVersion"] == "Varies with device")
    check(
        "updated epoch -> UTC date",
        s["updated"] == "2023-11-14" and s["updatedEpoch"] == 1700000000,
    )
    check("huge blobs dropped", "descriptionHTML" not in s and "screenshots" not in s)
    check(
        "empty recentChanges -> None",
        shape_details({**fake, "recentChanges": ""})["whatsNew"] is None,
    )

    import datetime

    rev = {
        "score": 5,
        "content": "great",
        "thumbsUpCount": 3,
        "at": datetime.datetime(2026, 1, 2, 3, 4, 5),
        "replyContent": None,
        "appVersion": "6.9.0",
        "reviewCreatedVersion": "6.9.0",
    }
    sr = shape_review(rev)
    check("review datetime -> iso", sr["at"] == "2026-01-02T03:04:05")
    check("review text mapped from content", sr["text"] == "great")
    check("per-review app version captured", sr["appVersion"] == "6.9.0")
    check(
        "appVersion falls back to reviewCreatedVersion",
        shape_review({"reviewCreatedVersion": "5.0"})["appVersion"] == "5.0",
    )

    check(
        "dict histogram tolerated",
        normalize_histogram({1: 5, 2: 6, 3: 7, 4: 8, 5: 9})
        == {"1": 5, "2": 6, "3": 7, "4": 8, "5": 9},
    )
    check("bad histogram -> None", normalize_histogram([1, 2, 3]) is None)

    import contextlib as _c
    import io as _io

    def rejects(shape):
        try:
            with _c.redirect_stderr(_io.StringIO()):
                check_shape(shape)
            return False
        except SystemExit:
            return True

    ok = {
        "title": "X",
        "url": "u",
        "installs": "1+",
        "minInstalls": 1,
        "score": 4.3,
        "ratings": 100,
        "histogram": {"5": 90},
    }
    check("valid shape passes", not rejects(ok))
    check("no-title shape fails loud", rejects({**ok, "title": None}))
    check("ratings-without-histogram fails loud", rejects({**ok, "histogram": None}))
    check(
        "genuinely unrated app passes (score None, 0 ratings)",
        not rejects({**ok, "score": None, "ratings": 0, "histogram": None}),
    )

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(
        prog="listing", description="Fetch public Google Play listing data."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("ref", help="Play URL, market:// URI, or package name")
        sp.add_argument("--lang", default="en")
        sp.add_argument("--country", default="us")
        sp.add_argument("--json", action="store_true")

    common(sub.add_parser("details", help="rating, installs, histogram, updated, what's new"))

    r = sub.add_parser("reviews", help="paginated public reviews")
    common(r)
    r.add_argument("--count", type=int, default=50, help="max reviews (paged by 100)")
    r.add_argument("--sort", choices=["newest", "relevant"], default="newest")
    r.add_argument("--star", type=int, choices=[1, 2, 3, 4, 5], help="only this star rating")

    a = p.parse_args(argv)
    return {"details": cmd_details, "reviews": cmd_reviews}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
