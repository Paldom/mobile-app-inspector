#!/usr/bin/env python3
"""Render one self-contained HTML report from every skill's output in a run directory.

A review produces a dozen scattered artifacts — a Play listing, a package dump, an
APK scan, startup numbers, a pcap host list, per-screen UX findings, screenshots.
This turns that pile into a single HTML file you can attach to an email: no CDN, no
webfont, no JavaScript, no tracking.

Two halves, matching how the skills split:

  * MEASURED — auto-discovered by filename from each skill's `--json` output.
    Nothing is invented; a skill that did not run is listed as NOT RUN, never
    silently dropped, because a gap you cannot see reads as a clean bill of health.
  * JUDGED — `review.json`, the analyst half: coverage, findings, limits. Scaffold
    it with `init`.

Deliberately absent: an overall score. The repo's rule is that the verdict is the
worst release gate, never an average that launders a blocker into a good number.

Everything the reviewed app produced — labels, review text, hostnames, crash lines —
is UNTRUSTED and HTML-escaped on the way in. A report is a document you open in a
browser; an app that puts `<script>` in its store listing does not get to run it.

Exit codes: 0 ok | 1 no artifacts found / nothing to report | 2 usage or bad input.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import glob
import html
import json
import os
import re
import sys

TEMPLATE = "report-template.html"
REVIEW = "review.json"
SEVERITIES = ("blocker", "major", "minor", "note")

# Severity says how bad it would be; confidence says how sure we are. Keeping them
# apart is the whole point — a high-impact guess must not read like a reproduced
# defect. Status is the evidence ladder a finding climbs.
CONFIDENCE = ("confirmed", "high", "medium", "low")
STATUSES = ("confirmed", "corroborated", "candidate", "not-assessed")

# Evidence strength implied by the skill that produced it: reproduced runtime
# behaviour beats a decompiled string, and the report should not pretend otherwise.
EVIDENCE_STRENGTH = {
    "android-intent-probe": "strongest", "android-device-matrix": "strongest",
    "android-network-trace": "strong", "android-app-profiling": "strong",
    "android-package-diagnostics": "strong", "android-ui-driver": "strong",
    "android-ux-audit": "moderate", "android-apk-analysis": "moderate",
    "play-store-listing": "moderate", "play-store-app-install": "moderate",
}

# Every skill, the artifact(s) it drops in the run directory, and the command that
# produces each. Drives both the section rendering and the "what ran" table.
SKILL_ARTIFACTS = [
    ("play-store-listing", [
        ("listing.json", "listing.py details <pkg> --json"),
        ("reviews.json", "listing.py reviews <pkg> --json"),
    ]),
    ("play-store-app-install", [
        ("install.json", "playapp.py status <pkg>"),
    ]),
    ("android-package-diagnostics", [
        ("pkgdiag.json", "pkgdiag.py report <pkg> --json"),
        ("crashes.txt", "pkgdiag.py crashes <pkg>"),
    ]),
    ("android-apk-analysis", [
        ("apkscan.json", "apkscan.py report <apk> --json"),
    ]),
    ("android-app-profiling", [
        ("startup.json", "profile.py startup <pkg> --json"),
        ("frames.json", "profile.py frames <pkg> --json"),
        ("memory.json", "profile.py memory <pkg> --json"),
        ("dexopt.json", "profile.py dexopt <pkg> --json"),
    ]),
    ("android-network-trace", [
        ("hosts.json", "nettrace.py hosts run.pcap --json"),
        ("endpoints.json", "nettrace.py apk-endpoints <apk> --json"),
    ]),
    ("android-ui-driver", [
        ("snap-*.json", "uia.py snap --json --all"),
        ("screens/*.png", "uia.py shot screens/NN-name.png"),
    ]),
    ("android-ux-audit", [
        ("ux-*.json", "uxcheck.py audit snap.json --json"),
    ]),
    ("android-intent-probe", [
        ("intents.json", "intentprobe.py run <pkg> --apkscan apkscan.json --go --out intents.json"),
    ]),
    ("android-device-matrix", [
        ("matrix.json", "devmatrix.py run <pkg> --profile quick, then cp <out>/report.json matrix.json"),
    ]),
    ("android-walkthrough-video", [
        ("walkthrough.json", "cp <out>/session.json walkthrough.json"),
    ]),
    ("android-app-review", [
        (REVIEW, "report.py init <run-dir>, then fill it in"),
    ]),
]


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- html plumbing


class Raw(str):
    """HTML that is already safe. Everything else gets escaped."""


def E(x) -> str:
    if isinstance(x, Raw):
        return str(x)
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "yes" if x else "no"
    return html.escape(str(x))


def tag(name: str, inner, **attrs) -> Raw:
    a = "".join(f' {k.rstrip("_").replace("_", "-")}="{html.escape(str(v))}"'
                for k, v in attrs.items() if v is not None)
    return Raw(f"<{name}{a}>{E(inner)}</{name}>")


def chip(label: str, kind: str = "") -> Raw:
    cls = f"chip s-{(kind or label).lower()}"
    return Raw(f'<span class="{cls}">{html.escape(str(label))}</span>')


def table(cols: list, rows: list) -> Raw:
    if not rows:
        return Raw("")
    head = "".join(f"<th>{E(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{E(c)}</td>" for c in r) + "</tr>"
                   for r in rows)
    return Raw(f'<div class="panel"><table><thead><tr>{head}</tr></thead>'
               f"<tbody>{body}</tbody></table></div>")


def kv_table(pairs: list) -> Raw:
    """Two-column facts table; rows whose value is None are dropped."""
    rows = [(k, v) for k, v in pairs if v is not None and v != ""]
    if not rows:
        return Raw("")
    body = "".join(f"<tr><th>{E(k)}</th><td>{E(v)}</td></tr>" for k, v in rows)
    return Raw(f'<div class="panel"><table><tbody>{body}</tbody></table></div>')


def bullets(items: list) -> Raw:
    items = [i for i in items if i]
    if not items:
        return Raw("")
    return Raw('<ul class="tight">' + "".join(f"<li>{E(i)}</li>" for i in items) + "</ul>")


def n_of(count: int, noun: str, plural: str = "") -> str:
    """`1 screen` / `4 screens`. Writing "screen(s)" everywhere is a tell, and it
    reads like a form letter rather than a result."""
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def sev_bar(counts: dict, unproven: int = 0) -> Raw:
    """Severity mix as one bar. Still not a score — the segments stay separate and
    labelled, so a blocker cannot be averaged away by a pile of notes."""
    total = sum(counts.values())
    if not total:
        return Raw("")
    segs = "".join(
        f'<span class="seg s-{sv}" style="flex:{n}" title="{n} {sv}"></span>'
        for sv, n in counts.items() if n)
    legend = " ".join(str(chip(f"{n} {sv}", sv)) for sv, n in counts.items() if n)
    tail = (f'<span class="muted">· {unproven} unproven</span>' if unproven else "")
    return Raw(f'<div class="bar">{segs}</div>'
               f'<p class="legend">{legend} {tail}</p>')


def dot_row(items: list) -> Raw:
    """One dot per thing, coloured by state — a whole table's worth of pass/fail
    read in a glance, with the name on hover."""
    if not items:
        return Raw("")
    dots = "".join(f'<span class="dot s-{html.escape(k)}" title="{html.escape(t)}"></span>'
                   for t, k in items)
    return Raw(f'<div class="dots">{dots}</div>')


def hbars(rows: list, unit: str = "") -> Raw:
    """Labelled horizontal bars: a distribution you can see instead of read."""
    rows = [(l, v) for l, v in rows if isinstance(v, (int, float))]
    if not rows:
        return Raw("")
    top = max(v for _, v in rows) or 1
    out = "".join(
        f'<div class="hbar"><span class="hl">{E(l)}</span>'
        f'<span class="track"><span class="fill" style="width:{100 * v / top:.1f}%"></span></span>'
        f'<span class="hv">{v:,}{E(unit)}</span></div>' for l, v in rows)
    return Raw(f'<div class="panel" style="padding:.7rem .9rem">{out}</div>')


def redact(secret: str) -> str:
    """A live secret never goes in a shareable document. Report a fingerprint that
    is enough to match it against the source, and nothing that can be used."""
    s = str(secret)
    return s if len(s) <= 8 else f"{s[:4]}…{s[-4:]} ({len(s)} chars)"


def raw_json(label: str, obj) -> Raw:
    """The underlying artifact, one click away — a report nobody can audit is a claim."""
    dump = json.dumps(obj, indent=1, ensure_ascii=False)
    if len(dump) > 60000:
        dump = dump[:60000] + "\n… truncated …"
    return Raw(f"<details><summary>{E(label)}</summary><pre>{html.escape(dump)}</pre></details>")


class Sec:
    """One topic of the report, at three depths.

    `summary` is the whole section in one line — the breadth-first layer, and the
    only part that also appears in the overview index and the nav. `lead` is the
    caveat a reader needs before trusting the numbers. `html` is the depth.
    """

    def __init__(self, sid: str, title: str, skill: str, summary: str,
                 metric: str, inner: str, lead: str = ""):
        self.id, self.title, self.skill = sid, title, skill
        self.summary, self.metric, self.lead, self.html = summary, metric, lead, inner

    def render(self) -> Raw:
        h = (f'<h2 id="{html.escape(self.id)}">{E(self.title)}'
             f'<span class="skill">/{E(self.skill)}</span>'
             f'<a class="top" href="#top">top ↑</a></h2>')
        if self.summary:
            h += f'<p class="summary">{E(self.summary)}</p>'
        if self.lead:
            h += f'<p class="lead">{E(self.lead)}</p>'
        return Raw(f"<section>{h}{self.html}</section>")


def section(sid: str, title: str, skill: str, summary: str, *blocks,
            lead: str = "", metric: str = "") -> Sec | None:
    """Build a section, or nothing at all when there is no evidence for it.

    Returning None rather than an empty shell is what keeps the nav and the
    overview honest: they list only topics this run actually has something to say
    about, and `sec_method` separately names the skills that produced nothing.
    """
    inner = "".join(str(b) for b in blocks if b)
    if not inner.strip():
        return None
    return Sec(sid, title, skill, summary, metric, inner, lead)


# ---------------------------------------------------------------- loading


def load_json(run: str, name: str):
    path = os.path.join(run, name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        die(f"{path} is not readable JSON: {e}")


def load_text(run: str, name: str):
    path = os.path.join(run, name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def load_many(run: str, pattern: str) -> list:
    out = []
    for p in sorted(glob.glob(os.path.join(run, pattern))):
        try:
            with open(p, encoding="utf-8") as fh:
                out.append((os.path.basename(p), json.load(fh)))
        except (OSError, ValueError):
            continue
    return out


def present(run: str, name: str) -> bool:
    if "*" in name:
        return bool(glob.glob(os.path.join(run, name)))
    return os.path.exists(os.path.join(run, name))


# ---------------------------------------------------------------- sections


def sec_method(run: str, review: dict) -> Raw:
    """What actually ran. A skill that did not run says so — the whole point."""
    rows = []
    for skill, artifacts in SKILL_ARTIFACTS:
        found = [n for n, _ in artifacts if present(run, n)]
        missing = [(n, c) for n, c in artifacts if not present(run, n)]
        if found:
            state = chip("ran", "ok") if not missing else chip("partial", "minor")
        else:
            state = chip("not run", "note")
        # Name every artifact either way: a present one by filename, a missing one with
        # the command that would produce it, so a gap is actionable rather than blank.
        detail = Raw("<br>".join(
            f"<code>{html.escape(n)}</code>" if present(run, n) else
            f"<code>{html.escape(n)}</code> <span class='mono' style='opacity:.65'>"
            f"&larr; {html.escape(c)}</span>"
            for n, c in artifacts))
        rows.append((skill, state, detail))
    cov = review.get("coverage") or {}

    reached = cov.get("reached") or []
    reached_tbl = table(
        ["#", "Screenshot", "Screen", "Reached by"],
        [(i + 1, Raw(f"<code>{html.escape(str(r.get('screenshot', '—')))}</code>"),
          r.get("screen"), r.get("reachedBy")) for i, r in enumerate(reached)])
    not_reached = cov.get("notReached") or []
    nr = Raw(f"<h3>Not reached, and why</h3>{bullets(not_reached)}") if not_reached else Raw("")
    dots = [(sk, "ok" if any(present(run, n) for n, _ in arts) else "note")
            for sk, arts in SKILL_ARTIFACTS]
    ran = sum(1 for _, k in dots if k == "ok")
    gaps = len(SKILL_ARTIFACTS) - ran
    return section(
        "method", "What ran", "android-app-review",
        (f"All {ran} skills ran." if not gaps
         else f"{ran} of {len(SKILL_ARTIFACTS)} skills ran; {gaps} did not.")
        + f" {n_of(len(reached), 'screen')} opened, "
        + f"{n_of(len(not_reached), 'area')} deliberately left.",
        dot_row(dots),
        Raw(f'<details><summary>Per-skill artifacts</summary>'
            f'{table(["Skill", "Status", "Artifact / command"], rows)}</details>'),
        Raw(f"<h3>Screens reached</h3>{reached_tbl}") if reached else Raw(""),
        nr,
        lead="A skill that did not run is a gap, not a pass.")


def coverage_meta(run: str, review: dict) -> dict:
    """What this run could and could not see, derived from the artifacts themselves.

    A scan that was defeated and a scan that found nothing look identical in a
    findings list. Stating coverage — and the confidence ceiling that follows from
    it — is what stops a blocked run reading as a clean bill of health.
    """
    apk = load_json(run, "apkscan.json") or {}
    hosts = load_json(run, "hosts.json") or {}
    st = load_json(run, "startup.json") or {}
    mx = load_json(run, "matrix.json") or {}
    drove = bool(load_many(run, "snap-*.json"))
    packer = (apk.get("heuristicPacker") or {}).get("suspected")
    skipped = apk.get("layersSkipped") or []

    static = ("not run" if not apk else
              "partial" if skipped else "complete")
    dynamic = ("not run" if not (drove or st or hosts or mx) else
               "partial" if not (drove and hosts) else "complete")
    emulator = bool(st.get("emulator") or mx.get("serial", "").startswith("emulator-")
                    or "emulator" in str((review.get("environment") or {}).get("device", "")))
    # Malware and hardened apps routinely suppress behaviour under an emulator, so a
    # clean dynamic result from one cannot be certified higher than "medium".
    ceiling = "medium" if emulator else "high"
    return {
        "static": static, "dynamic": dynamic,
        "packer": "suspected" if packer else ("none detected" if apk else "not assessed"),
        "layersSkipped": skipped,
        "screensReached": len(((review.get("coverage") or {}).get("reached") or [])),
        "emulator": emulator, "confidenceCeiling": ceiling,
        "networkDecrypted": False,
        "artifacts": [n for _, arts in SKILL_ARTIFACTS for n, _ in arts if present(run, n)],
    }


def sec_coverage(run: str, review: dict, cov: dict) -> Raw:
    rows = [
        ("Static analysis", cov["static"],
         "APK/AAB parsed" + (f"; layers skipped: {', '.join(cov['layersSkipped'])}"
                             if cov["layersSkipped"] else "")),
        ("Dynamic analysis", cov["dynamic"],
         f"{n_of(cov['screensReached'], 'screen')} driven; capture "
         + ("taken" if present(run, "hosts.json") else "not taken")),
        ("Packer / obfuscation", cov["packer"],
         "heuristic string check only — not a packer-detection engine"),
        ("HTTPS payloads", "not decrypted",
         "no user CA installed; hosts come from DNS and TLS SNI, bodies were never read"),
        ("Environment", "emulator" if cov["emulator"] else "physical device",
         "apps can suppress behaviour under emulation, so findings here are capped at "
         f"{cov['confidenceCeiling']} confidence" if cov["emulator"]
         else "behaviour observed on real hardware"),
    ]
    return section(
        "coverage", "Visibility", "android-app-review",
        f"Static analysis {cov['static']}, dynamic {cov['dynamic']}; HTTPS payloads "
        f"were never decrypted. Nothing here is certified above "
        f"{cov['confidenceCeiling']} confidence.",
        table(["Dimension", "Status", "What that means"],
              [(k, chip(v, {"complete": "ok", "partial": "minor",
                            "not run": "note", "not assessed": "note",
                            "suspected": "major"}.get(v, "note")), d)
               for k, v, d in rows]),
        lead="Apps can suppress behaviour under emulation; hence the ceiling.")


def sec_listing(run: str) -> Raw:
    d = load_json(run, "listing.json")
    revs = load_json(run, "reviews.json")
    if not d and not revs:
        return Raw("")
    blocks = []
    if d:
        hist = d.get("histogram") or {}
        blocks.append(Raw(f"<details><summary>Listing detail</summary>{kv_table([
            ("Title", d.get("title")), ("Developer", d.get("developer")),
            ("Rating", f"{d.get('score')} ★" if d.get("score") is not None else None),
            ("Ratings", f"{d['ratings']:,}" if isinstance(d.get("ratings"), int) else d.get("ratings")),
            ("Installs", d.get("installs")),
            ("Listing version", d.get("listingVersion")),
            ("Updated", d.get("updated")), ("Genre", d.get("genre")),
            ("Content rating", d.get("contentRating")),
            ("Free", d.get("free")), ("In-app purchases", d.get("offersIAP")),
            ("Ad supported", d.get("adSupported")),
            ("Store locale", f"{d.get('lang')} / {d.get('resolvedCountry')}"
             if d.get("lang") else None),
            ("Available in requested country", d.get("availableInRequestedCountry")),
        ])}</details>"))
        if hist:
            blocks.append(hbars([(f"{k}\u2605", hist.get(k, 0))
                                 for k in ("5", "4", "3", "2", "1")]))
        if d.get("whatsNew"):
            blocks.append(Raw(f"<h3>What's new</h3><pre>{html.escape(str(d['whatsNew'])[:1500])}</pre>"))
        blocks.append(raw_json("listing.json", d))
    if isinstance(revs, list) and revs:
        blocks.append(Raw(
            f"<details><summary>{n_of(min(len(revs), 12), 'recent review')}</summary>"
            + str(table(["Score", "Version", "Date", "Review"],
                        [(f"{r.get('score')}★", r.get("appVersion"),
                          (r.get("at") or "")[:10], (r.get("text") or "")[:280])
                         for r in revs[:12]])) + "</details>"))
    sm = []
    if d:
        if d.get("score") is not None:
            sm.append(f"{d['score']}\u2605"
                      + (f" from {d['ratings']:,} ratings" if isinstance(d.get("ratings"), int) else ""))
        if d.get("installs"):
            sm.append(f"{d['installs']} installs")
        if d.get("updated"):
            sm.append(f"updated {d['updated']}")
    if isinstance(revs, list) and revs:
        sm.append(f"{len(revs)} recent reviews read")
    return section("listing", "Store listing", "play-store-listing",
                   " \u00b7 ".join(sm) or "Play listing data.",
                   *blocks,
                   lead="Public market data; describes whatever build the store ships now.")


def sec_package(run: str) -> Raw:
    d = load_json(run, "pkgdiag.json")
    inst = load_json(run, "install.json")
    crashes = load_text(run, "crashes.txt")
    if not (d or inst or crashes):
        return Raw("")
    blocks = []
    if d:
        blocks.append(Raw(f"<details><summary>Package detail</summary>{kv_table([
            ("Package", Raw(f"<code>{html.escape(str(d.get('package')))}</code>")),
            ("Version", f"{d.get('versionName')} (code {d.get('versionCode')})"),
            ("min / target SDK", f"{d.get('minSdk')} / {d.get('targetSdk')}"),
            ("Install source", d.get("installer")),
            ("Signer digest", Raw(f"<code>{html.escape(str(d.get('signatureDigest')))}</code>")
             if d.get("signatureDigest") else None),
            ("First install", d.get("firstInstallTime")),
            ("Last update", d.get("lastUpdateTime")),
            ("ABI", d.get("primaryCpuAbi")),
            ("Debuggable", d.get("debuggable")), ("System app", d.get("system")),
            ("Split APKs", d.get("splitApks")),
        ])}</details>"))
        blocks.append(raw_json("pkgdiag.json", d))
    if inst:
        blocks.append(raw_json("install.json", inst))
    if crashes:
        blocks.append(Raw(f"<details><summary>Crash buffer</summary>"
                          f"<pre>{html.escape(crashes[:6000])}</pre></details>"))
    sm = []
    if d:
        sm.append(f"{d.get('versionName')} (code {d.get('versionCode')})")
        sm.append(f"target SDK {d.get('targetSdk')}")
        inst = str(d.get("installer") or "")
        sm.append("sideloaded" if "none" in inst else f"installed by {inst}")
    if crashes and "no crashes" not in crashes.lower():
        sm.append("crash lines present")
    elif crashes:
        sm.append("no crashes in the buffer")
    return section("build", "Installed build", "android-package-diagnostics",
                   " \u00b7 ".join(sm) or "Installed package identity.",
                   *blocks,
                   lead="What is actually installed, not what the store advertises.")


def sec_permissions(run: str) -> Raw:
    d = load_json(run, "pkgdiag.json")
    if not d:
        return Raw("")
    requested = d.get("requestedPermissions") or []
    dangerous = set(d.get("dangerousRequested") or [])
    granted = set(d.get("runtimeGranted") or [])
    denied = set(d.get("runtimeDenied") or [])
    if not requested:
        return Raw("")
    rows = []
    for p in requested:
        if p in granted:
            state = chip("granted", "high")
        elif p in denied:
            state = chip("denied", "ok")
        elif p in dangerous:
            state = chip("not requested", "note")
        else:
            state = chip("install-time", "note")
        short = p.replace("android.permission.", "")
        rows.append((Raw(f"<code>{html.escape(short)}</code>"),
                     chip("dangerous", "major") if p in dangerous else "", state))
    lead = "Runtime state is read from the device, so it reflects this review's taps."
    return section("permissions", "Permissions", "android-package-diagnostics",
                   f"{len(requested)} requested, {len(dangerous)} in a dangerous group, "
                   f"{len(granted)} granted at the end of this run.",
                   table(["Permission", "Class", "State after the review"], rows),
                   lead=lead)


def sec_binary(run: str) -> Raw:
    d = load_json(run, "apkscan.json")
    if not d:
        return Raw("")
    comp = d.get("composition") or {}
    man = d.get("manifest") or {}
    post = d.get("manifestPosture") or {}
    sign = d.get("signer") or {}
    blocks = [kv_table([
        ("Input", Raw(f"<code>{html.escape(os.path.basename(str(d.get('input'))))}</code>")),
        ("Kind", d.get("inputKind")),
        ("SHA-256", Raw(f"<code>{html.escape(str(d.get('inputSha256'))[:32])}…</code>")
         if d.get("inputSha256") else None),
        ("dex files", comp.get("dexFiles")),
        ("Native ABIs", ", ".join(comp.get("nativeAbis") or []) or "none"),
        ("Uncompressed", f"{comp['uncompressedBytes'] / 1e6:.0f} MB"
         if comp.get("uncompressedBytes") else None),
        ("Package / version", f"{man.get('package')} {man.get('versionName')}"
         if man.get("package") else None),
        ("Signature verified", sign.get("verified")),
        ("Signature schemes", ", ".join(k for k, v in (sign.get("schemes") or {}).items() if v)
         or None),
        ("Signer", sign.get("signerDN")),
        ("Debug key", sign.get("debugKey")),
        ("Signer note", sign.get("reason")),
        ("Exported components", post.get("exportedTotal")),
        ("Debuggable", post.get("debuggable")),
        ("allowBackup", post.get("allowBackup")),
        ("Cleartext traffic", post.get("usesCleartextTraffic")),
        ("Network security config", post.get("hasNetworkSecurityConfig")),
        ("Suspected packer", (d.get("heuristicPacker") or {}).get("suspected")),
    ])]
    exported = post.get("exportedComponents") or {}
    if exported:
        blocks.append(table(["Exported", "Count"],
                            [(k, v) for k, v in exported.items() if v]))
    skipped = d.get("layersSkipped") or []
    if skipped:
        blocks.append(Raw(
            f"<h3>Layers not run</h3><p class='lead'>These analyses were skipped, so "
            f"their absence is not a clean result: <code>"
            f"{html.escape(', '.join(skipped))}</code></p>"))
    blocks.append(raw_json("apkscan.json", d))
    cn = next((x.split("=", 1)[1] for x in str(sign.get("signerDN") or "").split(",")
               if x.strip().upper().startswith("CN=")), None)
    sm = [f"{comp.get('dexFiles')} dex", f"{len(comp.get('nativeAbis') or [])} ABIs"]
    if comp.get("uncompressedBytes"):
        sm.append(f"{comp['uncompressedBytes'] / 1e6:.0f} MB")
    if cn:
        sm.append(f"signed by {cn}")
    if post.get("exportedTotal") is not None:
        sm.append(f"{post['exportedTotal']} exported components")
    return section("binary", "Binary", "android-apk-analysis",
                   " \u00b7 ".join(sm), *blocks,
                   lead=(f"Layers not run: {', '.join(skipped)} \u2014 skipped is not clean."
                         if skipped else ""))


def sec_performance(run: str) -> Raw:
    st = load_json(run, "startup.json")
    fr = load_json(run, "frames.json")
    mem = load_json(run, "memory.json")
    dex = load_json(run, "dexopt.json")
    if not (st or fr or mem or dex):
        return Raw("")
    blocks = []
    if st:
        v = st.get("verdict")
        blocks.append(kv_table([
            ("Cold start (median)", Raw(
                f"{E(st.get('medianTotalTimeMs'))} ms &nbsp;"
                f"{chip(v or 'unknown', {'fast': 'ok', 'acceptable': 'minor'}.get(v, 'high'))}")),
            ("Runs", st.get("runs")),
            ("Samples over the excessive threshold", st.get("samplesOverExcessive")),
            ("Excessive threshold", f"{st.get('coldExcessiveMs')} ms"
             if st.get("coldExcessiveMs") else None),
            ("Mode", st.get("mode")),
            ("Metric", st.get("metric")),
        ]))
    if fr:
        blocks.append(kv_table([
            ("Frames rendered", fr.get("totalFrames")),
            ("Janky frames", f"{fr.get('jankyFrames')} ({fr.get('jankyPercent')}%)"
             if fr.get("jankyFrames") is not None else None),
            ("Jank verdict", chip(fr.get("verdict") or "unknown",
                                  {"good": "ok", "ok": "minor"}.get(fr.get("verdict"), "high"))),
            ("Frame time p50/p90/p95/p99 (ms)", " / ".join(
                str((fr.get("frameTimeMsPercentiles") or {}).get(p, "—"))
                for p in ("50", "90", "95", "99"))),
        ]))
    if mem:
        blocks.append(kv_table([
            ("Total PSS", f"{mem.get('totalPssMb')} MB" if mem.get("totalPssMb") else None),
            ("Java heap", f"{mem.get('javaHeapMb')} MB" if mem.get("javaHeapMb") else None),
            ("Native heap", f"{mem.get('nativeHeapMb')} MB" if mem.get("nativeHeapMb") else None),
        ]))
    if dex:
        blocks.append(kv_table([
            ("Compilation status", dex.get("status")),
            ("Reason", dex.get("reason")),
            ("Baseline profile applied", dex.get("baselineProfileApplied")),
        ]))
    emu = (st or fr or {}).get("emulator")
    lead = ("Black-box runtime measurements, no root and no source. "
            + ("Taken on an EMULATOR — indicative of regressions, not device-grade numbers."
               if emu else "Taken on a physical device."))
    sm = []                       # any of these artifacts may be absent
    if (st or {}).get("medianTotalTimeMs"):
        sm.append(f"cold start {st['medianTotalTimeMs']} ms ({st.get('verdict')})")
    if (mem or {}).get("totalPssMb"):
        sm.append(f"{mem['totalPssMb']} MB resident")
    if (fr or {}).get("jankyPercent") is not None:
        sm.append(f"{fr['jankyPercent']}% janky over {fr.get('totalFrames')} frames")
    return section("performance", "Performance", "android-app-profiling",
                   " \u00b7 ".join(sm) or "Runtime measurements.", *blocks, lead=lead)


def sec_network(run: str) -> Raw:
    h = load_json(run, "hosts.json")
    e = load_json(run, "endpoints.json")
    if not (h or e):
        return Raw("")
    blocks = []
    if h:
        dns = h.get("dnsQueries") or {}
        sni = h.get("tlsServerNames") or {}
        names = sorted(set(dns) | set(sni), key=lambda n: -(dns.get(n, 0) + sni.get(n, 0)))
        blocks.append(table(["Host", "DNS lookups", "TLS SNI"],
                            [(n, dns.get(n, "—"), sni.get(n, "—")) for n in names[:60]]))
        stats = h.get("stats") or {}
        if stats.get("complete") is False:
            blocks.append(Raw("<p class='lead'><span class='chip s-major'>truncated</span> "
                              "The capture was cut short, so this host list is not exhaustive.</p>"))
        blocks.append(kv_table([
            ("Packets", stats.get("packets")), ("TCP / UDP / QUIC",
             f"{stats.get('tcp')} / {stats.get('udp')} / {stats.get('quic')}"),
            ("Idle baseline subtracted", h.get("baselineSubtracted")),
        ]))
        blocks.append(raw_json("hosts.json", h))
    if e:
        hosts = e.get("hosts") or {}
        if hosts:
            top = sorted(hosts.items(), key=lambda kv: -kv[1])[:20]
            blocks.append(Raw("<h3>Hostnames found statically in the APK</h3>"))
            blocks.append(table(["Host", "Occurrences"], top))
            more = (f" Showing the top {len(top)} of {len(hosts)} distinct hostnames."
                    if len(hosts) > len(top) else "")
            blocks.append(Raw(f"<p class='lead'>Static strings prove the binary contains "
                              f"them, not that it contacts them at runtime.{E(more)}</p>"))
    dns_n = (h or {}).get("dnsQueries") or {}
    sni_n = (h or {}).get("tlsServerNames") or {}
    allh = set(dns_n) | set(sni_n)
    top = max(dns_n, key=dns_n.get) if dns_n else None
    sm = []
    if allh:
        sm.append(f"{len(allh)} hosts contacted")
    if top:
        sm.append(f"most traffic to {top}")
    if e and (e.get("hosts")):
        sm.append(f"{len(e['hosts'])} hostnames in the binary")
    return section("network", "Network", "android-network-trace",
                   " \u00b7 ".join(sm) or "Network observations.", *blocks,
                   lead="Device-wide capture: attribution is by timing, not by PID.")


def sec_ux(run: str) -> Raw:
    audits = load_many(run, "ux-*.json")
    if not audits:
        return Raw("")
    rows, total = [], 0
    for fname, a in audits:
        screen = (a.get("screen") or "—").split("/")[-1].split(".")[-1]
        for f in (a.get("findings") or []):
            total += 1
            el = f.get("element") or {}
            heur = f.get("heuristic") or "?"
            # The accessible-name evidence string is identical for every hit, so
            # printing it per row is seven copies of one sentence. Only show the
            # detail when it actually differs — target-size carries real numbers.
            detail = f.get("evidence") if heur == "target-size" else ""
            rows.append((
                f.get("id"), screen,
                Raw(f"<code>{html.escape(str(el.get('id') or el.get('label') or el.get('role')))}</code>"),
                heur,
                chip(f.get("priority") or "?", f.get("priority") or "note"),
                detail,
            ))
    if not rows:
        return section("ux", "Usability", "android-ux-audit",
                       f"{n_of(len(audits), 'screen')} checked, no target-size or "
                       f"accessible-name issue.",
                       Raw("<p class='lead'>The judgment categories \u2014 first-run, "
                           "forms, IA, dark patterns, contrast \u2014 still need a human.</p>"))
    highs = sum(1 for _, a in audits for f in (a.get("findings") or [])
                if f.get("priority") == "high")
    names = sum(1 for r in rows if r[3] == "accessible-name")
    return section(
        "ux", "Usability", "android-ux-audit",
        f"{n_of(total, 'finding')} across {n_of(len(audits), 'screen')}"
        + (f", {highs} at high priority" if highs else "") + ", all suspected.",
        table(["ID", "Screen", "Element", "Issue", "Priority", "Detail"], rows),
        Raw(f"<p class='lead'>{n_of(names, 'control')} expose no accessible name in "
            f"the snapshot: no text and no content-description. Hint and labelFor are "
            f"not captured there, so TalkBack settles it.</p>" if names else ""),
        lead="Tree proxies, provisional priority \u2014 confirm with TalkBack.")


def sec_gates(run: str) -> Raw:
    """Play release-readiness, ahead of the security triage on purpose: an app that
    cannot be published is blocked regardless of how its findings are scored."""
    pf = (load_json(run, "apkscan.json") or {}).get("playReadiness")
    if not pf:
        return Raw("")
    rows = [(g.get("gate"),
             chip(g.get("status", "?"), {"pass": "ok", "fail": "blocker",
                                         "warn": "minor"}.get(g.get("status"), "note")),
             g.get("detail")) for g in pf.get("gates") or []]
    failed = [g for g in pf.get("gates") or [] if g.get("status") == "fail"]
    lead = ("Deterministic publishing gates read from the binary — no judgement, no "
            "score. " + (f"{len(failed)} would block a Play release."
                         if failed else "None of these would block a Play release."))
    gates = pf.get("gates") or []
    passed = sum(1 for g in gates if g.get("status") == "pass")
    return section("gates", "Release gates", "android-apk-analysis",
                   f"{passed} of {len(gates)} gates pass"
                   + (f"; {len(failed)} would block a Play release." if failed
                      else "; nothing here blocks a Play release."),
                   dot_row([(g.get("gate", "?"), {"pass": "ok", "fail": "fail"}
                             .get(g.get("status"), "warn")) for g in gates]),
                   Raw(f'<details><summary>Each gate</summary>'
                       f'{table(["Gate", "Status", "Detail"], rows)}</details>'),
                   lead="Read from the binary. A blocker outranks any severity.")


def sec_findings(review: dict) -> Raw:
    findings = review.get("findings") or []
    if not findings:
        return section(
            "findings", "Findings", "android-app-review",
            "None recorded \u2014 measured data only, with no human judgement behind it.",
            Raw("<div class='panel' style='padding:.8rem 1rem'>No analyst findings "
                "were recorded for this run.</div>"))
    counts = {sv: sum(1 for f in findings if (f.get("severity") or "").lower() == sv)
              for sv in SEVERITIES}
    unproven = sum(1 for f in findings
                   if (f.get("confidence") or "").lower() in ("medium", "low")
                   or (f.get("status") or "").lower() == "candidate")
    rows = []
    for f in findings:
        sev = (f.get("severity") or "note").lower()
        conf = (f.get("confidence") or "").lower()
        badges = [chip(sev, sev)]
        if conf and conf not in ("confirmed", "high"):
            badges.append(chip(conf, "minor" if conf == "medium" else "note"))
        if (f.get("status") or "").lower() == "candidate":
            badges.append(chip("candidate", "minor"))
        # One line visible; the full record is one click away. A finding nobody
        # reads because it is a wall of text is a finding that does not land.
        detail = "".join(
            f"<dt>{E(k)}</dt><dd>{E(v)}</dd>" for k, v in (
                ("Evidence", f.get("evidence")), ("Steps", f.get("steps")),
                ("Expected", f.get("expected")),
                ("Standards", " \u00b7 ".join(str(x) for x in (
                    f.get("masvs"), f.get("mobileTop10"), f.get("cwe")) if x) or None),
                ("False positive if", f.get("falsePositiveIf")),
                ("Retest", f.get("retest")),
                ("Emulator artefact", f.get("emulatorCaveat")),
            ) if v)
        rows.append(Raw(
            f"<details class='f'><summary>"
            f"<span class='fid'>{E(f.get('id') or '')}</span>"
            f"<span class='ft'>{E(f.get('title'))}</span>"
            f"{' '.join(str(b) for b in badges)}</summary>"
            f"<div class='fb'><p class='obs'>{E(f.get('observed'))}</p>"
            f"<dl>{detail}</dl></div></details>"))
    worst = next((sv for sv in SEVERITIES if counts.get(sv)), None)
    return section("findings", "Findings", "android-app-review",
                   f"{n_of(len(findings), 'finding')}; worst is {worst}"
                   + (f", {unproven} unproven." if unproven else "."),
                   sev_bar(counts, unproven), *rows,
                   lead="Severity is how bad, confidence is how sure. No overall score.")


def sec_screens(run: str, embed: bool) -> Raw:
    shots = sorted(glob.glob(os.path.join(run, "screens", "*.png")))
    if not shots:
        return Raw("")
    figs = []
    for p in shots:
        name = os.path.basename(p)
        if embed:
            with open(p, "rb") as fh:
                src = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
        else:
            src = f"screens/{name}"
        figs.append(f'<figure><img alt="{html.escape(name)}" src="{html.escape(src)}">'
                    f'<figcaption>{html.escape(name)}</figcaption></figure>')
    return section("screens", "Screens", "android-ui-driver",
                   f"{n_of(len(shots), 'screen')} captured; findings cite these filenames.",
                   Raw(f'<div class="shots">{"".join(figs)}</div>'),
                   lead=("" if embed else "Images are linked, so keep the screens/ "
                                          "folder alongside this file."))


def sec_intents(run: str) -> Raw:
    d = load_json(run, "intents.json")
    if not d:
        return Raw("")
    probes = d.get("probes") or []
    kind = {"launched": "minor", "accepted": "minor", "crashed": "blocker",
            "denied": "ok", "not-found": "ok", "not-assessed": "note",
            "error": "minor"}
    rows = [(chip(p.get("result", "?"), kind.get(p.get("result"), "note")),
             p.get("type"), Raw(f"<code>{html.escape(str(p.get('name')))}</code>"),
             "" if p.get("result") in ("launched", "accepted")
             else re.sub(r"^Starting[^:]*:.*?(?=Error|$)", "", str(p.get("detail") or ""))[:150])
            for p in probes]
    summary = d.get("summary") or {}
    return section(
        "entrypoints", "Entry points", "android-intent-probe",
        ", ".join(f"{v} {k}" for k, v in sorted(summary.items()))
        + f" across {n_of(len(probes), 'probe')}.",
        table(["Result", "Type", "Component / URI", "Detail"], rows),
        lead="Reachable is not vulnerable; skipped is not assessed.")


def sec_matrix(run: str) -> Raw:
    m = load_json(run, "matrix.json")
    if not m:
        return Raw("")
    rows = []
    for c in m.get("cells") or []:
        v = c.get("verified") or {}
        st = c.get("status") or "?"
        rows.append((
            Raw(f"<code>{html.escape(str(c.get('id')))}</code>"),
            f"{v.get('widthDp')}×{v.get('heightDp')}dp" if v.get("widthDp") else "—",
            v.get("sizeClass"),
            chip(st, {"pass": "ok", "suspect": "minor", "fail": "blocker"}.get(st, "note")),
            "; ".join(f"{f.get('kind')}: {f.get('detail')}"
                      for f in (c.get("findings") or [])) or "—",
        ))
    verdict = m.get("verdict") or "unknown"
    passed = sum(1 for c in m.get("cells") or [] if c.get("status") == "pass")
    return section(
        "matrix", "Device matrix", "android-device-matrix",
        f"{passed} of {len(rows)} configurations pass \u2014 verdict {verdict}"
        + (f"; device restored." if m.get("restored") else "; DEVICE NOT RESTORED."),
        Raw(f"<p>Verdict {chip(verdict, 'ok' if verdict == 'pass' else 'blocker')} "
            f"\u00b7 profile {E(m.get('profile'))} "
            f"\u00b7 device restored: {E(m.get('restored'))}</p>"),
        table(["Cell", "Verified size", "Size class", "Status", "Findings"], rows),
        lead="Each cell verified from the device. Gates on crashes and ANRs only.")


def sec_walkthrough(run: str) -> Raw:
    w = load_json(run, "walkthrough.json")
    if not w:
        return Raw("")
    cards = w.get("cards") or []
    return section("walkthrough", "Walkthrough", "android-walkthrough-video",
                   f"{n_of(len(cards), 'card')} over "
                   f"{w.get('width')}\u00d7{w.get('height')} footage.",
                   table(["At", "Card", "Detail"],
                         [(f"{c.get('t')}s", c.get("title"), c.get("body")) for c in cards]))


def sec_limits(run: str, review: dict) -> Raw:
    """Every caveat the tools emitted, plus the analyst's own — collected in one place
    so nobody has to read eight sections to learn what this review cannot say."""
    items = list(review.get("limits") or [])
    apk = load_json(run, "apkscan.json") or {}
    # apkscan already emits a caveat naming the skipped layers — do not restate it.
    items += list(apk.get("analysisCaveats") or [])
    h = load_json(run, "hosts.json") or {}
    if (h.get("stats") or {}).get("complete") is False:
        items.append("The packet capture was truncated — the host list is a floor, not a total.")
    if h and not h.get("baselineSubtracted"):
        items.append("The capture is device-wide with no idle baseline subtracted, so some "
                     "hosts belong to the OS rather than the app.")
    mx = load_json(run, "matrix.json") or {}
    if mx.get("fidelity"):
        items.append("Device matrix: " + str(mx["fidelity"]))
    st = load_json(run, "startup.json") or {}
    if st.get("emulator"):
        items.append("Performance numbers come from an emulator: useful for spotting "
                     "regressions, not comparable to a real device.")
    if load_many(run, "ux-*.json"):
        items.append("UX findings are proxies from the accessibility tree (evidence type: "
                     "suspected) — each needs confirming with TalkBack or the real hit area.")
    items.append("Everything the app displayed was treated as untrusted data and escaped "
                 "into this document; no on-screen text was executed as an instruction.")
    seen, uniq = set(), []          # tools and analyst can raise the same caveat
    for i in items:
        k = " ".join(str(i).lower().split())
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    items = uniq
    return section("limits", "Limits", "android-app-review",
                   f"{n_of(len(items), 'limit')} on what this run can claim.",
                   Raw(f'<div class="panel" style="padding:.85rem 1.1rem">'
                       f'{bullets(items)}</div>'))


# ---------------------------------------------------------------- assembly


def facts_strip(run: str, review: dict) -> Raw:
    pk = load_json(run, "pkgdiag.json") or {}
    li = load_json(run, "listing.json") or {}
    st = load_json(run, "startup.json") or {}
    ap = load_json(run, "apkscan.json") or {}
    hosts = load_json(run, "hosts.json") or {}
    ux = sum(len(a.get("findings") or []) for _, a in load_many(run, "ux-*.json"))
    findings = review.get("findings") or []
    worst = next((s for s in SEVERITIES
                  if any((f.get("severity") or "").lower() == s for f in findings)), None)
    names = set(hosts.get("dnsQueries") or {}) | set(hosts.get("tlsServerNames") or {})
    # A full X.500 DN does not fit a summary tile — show the common name here and
    # keep the whole DN in the binary section.
    dn = (ap.get("signer") or {}).get("signerDN") or ""
    cn = next((p.split("=", 1)[1] for p in dn.split(",")
               if p.strip().upper().startswith("CN=")), dn) or None
    cells = [
        ("Version code", pk.get("versionCode") or None),
        ("Play rating", f"{li['score']} ★" if li.get("score") is not None else None),
        ("Installs", li.get("installs")),
        ("Cold start", f"{st['medianTotalTimeMs']} ms" if st.get("medianTotalTimeMs") else None),
        ("Permissions", f"{len(pk.get('requestedPermissions') or [])} "
         f"({len(pk.get('dangerousRequested') or [])} dangerous)"
         if pk.get("requestedPermissions") else None),
        ("Signed by", cn),
        ("Hosts contacted", len(names) if names else None),
        ("UX findings", ux if ux else None),
        ("Worst finding", Raw(str(chip(worst, worst))) if worst else
         ("none recorded" if findings else None)),
    ]
    cells = [(k, v) for k, v in cells if v is not None]
    if not cells:
        return Raw("")
    return Raw('<div class="facts">' + "".join(
        f'<div class="fact"><div class="k">{E(k)}</div><div class="v">{E(v)}</div></div>'
        for k, v in cells) + "</div>")


def build_body(run: str, review: dict, embed: bool) -> tuple:
    app = review.get("app") or {}
    pk = load_json(run, "pkgdiag.json") or {}
    label = app.get("label") or (load_json(run, "listing.json") or {}).get("title") or "App review"
    package = app.get("package") or pk.get("package") or ""
    env = review.get("environment") or {}
    meta = " · ".join(str(x) for x in (
        package, env.get("device"), env.get("androidVersion"), env.get("locale"),
        env.get("date")) if x)
    head = Raw(
        f'<header class="app" id="top"><h1>{E(label)} — app review</h1>'
        f'<div class="sub">{E(meta)}</div>'
        + (f'<p class="goal">{E(review["goal"])}</p>' if review.get("goal") else "")
        + str(facts_strip(run, review)) + "</header>")

    # Breadth first: every topic is built, then indexed at one line each, and only
    # then rendered in full. A reader who stops after the overview has still been
    # told everything that matters — including what was not covered.
    secs = [s for s in (
        sec_method(run, review),
        sec_coverage(run, review, coverage_meta(run, review)),
        sec_gates(run),
        sec_findings(review),
        sec_listing(run),
        sec_package(run),
        sec_permissions(run),
        sec_binary(run),
        sec_performance(run),
        sec_network(run),
        sec_ux(run),
        sec_intents(run),
        sec_matrix(run),
        sec_screens(run, embed),
        sec_walkthrough(run),
        sec_limits(run, review),
    ) if s]

    glance = "".join(
        f'<a class="row" href="#{html.escape(s.id)}">'
        f'<span class="t">{E(s.title)}</span>'
        f'<span class="s">{E(s.summary)}</span></a>' for s in secs)
    overview = Raw(
        '<section style="margin-top:1.8rem"><h2 id="overview">At a glance</h2>'
        f'<div class="glance" style="margin-top:.85rem">{glance}</div></section>')

    nav = Raw("".join(
        f'<a href="#{html.escape(s.id)}">{E(s.title)}</a>' for s in secs))
    body = str(head) + str(overview) + "".join(str(s.render()) for s in secs)
    return Raw(body), Raw(nav), label, package


SARIF_LEVEL = {"blocker": "error", "major": "error", "minor": "warning",
               "note": "note"}


def machine_twin(run: str, review: dict, cov: dict) -> dict:
    """The same report as data. A human reads the HTML; CI reads this."""
    findings = review.get("findings") or []
    return {
        "schema": "mobile-app-inspector/review-report/1",
        "app": review.get("app") or {},
        "environment": review.get("environment") or {},
        "coverage": cov,
        "severityCounts": {s: sum(1 for f in findings
                                  if (f.get("severity") or "").lower() == s)
                           for s in SEVERITIES},
        "overallScore": None,   # explicit: the verdict is the worst finding, not an average
        "findings": findings,
        "notRun": [n for _, arts in SKILL_ARTIFACTS for n, _ in arts
                   if not present(run, n)],
        "limits": review.get("limits") or [],
    }


def sarif(review: dict, cov: dict) -> dict:
    """Minimal SARIF 2.1.0 so a CI job can ingest findings (GitHub code scanning).

    `level` carries severity; the confidence and status fields ride along in
    properties so a low-confidence candidate is not silently promoted to a defect.
    """
    results = []
    for f in review.get("findings") or []:
        sev = (f.get("severity") or "note").lower()
        results.append({
            "ruleId": f.get("id") or "F",
            "level": SARIF_LEVEL.get(sev, "note"),
            "message": {"text": f.get("title") or ""},
            "properties": {"severity": sev,
                           "confidence": f.get("confidence"),
                           "status": f.get("status"),
                           "evidence": f.get("evidence"),
                           "confidenceCeiling": cov.get("confidenceCeiling")},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": (review.get("app") or {}).get("package")
                                     or "app"}}}],
        })
    return {"version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {
                "name": "mobile-app-inspector",
                "informationUri": "https://github.com/Paldom/mobile-app-inspector",
                "rules": []}},
                "results": results}]}


def cmd_build(a) -> int:
    run = a.run
    if not os.path.isdir(run):
        die(f"{run!r} is not a directory — collect each skill's output into one folder first")
    review = load_json(run, REVIEW) or {}
    found = [n for _, arts in SKILL_ARTIFACTS for n, _ in arts if present(run, n)]
    if not found:
        die(f"no skill artifacts in {run!r} — expected files like pkgdiag.json, "
            f"listing.json, ux-*.json (see `report.py sources`)", 1)

    tpl_path = a.template or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "assets", TEMPLATE)
    try:
        with open(tpl_path, encoding="utf-8") as fh:
            tpl = fh.read()
    except OSError as e:
        die(f"cannot read the template {tpl_path!r}: {e}")
    for ph in ("{{TITLE}}", "{{BODY}}", "{{NAV}}", "{{GENERATED}}"):
        if ph not in tpl:
            die(f"the template is missing the {ph} placeholder")

    body, nav, label, package = build_body(run, review, embed=not a.link_images)
    stamp = (review.get("environment") or {}).get("date") or \
        datetime.date.today().isoformat()
    generated = (f"Generated by android-app-review report.py from "
                 f"{n_of(len(found), 'artifact')} "
                 f"in {os.path.basename(os.path.abspath(run))} on {stamp}. "
                 f"App-supplied text is escaped, never executed. "
                 f"Screenshots and logs can carry personal data — redact before sharing.")
    out = a.out or os.path.join(run, "index.html")
    title = a.title or f"{label} — app review"
    html_doc = (tpl.replace("{{TITLE}}", html.escape(title))
                   .replace("{{SUBTITLE}}", html.escape(package))
                   .replace("{{NAV}}", str(nav))
                   .replace("{{BODY}}", str(body))
                   .replace("{{GENERATED}}", html.escape(generated)))
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html_doc)
    except OSError as e:
        die(f"cannot write {out!r}: {e}")
    missing = [n for _, arts in SKILL_ARTIFACTS for n, _ in arts if not present(run, n)]
    print(f"{out}")
    print(f"report: {len(found)} artifact(s) rendered, {len(html_doc) // 1024} KB, "
          f"{len(review.get('findings') or [])} analyst finding(s)")

    # The machine-readable twin ships beside the HTML: same run, no re-derivation.
    cov = coverage_meta(run, review)
    base = os.path.splitext(out)[0]
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(machine_twin(run, review, cov), fh, indent=1, ensure_ascii=False)
    print(f"{base}.json (machine-readable twin)")
    if a.sarif:
        with open(base + ".sarif", "w", encoding="utf-8") as fh:
            json.dump(sarif(review, cov), fh, indent=1)
        print(f"{base}.sarif (CI ingest)")
    print(f"coverage: static {cov['static']} · dynamic {cov['dynamic']} · "
          f"confidence ceiling {cov['confidenceCeiling']}")
    if missing:
        print(f"NOT RUN (listed as gaps in the report): {', '.join(missing)}")
    return 0


SKELETON = {
    "app": {"label": "<App name>", "package": "<com.example.app>"},
    "goal": "<why this review was run, one sentence>",
    "environment": {"device": "emulator-5554, google_apis_playstore arm64-v8a",
                    "androidVersion": "Android 15 (API 35)", "locale": "en-US",
                    "date": "YYYY-MM-DD"},
    "budget": "<N steps, M screens>",
    "coverage": {
        "reached": [{"screenshot": "01-launch.png", "screen": "First run",
                     "reachedBy": "cold launch"}],
        "notReached": ["<area> — <why: login wall / policy / budget>"],
    },
    "findings": [{
        "id": "F1", "title": "<short title>",
        # how bad it would be, if real
        "severity": "major",            # blocker | major | minor | note
        # how sure you are — kept separate so a guess never reads like a defect
        "confidence": "high",           # confirmed | high | medium | low
        "status": "corroborated",       # confirmed | corroborated | candidate | not-assessed
        "source": "android-ux-audit",   # which skill produced the evidence
        "observed": "<what happened, one sentence>",
        "evidence": "<screenshot filename; quoted snap row>",
        "steps": "<launch -> Settings -> Sync>",
        "expected": "<what a user would reasonably expect>",
        "masvs": "<MASVS-PLATFORM-1, optional>",
        "mobileTop10": "<M8: Security Misconfiguration, optional>",
        "cwe": "<CWE-926, optional>",
        "falsePositiveIf": "<what would make this a false alarm>",
        "retest": "<how to confirm it is fixed>",
        "emulatorCaveat": "no",
    }],
    "limits": ["<what this review could not cover>"],
}


def cmd_init(a) -> int:
    os.makedirs(a.run, exist_ok=True)
    path = os.path.join(a.run, REVIEW)
    if os.path.exists(path) and not a.force:
        die(f"{path} already exists — edit it, or pass --force to overwrite")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(SKELETON, fh, indent=1)
        fh.write("\n")
    print(f"{path}")
    print("fill in the judgment half (coverage, findings, limits), collect each skill's "
          "--json output beside it, then `report.py build`.")
    return 0


def cmd_sources(a) -> int:
    print("Collect these into one run directory; every missing file is reported as a gap.\n")
    for skill, arts in SKILL_ARTIFACTS:
        print(f"{skill}")
        for name, cmd in arts:
            print(f"    {name:<22} <- {cmd}")
    return 0


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    import tempfile
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    XSS = '<script>alert("pwn")</script>'
    with tempfile.TemporaryDirectory() as run:
        os.makedirs(os.path.join(run, "screens"), exist_ok=True)
        json.dump({"package": "com.example.app", "versionName": "1.2.3",
                   "versionCode": "10203", "minSdk": "24", "targetSdk": "35",
                   "installer": "com.android.vending", "signatureDigest": "ab12",
                   "requestedPermissions": ["android.permission.CAMERA",
                                            "android.permission.INTERNET"],
                   "dangerousRequested": ["android.permission.CAMERA"],
                   "runtimeGranted": ["android.permission.CAMERA"], "runtimeDenied": []},
                  open(os.path.join(run, "pkgdiag.json"), "w"))
        json.dump({"title": XSS, "score": 4.3, "ratings": 1234, "installs": "1,000,000+",
                   "histogram": {"1": 10, "2": 5, "3": 20, "4": 100, "5": 400}},
                  open(os.path.join(run, "listing.json"), "w"))
        json.dump({"package": "com.example.app", "medianTotalTimeMs": 342, "runs": [342],
                   "verdict": "fast", "emulator": True, "coldExcessiveMs": 5000},
                  open(os.path.join(run, "startup.json"), "w"))
        json.dump({"screen": "com.example/.Main", "density": 420, "elements": 12,
                   "findings": [{"id": "UX-001", "screen": "com.example/.Main",
                                 "category": "accessibility", "heuristic": "accessible-name",
                                 "priority": "high", "evidenceType": "suspected",
                                 "element": {"role": "btn", "label": "", "id": "b1"},
                                 "evidence": "no accessible name"}]},
                  open(os.path.join(run, "ux-01.json"), "w"))
        json.dump({"dnsQueries": {"api.example.com": 3}, "tlsServerNames": {},
                   "ipPeers": {}, "stats": {"packets": 10, "complete": False},
                   "baselineSubtracted": False},
                  open(os.path.join(run, "hosts.json"), "w"))
        json.dump({"app": {"label": "Example", "package": "com.example.app"},
                   "environment": {"date": "2026-01-01"},
                   "coverage": {"reached": [{"screenshot": "01.png", "screen": "Home",
                                             "reachedBy": "launch"}],
                                "notReached": ["Checkout — policy (financial action)"]},
                   "findings": [{"id": "F1", "title": "Crashes on rotate",
                                 "severity": "blocker", "observed": XSS,
                                 "evidence": "02.png"},
                                {"id": "F2", "title": "Small target", "severity": "minor"}],
                   "limits": ["One locale only"]},
                  open(os.path.join(run, REVIEW), "w"))

        out = os.path.join(run, "r.html")
        rc = cmd_build(argparse.Namespace(run=run, out=out, template=None, title=None,
                                          link_images=True, sarif=False))
        check("build exits 0", rc == 0)
        doc = open(out, encoding="utf-8").read()

        check("report is one self-contained file (no external fetches)",
              "http://" not in doc and "https://" not in doc and "<script src" not in doc)
        check("untrusted app text is escaped, never live markup",
              "<script>alert" not in doc and "&lt;script&gt;" in doc)
        check("measured facts reach the summary strip", "342" in doc and "4.3" in doc)
        check("a skill that did not run is named as a gap",
              "not run" in doc and "apkscan.json" in doc)
        check("analyst findings render with their severity",
              "Crashes on rotate" in doc and "blocker" in doc)
        # The repo's rule: a verdict is the worst unresolved finding, never an average.
        # So assert no aggregate score is COMPUTED, and that the report says as much.
        check("no aggregate score is computed, and the report says so",
              ">Score<" not in doc and "s-blocker" in doc
              and "no overall score" in doc.lower())
        check("coverage names what was not reached", "Checkout" in doc)
        check("UX findings keep their suspected/provisional framing",
              "UX-001" in doc and "suspected" in doc)
        check("permissions table separates dangerous from install-time",
              "CAMERA" in doc and "dangerous" in doc)
        check("tool caveats are aggregated into the limits section",
              "truncated" in doc and "emulator" in doc.lower())
        check("the run's own limits survive", "One locale only" in doc)
        check("template placeholders are all consumed",
              "{{" not in doc and "}}" not in doc)
        # A placeholder named in the template's own comment would be filled too,
        # silently duplicating the whole report inside that comment. Section titles
        # legitimately repeat (nav + overview + heading), so anchor on the id.
        check("the body is injected exactly once",
              doc.count('id="method"') == 1 and doc.count('id="top"') == 1)
        check("every topic is reachable from the nav and the overview",
              doc.count('href="#method"') == 2 and 'class="glance"' in doc)
        check("the overview lists a one-line summary per topic",
              doc.count('class="row"') >= 4)

        # an empty directory must fail loudly rather than emit a blank report
        with tempfile.TemporaryDirectory() as empty:
            try:
                cmd_build(argparse.Namespace(run=empty, out=os.path.join(empty, "x.html"),
                                             template=None, title=None, link_images=True, sarif=False))
                check("an empty run directory is rejected", False)
            except SystemExit as e:
                check("an empty run directory is rejected", e.code == 1)

        # a review with no findings still builds, and says so
        os.remove(os.path.join(run, REVIEW))
        rc = cmd_build(argparse.Namespace(run=run, out=out, template=None, title=None,
                                          link_images=True, sarif=False))
        doc2 = open(out, encoding="utf-8").read()
        check("a run with no analyst findings still builds", rc == 0)
        check("...and admits the report is measurement only",
              "No analyst findings" in doc2)

    check("every skill in the repo has an artifact row",
          len(SKILL_ARTIFACTS) == len([d for d in os.listdir(
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
              if os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "..", "..", d, "SKILL.md"))]))
    check("escaping helper leaves pre-marked HTML alone",
          E(Raw("<b>x</b>")) == "<b>x</b>" and E("<b>") == "&lt;b&gt;")
    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(
        prog="report",
        description="Render one self-contained HTML report from a directory of skill outputs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="render the HTML report")
    b.add_argument("run", help="directory holding each skill's output")
    b.add_argument("--out", help="output path (default: <run>/index.html)")
    b.add_argument("--template", help="custom HTML template with {{TITLE}}/{{BODY}}/{{GENERATED}}")
    b.add_argument("--title")
    b.add_argument("--sarif", action="store_true",
                   help="also write <out>.sarif for CI ingest (GitHub code scanning)")
    b.add_argument("--link-images", action="store_true",
                   help="reference screenshots instead of embedding them (smaller file, "
                        "no longer self-contained)")

    i = sub.add_parser("init", help="scaffold review.json, the judgment half")
    i.add_argument("run")
    i.add_argument("--force", action="store_true")

    sub.add_parser("sources", help="list every artifact the report can consume")

    a = p.parse_args(argv)
    return {"build": cmd_build, "init": cmd_init, "sources": cmd_sources}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
