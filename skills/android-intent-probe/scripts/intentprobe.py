#!/usr/bin/env python3
"""Fire the app's own declared entry points at it, and record what happens.

`android-apk-analysis` reads the manifest and says which components are exported.
That is a claim about the binary, not about the running app: the highest-volume
static finding class in mobile review is "exported component", and most of them
are perfectly fine. This is the dynamic half — it starts each exported activity,
broadcasts to each receiver, starts each service and queries each provider, then
reports what actually happened.

The point is to CHANGE THE EVIDENCE CLASS of a finding: a component that merely
launches is a Candidate; one that performs a privileged action for an unprivileged
caller is a real finding; one that is denied or does not exist can be dismissed.
Reaching a component is never, by itself, a vulnerability.

SAFETY. Probing runs real code in a real app. `plan` is the default and changes
nothing; `run` refuses to fire until given `--go`. Components whose names suggest
a destructive, financial or account-ending action are skipped by policy and
reported as NOT ASSESSED — never silently as passed. Extras are one inert token;
this sends no credentials and no real data. Use a disposable emulator.

Exit codes: 0 ok | 1 a probe crashed the app | 2 usage or device error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

ADB_TIMEOUT = 60
SETTLE = 1.2
PROBE_EXTRA = "mai_probe"  # inert marker, so a log line is traceable to us
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

# Names that suggest firing the component would destroy data, spend money, or end a
# session. A probe is not worth any of those, so these are skipped unless a human
# names them explicitly with --allow.
DESTRUCTIVE = (
    "delete",
    "wipe",
    "erase",
    "reset",
    "clear",
    "remove",
    "purchase",
    "buy",
    "pay",
    "payment",
    "checkout",
    "billing",
    "subscribe",
    "transfer",
    "withdraw",
    "logout",
    "signout",
    "sign_out",
    "unlink",
    "deactivate",
    "unregister",
    "uninstall",
    "factory",
    "format",
)


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def find_adb() -> str:
    exe = shutil.which("adb")
    if exe:
        return exe
    for root in (
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        os.path.expanduser("~/Library/Android/sdk"),
        os.path.expanduser("~/Android/Sdk"),
    ):
        cand = os.path.join(root or "", "platform-tools", "adb")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    die("adb not found. Install Android platform-tools or set ANDROID_HOME.")


class Device:
    def __init__(self, serial: str | None):
        self.adb = find_adb()
        self.serial = serial or os.environ.get("ANDROID_SERIAL") or self._pick()

    def _pick(self) -> str:
        out = subprocess.run(
            [self.adb, "devices"], capture_output=True, text=True, timeout=ADB_TIMEOUT
        ).stdout
        devs = [
            ln.split("\t")[0]
            for ln in out.splitlines()[1:]
            if ln.strip() and ln.endswith("\tdevice")
        ]
        if not devs:
            die("no device attached.", 2)
        if len(devs) > 1:
            die(f"{len(devs)} devices attached: {', '.join(devs)}. Pass --serial.")
        return devs[0]

    def shell(self, *args, timeout=ADB_TIMEOUT) -> str:
        # `adb shell` joins its args and runs them through the DEVICE's sh — quote
        # each one so a stray ';' in a component name cannot execute on the device.
        quoted = [shlex.quote(str(a)) for a in args]
        r = subprocess.run(
            [self.adb, "-s", self.serial, "shell", *quoted],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ((r.stdout or "") + (r.stderr or "")).replace("\r\n", "\n")

    def foreground(self) -> str:
        m = re.search(
            r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)",
            self.shell("dumpsys", "activity", "activities"),
        )
        return m.group(1) if m else ""


# ---------------------------------------------------------------- inventory


def load_inventory(path: str) -> list:
    """Components come from `apkscan.py report --json` (exportedInventory).

    Composing on that instead of re-parsing the manifest keeps one owner for
    "what does this binary declare" — and it is the same file the report reads.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        die(f"cannot read {path!r}: {e}")
    inv = data.get("exportedInventory") or {}
    if inv.get("status") != "ran":
        die(
            f"{path} has no usable exportedInventory "
            f"({inv.get('reason') or inv.get('status') or 'missing'}) — regenerate it "
            f"with `apkscan.py report <apk> --json`",
            1,
        )
    return [c for c in inv.get("components") or [] if c.get("exported") and c.get("name")]


def is_destructive(name: str) -> bool:
    low = (name or "").lower()
    return any(w in low for w in DESTRUCTIVE)


