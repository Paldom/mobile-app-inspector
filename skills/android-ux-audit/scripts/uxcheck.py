#!/usr/bin/env python3
"""The measurable half of a mobile UX/UI heuristic audit. Standard library only.

A full UX audit is mostly judgment (the 12-category rubric lives in the SKILL and
references). This script computes the parts that are *deterministic from the
accessibility tree* — the checks a human shouldn't eyeball and a model shouldn't
guess:

  * actionable controls with no accessible name in the snapshot (a strong
    screen-reader signal — but confirm with TalkBack; hint/labelFor aren't captured)
  * touch-target size in dp per actionable element (48dp Android / ~44pt iOS),
    from reported bounds (confirm the real hit area — TouchDelegate can differ)

It consumes an `android-ui-driver` snapshot (`uia.py snap --json`) + device density
and emits per-screen findings as SUSPECTED signals with a PROVISIONAL priority
(high/medium) — the rubric assigns the final Nielsen severity. No raw-pixel "WCAG
floor" (WCAG 2.5.8's 24px is CSS px, not device px). Does NOT compute contrast
(false precision on a black-box app; judge by eye or a real tool) and never emits
an overall "UX score".

Exit codes: 0 clean | 1 findings present | 2 usage / bad input.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Platform target-size guidance, resolved to px at the given density.
DP_MIN = 48  # Material 3 target (dp); ~44pt iOS lands just under
DP_WELL_BELOW = 24  # dp — well below any platform guidance (a *policy* line in dp,
# NOT WCAG 2.5.8, which is 24 CSS px with spacing exceptions)
ACTIONABLE = {"btn", "edit", "chk"}  # roles a user is meant to operate


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def find_adb() -> str | None:
    exe = shutil.which("adb")
    if exe:
        return exe
    for root in (
        os.environ.get("ANDROID_HOME"),
        os.path.expanduser("~/Library/Android/sdk"),
        os.path.expanduser("~/Android/Sdk"),
    ):
        cand = os.path.join(root or "", "platform-tools", "adb")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def device_density(serial: str | None) -> int | None:
    adb = find_adb()
    if not adb:
        return None
    cmd = [adb] + (["-s", serial] if serial else []) + ["shell", "wm", "density"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # "Override density: 420" wins over "Physical density: 420" when present
    m = re.search(r"Override density:\s*(\d+)", out) or re.search(r"Physical density:\s*(\d+)", out)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- audit


def actionable(e: dict) -> bool:
    return e.get("role") in ACTIONABLE and e.get("enabled", True)


def label_of(e: dict) -> str:
    return (e.get("text") or "").strip() or (e.get("desc") or "").strip()


def dp(px: float, density: int) -> float:
    return round(px * 160.0 / density, 1)


def audit(snap: dict, density: int) -> list[dict]:
    """Findings for one screen. These are SUSPECTED signals (proxies from the
    serialized tree), each with a PROVISIONAL priority — the rubric assigns the
    final Nielsen severity. Everything works in dp; there is no raw-pixel floor
    (WCAG 2.5.8's 24px is CSS px, not device px)."""
    screen = snap.get("activity", "?")
    els = snap.get("elements", [])
    findings, n = [], 0

    def add(cat, heur, priority, el, evidence, rec):
        nonlocal n
        n += 1
        findings.append(
            {
                "id": f"UX-{n:03d}",
                "screen": screen,
                "category": cat,
                "heuristic": heur,
                "priority": priority,  # provisional
                "evidenceType": "suspected",  # a proxy, needs confirm
                "element": {
                    "role": el.get("role"),
                    "label": label_of(el),
                    "id": el.get("id"),
                    "bounds": el.get("bounds"),
                },
                "evidence": evidence,
                "recommendation": rec,
            }
        )

    acts = [e for e in els if actionable(e)]
    for e in acts:
        b = e.get("bounds")
        if not b or len(b) != 4:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        small_dp = dp(min(w, h), density)
        if small_dp < DP_WELL_BELOW:
            add(
                "interaction",
                "target-size",
                "high",
                e,
                f"{w}x{h}px = {dp(w, density)}x{dp(h, density)}dp — well below the "
                f"{DP_MIN}dp Android target",
                f"expand the hit region to >= {DP_MIN}dp; confirm the effective "
                f"touch area (bounds may differ from a TouchDelegate hit region)",
            )
        elif small_dp < DP_MIN:
            add(
                "interaction",
                "target-size",
                "medium",
                e,
                f"{w}x{h}px = {dp(w, density)}x{dp(h, density)}dp — below the "
                f"{DP_MIN}dp Android target",
                f"expand the hit region to >= {DP_MIN}dp (44pt iOS); confirm the "
                f"effective touch area (TouchDelegate may enlarge it)",
            )
        # No name in the SERIALIZED snapshot. This is a strong but not certain
        # signal: `uiautomator dump` carries text + content-desc, but NOT hintText,
        # labelFor/labeledBy, or merged Compose semantics — so confirm with
        # TalkBack. (resource-id is not a name.) The driver folds child text into a
        # clickable parent, so this means no name on the node or its folded text.
        if not label_of(e):
            add(
                "accessibility",
                "accessible-name",
                "high",
                e,
                f"actionable {e.get('role')} has no accessible name in the snapshot "
                f"(no text/content-desc; hint & labelFor are not captured here)",
                "give it an accessible name (visible label or content-description); "
                "confirm the announcement with TalkBack",
            )

    return findings


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def cmd_audit(a) -> int:
    try:
        snap = (
            json.loads(Path(a.snapshot).read_text()) if a.snapshot != "-" else json.load(sys.stdin)
        )
    except (OSError, ValueError) as e:
        die(f"could not read the snapshot JSON (from `uia.py snap --json`): {e}")
    if "elements" not in snap:
        die(
            "that JSON is not an android-ui-driver snapshot (no 'elements'); "
            "produce it with `uia.py snap --json`"
        )
    density = a.density or device_density(a.serial)
    if not density:
        die("device density unknown — pass --density N (from `adb shell wm density`)")
    findings = audit(snap, density)
    if a.json:
        print(
            json.dumps(
                {
                    "screen": snap.get("activity"),
                    "density": density,
                    "elements": len(snap.get("elements", [])),
                    "findings": findings,
                },
                indent=1,
            )
        )
        return 1 if findings else 0
    print(
        f"UX audit — {snap.get('activity', '?')}  (density {density}, "
        f"{len(snap.get('elements', []))} elements)"
    )
    if not findings:
        print("  no measurable target-size or label issues on this screen.")
        print(
            "  (measurable checks only — judge the 12 rubric categories by eye; "
            "see references/heuristics.md)"
        )
        return 0
    for f in sorted(findings, key=lambda f: PRIORITY_ORDER.get(f["priority"], 9)):
        el = f["element"]
        print(f"  [{f['priority'].upper()} · suspected] {f['id']} {f['category']}/{f['heuristic']}")
        print(
            f"      {el['role']} {el['label']!r}"
            + (f" #{el['id']}" if el["id"] else "")
            + f"  @{el['bounds']}"
        )
        print(f"      {f['evidence']}")
        print(f"      -> {f['recommendation']}")
    byp = {}
    for f in findings:
        byp[f["priority"]] = byp.get(f["priority"], 0) + 1
    print(
        "  provisional priorities: "
        + ", ".join(f"{p}×{byp[p]}" for p in ("high", "medium", "low") if p in byp)  # noqa: RUF001 - U+00D7 is intentional in a rendered size label
    )
    print(
        "  NOTE: these are SUSPECTED signals (proxies from the a11y tree) with a "
        "PROVISIONAL priority — the rubric assigns the final Nielsen severity, "
        "and each needs confirming (TalkBack for names, hit-area for targets). "
        "This is the measurable subset only, no overall UX score; weight it "
        "against the judgment categories (first-run, forms, IA, dark patterns, "
        "contrast, screen-reader flow)."
    )
    return 1


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    check("dp conversion at 420dpi", dp(126, 420) == 48.0)
    check("dp conversion at 160dpi is 1:1", dp(48, 160) == 48.0)

    # density 420 (2.625x): 126px=48dp, 100px=38dp (<48 -> medium), 40px=15.2dp (<24 -> high)
    snap = {
        "activity": "com.x/.Main",
        "elements": [
            {
                "role": "btn",
                "text": "Sign in",
                "desc": "",
                "bounds": [0, 0, 126, 126],
                "enabled": True,
            },  # 48dp labelled -> clean
            {
                "role": "btn",
                "text": "x",
                "desc": "",
                "bounds": [200, 0, 300, 100],
                "enabled": True,
            },  # 38dp -> medium
            {
                "role": "btn",
                "text": "y",
                "desc": "",
                "bounds": [300, 0, 340, 40],
                "enabled": True,
            },  # 15dp -> high
            {
                "role": "btn",
                "text": "",
                "desc": "",
                "bounds": [0, 400, 200, 600],
                "enabled": True,
            },  # unlabelled -> high a11y
            {
                "role": "txt",
                "text": "Heading",
                "desc": "",
                "bounds": [0, 700, 500, 760],
                "enabled": True,
            },  # not actionable
            {
                "role": "btn",
                "text": "",
                "desc": "",
                "bounds": [0, 800, 10, 810],
                "enabled": False,
            },  # disabled
        ],
    }
    f = audit(snap, 420)
    labels = [x["element"]["label"] for x in f]
    check("48dp labelled button produces no finding", "Sign in" not in labels)
    check(
        "38dp button flagged medium target-size",
        any(
            x["priority"] == "medium"
            and x["category"] == "interaction"
            and x["element"]["label"] == "x"
            for x in f
        ),
    )
    check(
        "15dp button flagged high target-size",
        any(
            x["priority"] == "high"
            and x["category"] == "interaction"
            and x["element"]["label"] == "y"
            for x in f
        ),
    )
    check(
        "unlabelled actionable flagged high accessible-name",
        any(x["priority"] == "high" and x["heuristic"] == "accessible-name" for x in f),
    )
    check(
        "all findings are suspected (proxy, not certain)",
        all(x["evidenceType"] == "suspected" for x in f),
    )
    check("no numeric severity emitted by the script", all("severity" not in x for x in f))
    check("plain text not audited", not any(x["element"]["label"] == "Heading" for x in f))
    check(
        "disabled control not audited",
        not any(x["element"]["bounds"] == [0, 800, 10, 810] for x in f),
    )

    clean = audit(
        {
            "activity": "s",
            "elements": [
                {"role": "btn", "text": "OK", "bounds": [0, 0, 200, 200], "enabled": True}
            ],
        },
        420,
    )
    check("clean screen yields no findings", clean == [])

    check(
        "density regex prefers override",
        re.search(
            r"Override density:\s*(\d+)", "Physical density: 440\nOverride density: 420"
        ).group(1)
        == "420",
    )

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(
        prog="uxcheck", description="Measurable UX checks from a ui-driver snapshot."
    )
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("audit", help="target-size, label and spacing findings for one screen")
    au.add_argument("snapshot", help="`uia.py snap --json` output file, or - for stdin")
    au.add_argument("--density", type=int, help="dpi (default: read from the device)")
    au.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    return cmd_audit(a)


if __name__ == "__main__":
    sys.exit(main())
