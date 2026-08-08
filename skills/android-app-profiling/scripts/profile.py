#!/usr/bin/env python3
"""Black-box runtime performance profiling of an installed Android app — no root,
no source, no debuggable/profileable flag. Standard library only.

Everything here works on a STOCK release APK on a stock device/emulator:
  startup   `am start -W` process-cold TotalTime, median of N, vs vitals excessive
  frames    `dumpsys gfxinfo` HWUI jank rate + frame-time percentiles
  memory    `dumpsys meminfo` total PSS + Java/Native heap PSS categories
  dexopt    `dumpsys package dexopt` compilation status (profile-guided AOT signal)
  trace     capture a Perfetto system trace (no root) to hand to ui.perfetto.dev

For the quick one-shot memory+jank number use `android-package-diagnostics perf`;
this skill is the deep, benchmarked version. Heap dumps, allocation tracking and
Studio Profiler need a profileable/debuggable app or root — out of scope (see
references).

**Emulator caveat:** an emulator has no real GPU, thermal, or power behavior, so
these numbers are indicative for finding regressions and gross problems, NOT
device-grade benchmarks. Report them as such.

Exit codes: 0 ok | 1 not installed / no data | 2 usage or device error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

ADB_TIMEOUT = 120
# Android vitals "excessive" boundaries (ms) and a rough "good" ceiling.
# Android-vitals "excessive" cold-start boundary (ms). We only measure PROCESS-COLD
# (force-stopped) launches — not first-install/storage-cold — so we report against
# the excessive threshold rather than awarding a "good" grade.
COLD_EXCESSIVE_MS = 5000
COLD_FAST_MS = 1500


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def find_adb() -> str:
    exe = shutil.which("adb")
    if exe:
        return exe
    for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.path.expanduser("~/Library/Android/sdk"),
                 os.path.expanduser("~/Android/Sdk")):
        if root:
            cand = os.path.join(root, "platform-tools", "adb")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    die("adb not found. Install Android platform-tools or set ANDROID_HOME.")


class Device:
    def __init__(self, serial: str | None):
        self.adb = find_adb()
        self.serial = serial or os.environ.get("ANDROID_SERIAL") or self._pick()

    def _pick(self) -> str:
        out = subprocess.run([self.adb, "devices"], capture_output=True,
                             text=True, timeout=ADB_TIMEOUT).stdout
        devs = [l.split("\t")[0] for l in out.splitlines()[1:]
                if l.strip() and l.endswith("\tdevice")]
        if not devs:
            die("no device attached.", 1)
        if len(devs) > 1:
            die(f"{len(devs)} devices attached: {', '.join(devs)}. Pass --serial.")
        if not devs[0].startswith("emulator-"):
            self.serial = devs[0]
            if "emulator" not in self.shell("getprop", "ro.build.characteristics"):
                self.is_emulator = False
                return devs[0]
        self.is_emulator = True
        return devs[0]

    is_emulator = True

    def run(self, *args, binary=False, timeout=ADB_TIMEOUT):
        return subprocess.run([self.adb, "-s", self.serial] + list(args),
                              capture_output=True, timeout=timeout, text=not binary)

    def shell(self, *args, timeout=ADB_TIMEOUT) -> str:
        import shlex
        q = [shlex.quote(str(a)) for a in args]   # device-shell injection guard
        return (self.run("shell", *q, timeout=timeout).stdout or "").replace("\r\n", "\n")

    def installed(self, pkg: str) -> bool:
        return bool(self.shell("pm", "path", "--user", "0", pkg).strip())

    def launch_activity(self, pkg: str) -> str | None:
        brief = self.shell("cmd", "package", "resolve-activity", "--brief", "--user", "0",
                           "-a", "android.intent.action.MAIN",
                           "-c", "android.intent.category.LAUNCHER", pkg)
        return next((l.strip() for l in brief.splitlines()
                     if "/" in l and not l.startswith("No ")), None)


# ---------------------------------------------------------------- parsing


def parse_amstart(out: str) -> dict:
    def g(k):
        m = re.search(rf"^{k}:\s*(\d+)", out, re.M)
        return int(m.group(1)) if m else None
    status = re.search(r"^Status:\s*(\w+)", out, re.M)
    ls = re.search(r"^LaunchState:\s*(\w+)", out, re.M)
    return {"status": status.group(1) if status else None,
            "thisTime": g("ThisTime"), "totalTime": g("TotalTime"),
            "waitTime": g("WaitTime"), "launchState": ls.group(1) if ls else None}


def classify_startup(ms: int | None) -> str:
    if ms is None:
        return "unknown"
    if ms >= COLD_EXCESSIVE_MS:
        return "exceeds-vitals-excessive"
    return "fast" if ms < COLD_FAST_MS else "acceptable"


def parse_gfxinfo(out: str) -> dict:
    def num(pat, cast=float):
        m = re.search(pat, out)
        return cast(m.group(1)) if m else None
    total = num(r"Total frames rendered:\s*(\d+)", int)
    jm = re.search(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", out)
    pct = {p: num(rf"{p}th percentile:\s*(\d+)ms", int)
           for p in ("50", "90", "95", "99")}
    return {"totalFrames": total,
            "jankyFrames": int(jm.group(1)) if jm else None,
            "jankyPercent": float(jm.group(2)) if jm else None,
            "frameTimeMsPercentiles": pct,
            "missedVsync": num(r"Number Missed Vsync:\s*(\d+)", int)}


def classify_jank(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    return "good" if pct < 1 else ("poor" if pct >= 5 else "ok")


def parse_meminfo(out: str) -> dict:
    def mb(pat):
        m = re.search(pat, out)
        return round(int(m.group(1)) / 1024, 1) if m else None
    return {"totalPssMb": mb(r"TOTAL PSS:\s*(\d+)") or mb(r"TOTAL\s+(\d+)"),
            "totalRssMb": mb(r"TOTAL RSS:\s*(\d+)"),
            "javaHeapMb": mb(r"Java Heap:\s*(\d+)"),
            "nativeHeapMb": mb(r"Native Heap:\s*(\d+)")}


def parse_dexopt(out: str, pkg: str) -> dict:
    # find the pkg block, then the first "abi: [status=X] [reason=Y]" under it
    lines = out.splitlines()
    for i, l in enumerate(lines):
        if f"[{pkg}]" in l:
            for j in range(i, min(i + 12, len(lines))):
                m = re.search(r"(\w[\w-]*): \[status=([\w-]+)\] \[reason=([\w-]+)\]",
                              lines[j])
                if m:
                    status = m.group(2)
                    return {"abi": m.group(1), "status": status, "reason": m.group(3),
                            "baselineProfileApplied": status == "speed-profile"}
    return {"status": None, "note": "no dexopt entry found for this package"}


# ---------------------------------------------------------------- commands


def cmd_startup(dev: Device, a) -> int:
    if not dev.installed(a.package):
        die(f"{a.package} is not installed", 1)
    activity = a.activity or dev.launch_activity(a.package)
    if not activity:
        die(f"could not resolve a launcher activity for {a.package}; pass --activity", 1)
    comp = activity if "/" in activity else f"{a.package}/{activity}"
    runs, states = [], []
    for i in range(a.runs):
        dev.shell("am", "force-stop", a.package)   # process-cold each run
        time.sleep(1.0)
        r = parse_amstart(dev.shell("am", "start", "-W", "-n", comp))
        if r["status"] != "ok" or r["totalTime"] is None:
            die(f"am start did not report a time (status={r['status']}); "
                f"is {comp} the right activity?", 1)
        runs.append(r["totalTime"])
        if r.get("launchState"):
            states.append(r["launchState"])
    median = int(statistics.median(runs))
    over = sum(1 for x in runs if x >= COLD_EXCESSIVE_MS)
    verdict = classify_startup(median)
    out = {"package": a.package, "activity": comp, "mode": "process-cold",
           "runs": runs, "medianTotalTimeMs": median,
           "samplesOverExcessive": over, "launchStates": states or None,
           "verdict": verdict, "coldExcessiveMs": COLD_EXCESSIVE_MS,
           "emulator": dev.is_emulator,
           "metric": "am start -W TotalTime (TTID-like proxy; not TTFD or field Vitals)"}
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"{a.package} process-cold startup: median {median} ms  [{verdict}]")
        print(f"  samples: {runs}"
              + (f"  launchState={states[0]}" if states else "")
              + (f"  ({over}/{len(runs)} over the {COLD_EXCESSIVE_MS} ms vitals "
                 f"'excessive' line)" if over else ""))
        print(f"  am start -W TotalTime is a TTID-like proxy — not time-to-full-"
              f"display and not field Vitals. 'cold' here = process-cold (force-"
              f"stopped), not first-install/storage-cold.")
        if dev.is_emulator:
            print("  note: emulator timing is indicative, not device-grade — confirm "
                  "on a physical device")
    return 0


def cmd_frames(dev: Device, a) -> int:
    if not dev.installed(a.package):
        die(f"{a.package} is not installed", 1)
    if a.reset:
        dev.shell("dumpsys", "gfxinfo", a.package, "reset")
        print(f"gfxinfo counters reset for {a.package} — exercise the app (scroll, "
              f"navigate), then re-run without --reset.")
        return 0
    g = parse_gfxinfo(dev.shell("dumpsys", "gfxinfo", a.package))
    if not g["totalFrames"]:
        die("no frames recorded — launch and interact with the app first (or run "
            "`frames --reset` then exercise it).", 1)
    verdict = classify_jank(g["jankyPercent"])
    out = {"package": a.package, **g, "verdict": verdict, "emulator": dev.is_emulator}
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"{a.package} frames: {g['totalFrames']} rendered, "
              f"{g['jankyPercent']}% janky  [{verdict}]")
        p = g["frameTimeMsPercentiles"]
        print(f"  frame time  50th {p['50']}  90th {p['90']}  95th {p['95']}  "
              f"99th {p['99']} ms   (budget 16.6 ms @60Hz)")
        print(f"  missed vsync {g['missedVsync']};  <1% janky is good, >=5% poor")
        print("  these are HWUI render stats (may miss SurfaceView/Compose-to-"
              "Surface paths); the 16.6 ms budget assumes 60 Hz — on 90/120 Hz or "
              "variable-refresh use a Perfetto FrameTimeline trace instead.")
        if g["totalFrames"] < 100:
            print(f"  note: only {g['totalFrames']} frames — exercise the app more "
                  f"(scroll/navigate) after `frames --reset` for a stable number")
    return 0


def cmd_memory(dev: Device, a) -> int:
    if not dev.installed(a.package):
        die(f"{a.package} is not installed", 1)
    out = dev.shell("dumpsys", "meminfo", a.package)
    if "No process found" in out:
        die(f"{a.package} is not running — start it first (memory needs a live process)", 1)
    m = parse_meminfo(out)
    if a.json:
        print(json.dumps({"package": a.package, **m}, indent=1))
    else:
        print(f"{a.package} memory:")
        print(f"  total PSS  {m['totalPssMb']} MB" + (f"   RSS {m['totalRssMb']} MB"
              if m["totalRssMb"] else ""))
        if m["javaHeapMb"] is not None:
            print(f"  java heap  {m['javaHeapMb']} MB   native heap {m['nativeHeapMb']} MB")
        print("  values are PSS (proportional set size) from dumpsys meminfo — the "
              "standard per-app RAM cost; Java/Native are the PSS heap categories. "
              "Watch for monotonic growth across runs (a leak); compare idle vs load.")
    return 0


def cmd_dexopt(dev: Device, a) -> int:
    d = parse_dexopt(dev.shell("dumpsys", "package", "dexopt"), a.package)
    if a.json:
        print(json.dumps({"package": a.package, **d}, indent=1))
        return 0
    if not d.get("status"):
        print(f"{a.package}: {d.get('note', 'no dexopt info')}")
        return 1
    print(f"{a.package} compilation: status={d['status']} reason={d['reason']} "
          f"({d['abi']})")
    if d["baselineProfileApplied"]:
        print("  speed-profile — profile-guided AOT compilation is active (helps "
              "startup/jank). The profile may be a shipped baseline, a Play cloud "
              "profile, or runtime-derived; the source can't be told apart here.")
    else:
        print(f"  status={d['status']!r} means it is not currently profile-guided "
              f"AOT compiled. This alone does NOT prove a profile is missing or "
              f"queued (esp. for sideloaded/F-Droid installs) — for a real signal "
              f"check assets/dexopt/baseline.prof in the APK (android-apk-analysis) "
              f"or recheck after Play's background dexopt on a Play-installed app.")
    return 0


def cmd_trace(dev: Device, a) -> int:
    remote = "/data/misc/perfetto-traces/apptrace.pt"
    cats = ["sched", "freq", "idle", "am", "wm", "gfx", "view", "binder_driver"]
    print(f"capturing a {a.secs}s Perfetto system trace (no root needed)…")
    print("exercise the app now — launch it, scroll, navigate.")
    r = dev.run("shell", "perfetto", "-o", remote, "-t", f"{a.secs}s", *cats,
                timeout=a.secs + 60)
    err = (r.stderr or "")
    if "Wrote" not in ((r.stdout or "") + err):
        die(f"perfetto capture failed: {err.strip()[:300]}", 1)
    local = a.out or "apptrace.perfetto-trace"
    dev.run("pull", remote, local, timeout=120)
    dev.shell("rm", "-f", remote)
    if not os.path.isfile(local) or os.path.getsize(local) < 1024:
        die("trace pulled empty — the capture may have been interrupted", 1)
    print(f"\n{local} ({os.path.getsize(local) // 1024} KB)")
    print("analyze it (this is a protobuf trace, not parsed here):")
    print("  - open https://ui.perfetto.dev and load the file, OR")
    print("  - query with trace_processor_shell (SQL); see references/deep-profiling.md")
    print("  read the FrameTimeline track for per-frame jank attribution (app vs "
          "SurfaceFlinger), main-thread Choreographer#doFrame slices, and binder/"
          "lock contention.")
    print("  a non-empty trace is not proof of useful data — confirm the expected "
          "tracks loaded (some data sources can be denied). The trace embeds "
          "package/activity/thread/slice strings; treat it as sensitive and get "
          "consent before sharing it externally.")
    return 0


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    am = parse_amstart("Status: ok\nActivity: c/.M\nThisTime: 300\n"
                       "TotalTime: 812\nWaitTime: 838")
    check("am start parses TotalTime", am["totalTime"] == 812 and am["status"] == "ok")
    check("am start parses WaitTime", am["waitTime"] == 838)

    check("812ms classified fast", classify_startup(812) == "fast")
    check("5200ms exceeds vitals excessive",
          classify_startup(5200) == "exceeds-vitals-excessive")
    check("3000ms acceptable", classify_startup(3000) == "acceptable")
    check("am start parses LaunchState",
          parse_amstart("Status: ok\nLaunchState: COLD\nTotalTime: 300")["launchState"] == "COLD")

    gfx = parse_gfxinfo("Total frames rendered: 500\nJanky frames: 4 (0.80%)\n"
                        "50th percentile: 8ms\n90th percentile: 14ms\n"
                        "95th percentile: 18ms\n99th percentile: 40ms\n"
                        "Number Missed Vsync: 2")
    check("gfxinfo parses total frames", gfx["totalFrames"] == 500)
    check("gfxinfo parses janky percent", gfx["jankyPercent"] == 0.80)
    check("gfxinfo parses 99th percentile", gfx["frameTimeMsPercentiles"]["99"] == 40)
    check("0.8% jank is good", classify_jank(0.80) == "good")
    check("7% jank is poor", classify_jank(7.0) == "poor")
    check("3% jank is ok", classify_jank(3.0) == "ok")

    dx = parse_dexopt("Dexopt state:\n  [org.fdroid.fdroid]\n    path: /x/base.apk\n"
                      "      arm64: [status=verify] [reason=install] [primary-abi]",
                      "org.fdroid.fdroid")
    check("dexopt parses status", dx["status"] == "verify" and dx["reason"] == "install")
    check("verify is not baseline-applied", dx["baselineProfileApplied"] is False)
    dx2 = parse_dexopt("  [com.x]\n      arm64: [status=speed-profile] [reason=bg-dexopt]",
                       "com.x")
    check("speed-profile is baseline-applied", dx2["baselineProfileApplied"] is True)
    check("missing package -> no status",
          parse_dexopt("nothing here", "com.absent")["status"] is None)

    mem = parse_meminfo("               Pss  Private\n TOTAL PSS:    81920\n"
                        " Java Heap:    17408\n Native Heap:  29696")
    check("meminfo parses PSS to MB", mem["totalPssMb"] == 80.0)

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(prog="profile",
                                description="No-root runtime profiling of an installed app.")
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("startup", help="am start -W timing vs Android-vitals thresholds")
    s.add_argument("package")
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--activity", help="explicit component if auto-resolve is wrong")
    s.add_argument("--json", action="store_true")

    fr = sub.add_parser("frames", help="dumpsys gfxinfo jank + frame-time percentiles")
    fr.add_argument("package")
    fr.add_argument("--reset", action="store_true", help="zero counters, then exercise the app")
    fr.add_argument("--json", action="store_true")

    me = sub.add_parser("memory", help="dumpsys meminfo PSS/USS (app must be running)")
    me.add_argument("package")
    me.add_argument("--json", action="store_true")

    dx = sub.add_parser("dexopt", help="compilation status / baseline-profile signal")
    dx.add_argument("package")
    dx.add_argument("--json", action="store_true")

    tr = sub.add_parser("trace", help="capture a Perfetto system trace (no root)")
    tr.add_argument("package")
    tr.add_argument("--secs", type=int, default=15)
    tr.add_argument("--out", help="local output path")

    a = p.parse_args(argv)
    dev = Device(a.serial)
    handlers = {"startup": cmd_startup, "frames": cmd_frames, "memory": cmd_memory,
                "dexopt": cmd_dexopt, "trace": cmd_trace}
    try:
        return handlers[a.cmd](dev, a)
    except subprocess.TimeoutExpired:
        die("adb timed out — the device may be busy or disconnected.")


if __name__ == "__main__":
    sys.exit(main())