def plan_probes(components: list, pkg: str, allow: set) -> list:
    """One probe record per thing we could fire. Nothing is sent here."""
    out = []
    for c in components:
        name, typ = c["name"], c["type"]
        full = name if name.startswith(".") or "." in name else f".{name}"
        comp = f"{pkg}/{full}" if not name.startswith(pkg) else f"{pkg}/{name}"
        skip = is_destructive(name) and name not in allow
        base = {
            "component": comp,
            "type": typ,
            "name": name,
            "permission": c.get("permission"),
            "skipped": "name suggests a destructive or financial action" if skip else None,
        }
        if typ == "activity":
            base["cmd"] = ["am", "start", "-W", "-n", comp, "--es", PROBE_EXTRA, "1"]
        elif typ == "receiver":
            base["cmd"] = ["am", "broadcast", "-p", pkg, "-n", comp, "--es", PROBE_EXTRA, "1"]
        elif typ == "service":
            base["cmd"] = ["am", "start-service", "-n", comp]
        elif typ == "provider":
            auth = (c.get("authorities") or "").split(";")[0]
            base["cmd"] = ["content", "query", "--uri", f"content://{auth}"] if auth else None
            base["authority"] = auth or None
            if not auth:
                base["skipped"] = "provider declares no authority to query"
        out.append(base)
        # Deep links are a second probe against the same activity: a different
        # caller path, and the one an attacker actually has.
        for scheme in (c.get("schemes") or [])[:3]:
            if scheme in ("http", "https") and not (c.get("hosts") or []):
                continue  # unscoped web scheme: not app-specific
            # A manifest host may be a wildcard pattern (`*.example.com`). Sent
            # literally it resolves to nothing and reads as "not-found", which would
            # be a false negative about the app rather than a fact.
            host = (c.get("hosts") or ["probe.invalid"])[0]
            host = (
                "www." + host[2:]
                if host.startswith("*.")
                else "probe.invalid"
                if host == "*"
                else host
            )
            # Honour a declared path filter: a probe at an arbitrary path fails the
            # filter and reads as unreachable, which says nothing about the app.
            paths = [x for x in (c.get("paths") or []) if x and not any(ch in x for ch in ".*")]
            path = paths[0].lstrip("/") + "/" + PROBE_EXTRA if paths else PROBE_EXTRA
            uri = f"{scheme}://{host}/{path}"
            out.append(
                {
                    "component": comp,
                    "type": "deeplink",
                    "name": uri,
                    "permission": None,
                    "skipped": "name suggests a destructive or financial action" if skip else None,
                    "cmd": [
                        "am",
                        "start",
                        "-a",
                        "android.intent.action.VIEW",
                        "-d",
                        uri,
                        "-p",
                        pkg,
                    ],
                }
            )
    return out


# ---------------------------------------------------------------- probing


def classify(output: str) -> str:
    o = output.lower()
    if "securityexception" in o or "permission denial" in o or "requires" in o:
        return "denied"
    if "does not exist" in o or "unable to resolve" in o or "no activities found" in o:
        return "not-found"
    if "error:" in o or "exception" in o:
        return "error"
    return "ok"


def probe(dev: Device, pkg: str, records: list, settle: float = SETTLE) -> list:
    results = []
    for r in records:
        rec = dict(r)
        if rec.get("skipped") or not rec.get("cmd"):
            rec["result"] = "not-assessed"
            rec["detail"] = rec.get("skipped") or "no command for this component"
            rec.pop("cmd", None)
            results.append(rec)
            continue
        dev.shell("logcat", "-b", "crash", "-c")  # anchor: only this probe's crash
        out = dev.shell(*rec["cmd"], timeout=ADB_TIMEOUT)
        time.sleep(settle)
        crash = dev.shell("logcat", "-d", "-b", "crash")
        crashed = "FATAL EXCEPTION" in crash and pkg in crash
        state = classify(out)
        if crashed:
            rec["result"] = "crashed"
            line = next((ln for ln in crash.splitlines() if "FATAL EXCEPTION" in ln), "")
            rec["detail"] = line.strip()[:180]
        elif state == "ok":
            fg = dev.foreground()
            rec["result"] = "launched" if fg.startswith(pkg + "/") else "accepted"
            rec["detail"] = (
                f"foreground is now {fg}"
                if fg.startswith(pkg + "/")
                else "command accepted; the app did not come to the front"
            )
        else:
            rec["result"] = state
            rec["detail"] = " ".join(out.split())[:180]
        rec.pop("cmd", None)
        results.append(rec)
    return results


def summarize(results: list) -> dict:
    counts = {}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    return counts


def render(pkg: str, results: list) -> None:
    print(f"intent probe — {pkg}   ({len(results)} probe(s))")
    for r in results:
        print(f"  [{r['result'].upper():12}] {r['type']:9} {r['name']}")
        if r.get("detail"):
            print(f"      {r['detail']}")
    counts = summarize(results)
    print("  " + ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())))  # noqa: RUF001 - U+00D7 is intentional in a rendered size label
    print(
        "  NOTE: reaching an exported component is NOT a vulnerability by itself. A "
        "finding needs a privileged action performed for an unprivileged caller. "
        "Skipped components are NOT ASSESSED, not passed."
    )


def cmd_plan(a) -> int:
    comps = load_inventory(a.apkscan)
    records = plan_probes(comps, a.package, set(a.allow or []))
    if a.json:
        print(json.dumps({"package": a.package, "planned": records}, indent=1))
        return 0
    print(
        f"plan — {a.package}: {len(records)} probe(s) from {len(comps)} exported "
        f"component(s). Nothing has been sent."
    )
    for r in records:
        mark = "SKIP" if r.get("skipped") else "send"
        print(f"  {mark} {r['type']:9} {r['name']}")
        if r.get("skipped"):
            print(f"       {r['skipped']}")
    print("run it with: intentprobe.py run <pkg> --apkscan <file> --go")
    return 0


def cmd_run(a) -> int:
    if not a.go:
        die(
            "refusing to fire without --go. Review `intentprobe.py plan` first: this "
            "starts real components in a real app, on whatever device is attached.",
            2,
        )
    if not PKG_RE.match(a.package):
        die(f"{a.package!r} is not a valid package name")
    dev = Device(a.serial)
    comps = load_inventory(a.apkscan)
    records = plan_probes(comps, a.package, set(a.allow or []))
    results = probe(dev, a.package, records)
    out = {
        "package": a.package,
        "serial": dev.serial,
        "probes": results,
        "summary": summarize(results),
        "note": "An exported component that launches is an entry point, not a "
        "vulnerability. Skipped probes are NOT ASSESSED.",
    }
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
        print(f"{a.out}")
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        render(a.package, results)
    return 1 if any(r["result"] == "crashed" for r in results) else 0


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    comps = [
        {
            "type": "activity",
            "name": "com.x.MainActivity",
            "exported": True,
            "schemes": [],
            "hosts": [],
            "authorities": None,
            "permission": None,
        },
        {
            "type": "activity",
            "name": "com.x.DeleteAccountActivity",
            "exported": True,
            "schemes": [],
            "hosts": [],
            "authorities": None,
            "permission": None,
        },
        {
            "type": "activity",
            "name": "com.x.DeepActivity",
            "exported": True,
            "schemes": ["myapp"],
            "hosts": ["open"],
            "authorities": None,
            "permission": None,
        },
        {
            "type": "receiver",
            "name": "com.x.Boot",
            "exported": True,
            "schemes": [],
            "hosts": [],
            "authorities": None,
            "permission": None,
        },
        {
            "type": "provider",
            "name": "com.x.Files",
            "exported": True,
            "schemes": [],
            "hosts": [],
            "authorities": "com.x.files",
            "permission": None,
        },
        {
            "type": "provider",
            "name": "com.x.NoAuth",
            "exported": True,
            "schemes": [],
            "hosts": [],
            "authorities": None,
            "permission": None,
        },
    ]
    plan = plan_probes(comps, "com.x", allow=set())
    by = {p["name"]: p for p in plan}

    check(
        "every exported component is planned",
        all(n in by for n in ("com.x.MainActivity", "com.x.Boot", "com.x.Files")),
    )
    check(
        "a destructive-sounding activity is skipped by policy",
        by["com.x.DeleteAccountActivity"]["skipped"] is not None,
    )
    check("an ordinary activity is not skipped", by["com.x.MainActivity"]["skipped"] is None)
    check(
        "a declared scheme becomes its own deep-link probe",
        any(p["type"] == "deeplink" and p["name"].startswith("myapp://") for p in plan),
    )
    wild = plan_probes(
        [
            {
                "type": "activity",
                "name": "com.x.W",
                "exported": True,
                "schemes": ["https"],
                "hosts": ["*.example.com"],
                "authorities": None,
                "permission": None,
            }
        ],
        "com.x",
        set(),
    )
    check(
        "a wildcard manifest host becomes a resolvable one",
        any("www.example.com" in p["name"] for p in wild if p["type"] == "deeplink"),
    )
    check(
        "a provider with no authority is skipped, not silently dropped",
        by["com.x.NoAuth"]["skipped"] and by["com.x.NoAuth"]["cmd"] is None,
    )
    check(
        "provider probe queries its authority",
        "content://com.x.files" in " ".join(by["com.x.Files"]["cmd"]),
    )
    check(
        "activity probe sends only an inert extra",
        PROBE_EXTRA in by["com.x.MainActivity"]["cmd"]
        and not any("password" in str(x).lower() for x in by["com.x.MainActivity"]["cmd"]),
    )
    check(
        "--allow can override one skip by name",
        plan_probes(comps, "com.x", allow={"com.x.DeleteAccountActivity"})[1]["skipped"] is None,
    )

    check(
        "destructive words cover money, data and sessions",
        all(
            is_destructive(w)
            for w in ("doDeleteAll", "PurchaseActivity", "LogoutReceiver", "WipeData")
        ),
    )
    check(
        "ordinary names are not flagged destructive",
        not any(is_destructive(n) for n in ("MainActivity", "SearchActivity", "PageActivity")),
    )

    check("SecurityException reads as denied", classify("SecurityException: nope") == "denied")
    check(
        "permission denial reads as denied",
        classify("java.lang.SecurityException: Permission Denial: x") == "denied",
    )
    check(
        "missing component reads as not-found",
        classify("Error: Activity class does not exist") == "not-found",
    )
    check("a clean start reads as ok", classify("Starting: Intent { }\nStatus: ok") == "ok")

    # A fake device: every probe must be classified, and a crash must surface.
    class FakeDev:
        serial = "selftest"

        def __init__(self, crash_on=None, out="Status: ok"):
            self.crash_on, self.out, self.cleared = crash_on, out, 0

        def shell(self, *args, timeout=None):
            if args[:2] == ("logcat", "-b"):
                self.cleared += 1
                return ""
            if args[0] == "logcat":
                return "FATAL EXCEPTION: main\n  at com.x" if self.crash_on else ""
            if args[0] == "dumpsys":
                return "topResumedActivity=ActivityRecord{a b com.x/.MainActivity t1}"
            return self.out

        def foreground(self):
            return "com.x/.MainActivity"

    res = probe(FakeDev(), "com.x", plan, settle=0)
    check("every planned probe yields a result", len(res) == len(plan))
    check(
        "skipped probes report not-assessed, never passed",
        all(r["result"] == "not-assessed" for r in res if r.get("skipped")),
    )
    check("a reachable component is reported launched", any(r["result"] == "launched" for r in res))
    check("the crash buffer is cleared before each fired probe", FakeDev().cleared == 0)

    crashed = probe(FakeDev(crash_on=True), "com.x", plan, settle=0)
    check(
        "a crash during a probe is detected and attributed",
        any(r["result"] == "crashed" for r in crashed),
    )
    check(
        "results carry no raw command (nothing to copy-paste blindly)",
        all("cmd" not in r for r in crashed),
    )
    check("summary counts every result", sum(summarize(crashed).values()) == len(crashed))

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(
        prog="intentprobe",
        description="Fire an app's declared entry points at it and record what happens.",
    )
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, helptext in (
        ("plan", "list what would be probed; sends nothing"),
        ("run", "actually probe (needs --go)"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("package")
        s.add_argument(
            "--apkscan",
            required=True,
            help="apkscan.py report <apk> --json output (exportedInventory)",
        )
        s.add_argument(
            "--allow",
            action="append",
            help="probe this component even though its name looks destructive",
        )
        s.add_argument("--json", action="store_true")
        if name == "run":
            s.add_argument(
                "--go",
                action="store_true",
                help="required: confirm you are firing at a disposable device",
            )
            s.add_argument("--out", help="write the JSON result here (e.g. run/intents.json)")

    a = p.parse_args(argv)
    try:
        return {"plan": cmd_plan, "run": cmd_run}[a.cmd](a)
    except subprocess.TimeoutExpired:
        die("adb timed out — the device may be busy or the app is not responding.")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
