#!/usr/bin/env python3
"""Non-root package and runtime diagnostics for an installed Android app.

Distils `dumpsys package` (700+ lines for one app) plus logcat and meminfo into
a short, structured report. Standard library only; no root required, so it works
on the release-signed Google Play emulator images.

What this CANNOT do without root: read /data/data, inspect app databases or
SharedPreferences, or intercept TLS traffic. Those need a debuggable build
(`run-as`), a rooted/AOSP image, or a proxy with a user CA the app trusts.

Exit codes: 0 ok | 1 package not installed / nothing found | 2 usage or device error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

ADB_TIMEOUT = 120
# Dot-separated Java identifiers. Also the injection guard for anything that
# reaches `adb shell`, alongside shlex quoting.
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


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
        if root:
            cand = os.path.join(root, "platform-tools", "adb")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    die("adb not found. Install Android platform-tools or set ANDROID_HOME.")


def find_aapt2() -> str | None:
    exe = shutil.which("aapt2")
    if exe:
        return exe
    for root in (os.environ.get("ANDROID_HOME"), os.path.expanduser("~/Library/Android/sdk")):
        bt = os.path.join(root or "", "build-tools")
        if os.path.isdir(bt):
            for ver in sorted(os.listdir(bt), reverse=True):
                cand = os.path.join(bt, ver, "aapt2")
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return None


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
            die("no device attached.", 1)
        if len(devs) > 1:
            die(f"{len(devs)} devices attached: {', '.join(devs)}. Pass --serial.")
        return devs[0]

    def run(self, *args, binary=False, timeout=ADB_TIMEOUT):
        return subprocess.run(
            [self.adb, "-s", self.serial, *list(args)],
            capture_output=True,
            timeout=timeout,
            text=not binary,
        )

    def user(self) -> str:
        """The foreground Android user. A work profile or secondary user makes a
        hardcoded 0 report the wrong package state."""
        if getattr(self, "_user", None) is None:
            out = self.shell("cmd", "activity", "get-current-user").strip()
            self._user = out if out.isdigit() else "0"
        return self._user

    def shell(self, *args, timeout=ADB_TIMEOUT) -> str:
        # `adb shell` joins its arguments and runs them through the DEVICE's sh,
        # so an unquoted `;` in any value executes on the device. Quote each one.
        quoted = [shlex.quote(str(a)) for a in args]
        return (self.run("shell", *quoted, timeout=timeout).stdout or "").replace("\r\n", "\n")


# ---------------------------------------------------------------- parsing

SECTION = re.compile(
    r"^\s*(declared permissions|requested permissions|"
    r"install permissions|runtime permissions|"
    r"disabled components|enabled components):\s*$"
)


def parse_sections(dump: str) -> dict[str, list[str]]:
    """Pull the indented blocks out of `dumpsys package` by heading."""
    out: dict[str, list[str]] = {}
    current, indent = None, 0
    for line in dump.splitlines():
        m = SECTION.match(line)
        if m:
            current = m.group(1)
            indent = len(line) - len(line.lstrip())
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        if not line.strip():
            continue
        if (len(line) - len(line.lstrip())) <= indent:
            current = None
            continue
        out[current].append(line.strip())
    return out


def parse_perm_line(line: str) -> tuple[str, bool | None]:
    """'android.permission.CAMERA: granted=true, flags=[..]' -> (name, True)."""
    name = line.split(":", 1)[0].strip()
    m = re.search(r"granted=(true|false)", line)
    return name, (m.group(1) == "true") if m else None


def identity(dump: str) -> dict:
    def grab(pattern, default=""):
        m = re.search(pattern, dump)
        return m.group(1) if m else default

    return {
        "versionName": grab(r"versionName=(\S+)"),
        "versionCode": grab(r"versionCode=(\d+)"),
        "minSdk": grab(r"minSdk=(\d+)"),
        "targetSdk": grab(r"targetSdk=(\d+)"),
        "primaryCpuAbi": grab(r"primaryCpuAbi=(\S+)", "(none — no native code)"),
        "firstInstallTime": grab(r"firstInstallTime=(.+)").strip(),
        "lastUpdateTime": grab(r"lastUpdateTime=(.+)").strip(),
        "installer": (lambda w: "(none — sideloaded or preinstalled)" if w in ("null", "") else w)(
            grab(r"installerPackageName=(\S+)")
        ),
        "signatureDigest": grab(r"signatures:\[([0-9a-f]+)"),
        "dataDir": grab(r"dataDir=(\S+)"),
        "uid": grab(r"appId=(\d+)"),  # `userId=` is the Android *user*, not the app uid
        "flags": grab(r"pkgFlags=\[(.*?)\]").strip(),
    }


# ---------------------------------------------------------------- commands


def _dump_or_die(dev: Device, pkg: str) -> str:
    if not PKG_RE.match(pkg):
        die(f"{pkg!r} is not a valid package name (expected e.g. com.example.app)")
    dump = dev.shell("dumpsys", "package", pkg)
    if not dev.shell("pm", "path", "--user", dev.user(), pkg).strip():
        die(f"{pkg} is not installed on {dev.serial}", 1)
    return dump


def dangerous_set(dev: Device) -> set[str]:
    """Ask the platform which permissions are 'dangerous' rather than hardcoding
    a list that rots with each API level."""
    # -g is required: `pm list permissions -d` alone returns only the handful of
    # ungrouped dangerous permissions (3 on API 35), not the ~129 real ones.
    # Entries are indented under their group, so strip before matching.
    out = dev.shell("pm", "list", "permissions", "-g", "-d")
    return {
        ln.strip().split(":", 1)[1]
        for ln in out.splitlines()
        if ln.strip().startswith("permission:")
    }


def cmd_report(dev: Device, a) -> int:
    pkg = a.package
    dump = _dump_or_die(dev, pkg)
    ident = identity(dump)
    secs = parse_sections(dump)
    dangerous = dangerous_set(dev)

    requested = sorted(set(secs.get("requested permissions", [])))
    runtime = [parse_perm_line(ln) for ln in secs.get("runtime permissions", [])]
    granted = sorted(n for n, g in runtime if g)
    denied = sorted(n for n, g in runtime if g is False)
    apks = [
        ln.split(":", 1)[1].strip()
        for ln in dev.shell("pm", "path", "--user", dev.user(), pkg).splitlines()
        if ln.startswith("package:")
    ]

    data = {
        "package": pkg,
        **ident,
        "apks": apks,
        "splitApks": len(apks) > 1,
        "requestedPermissions": requested,
        "dangerousRequested": sorted(set(requested) & dangerous),
        "runtimeGranted": granted,
        "runtimeDenied": denied,
        "declaresOwnPermissions": sorted(
            {ln.split(":")[0] for ln in secs.get("declared permissions", [])}
        ),
        "debuggable": "DEBUGGABLE" in ident["flags"],
        "system": "SYSTEM" in ident["flags"],
    }

    if a.json:
        print(json.dumps(data, indent=1))
        return 0

    print(f"{pkg}  {ident['versionName']} (code {ident['versionCode']})")
    print(f"  sdk          min {ident['minSdk']} / target {ident['targetSdk']}")
    print(f"  abi          {ident['primaryCpuAbi']}")
    print(f"  installer    {ident['installer']}")
    print(f"  installed    {ident['firstInstallTime']}  (updated {ident['lastUpdateTime']})")
    print(f"  signer       {ident['signatureDigest'] or '?'}")
    print(f"  uid/dataDir  {ident['uid']}  {ident['dataDir']}")
    print(f"  flags        {ident['flags']}")
    print(f"  apks         {len(apks)}" + ("  (split APKs)" if len(apks) > 1 else ""))
    for p in apks:
        print(f"                 {p}")
    print(
        f"  permissions  {len(requested)} requested, "
        f"{len(data['dangerousRequested'])} dangerous, "
        f"{len(granted)} runtime-granted, {len(denied)} denied"
    )
    if data["dangerousRequested"]:
        print("  dangerous:")
        for p in data["dangerousRequested"]:
            state = (
                "granted"
                if p in granted
                else "denied"
                if p in denied
                else "not yet requested at runtime"
            )
            print(f"    - {p.replace('android.permission.', '')}  [{state}]")
    if data["declaresOwnPermissions"]:
        print(f"  declares     {', '.join(data['declaresOwnPermissions'])}")
    if data["debuggable"]:
        print(
            "  NOTE: DEBUGGABLE build — `run-as` works, so app-private storage "
            "is readable without root."
        )
    return 0


def cmd_permissions(dev: Device, a) -> int:
    pkg = a.package
    dump = _dump_or_die(dev, pkg)
    secs = parse_sections(dump)
    dangerous = dangerous_set(dev)
    requested = sorted(set(secs.get("requested permissions", [])))
    runtime = dict(parse_perm_line(ln) for ln in secs.get("runtime permissions", []))
    install = dict(parse_perm_line(ln) for ln in secs.get("install permissions", []))

    if not requested:
        print(f"{pkg} requests no permissions")
        return 0
    for p in requested:
        short = p.replace("android.permission.", "")
        if p in runtime:
            state = "GRANTED " if runtime[p] else "denied  "
            kind = "runtime"
        elif p in install:
            state = "GRANTED " if install[p] else "denied  "
            kind = "install"
        else:
            state = "--      "
            kind = "not held"
        flag = " *DANGEROUS*" if p in dangerous else ""
        print(f"{state} {kind:9} {short}{flag}")
    print(
        f"\n{len(requested)} requested; "
        f"{len(set(requested) & dangerous)} classed dangerous by this device "
        f"(API {dev.shell('getprop', 'ro.build.version.sdk').strip()})."
    )
    print(
        "Runtime state reflects what has actually been granted — installing with "
        "`adb install -g` pre-grants everything and will make a privacy review "
        "look falsely permissive."
    )
    return 0


def cmd_crashes(dev: Device, a) -> int:
    pkg = a.package
    crash = dev.shell("logcat", "-b", "crash", "-d", "-v", "threadtime")
    lines = [ln for ln in crash.splitlines() if pkg in ln or "FATAL EXCEPTION" in ln]
    anr = [
        ln
        for ln in dev.shell("logcat", "-b", "main", "-d", "-v", "brief").splitlines()
        if "ANR in" in ln and pkg in ln
    ]
    if not lines and not anr:
        print(
            f"no crashes or ANRs for {pkg} in the current log buffers "
            f"(buffers are ring buffers — clear with `adb logcat -c` before a run "
            f"so you know what is fresh)"
        )
        return 1
    if lines:
        print(f"--- crash buffer ({len(lines)} lines) ---")
        print("\n".join(lines[-a.lines :]))
    if anr:
        print(f"--- ANRs ({len(anr)}) ---")
        print("\n".join(anr))
    return 0


def cmd_perf(dev: Device, a) -> int:
    pkg = a.package
    mem = dev.shell("dumpsys", "meminfo", pkg)
    if "No process found" in mem:
        print(
            f"{pkg} is not running — start it first (memory is only measurable for a live process)",
            file=sys.stderr,
        )
        return 1
    total = re.search(r"TOTAL PSS:\s*(\d+)", mem) or re.search(r"TOTAL\s+(\d+)", mem)
    java = re.search(r"Java Heap:\s*(\d+)", mem)
    native = re.search(r"Native Heap:\s*(\d+)", mem)
    print(f"{pkg} memory")
    print(f"  total PSS    {int(total.group(1)) // 1024 if total else '?'} MB")
    if java:
        print(f"  java heap    {int(java.group(1)) // 1024} MB")
    if native:
        print(f"  native heap  {int(native.group(1)) // 1024} MB")

    gfx = dev.shell("dumpsys", "gfxinfo", pkg)
    frames = re.search(r"Total frames rendered: (\d+)", gfx)
    janky = re.search(r"Janky frames: (\d+) \(([\d.]+)%\)", gfx)
    if frames and janky:
        print(
            f"  frames       {frames.group(1)} rendered, {janky.group(1)} janky ({janky.group(2)}%)"
        )
    else:
        print("  frames       (no gfxinfo yet — interact with the app, then re-run)")
    return 0


def cmd_apk(dev: Device, a) -> int:
    pkg = a.package
    paths = [
        ln.split(":", 1)[1].strip()
        for ln in dev.shell("pm", "path", "--user", dev.user(), pkg).splitlines()
        if ln.startswith("package:")
    ]
    if not paths:
        die(f"{pkg} is not installed", 1)
    print(f"{pkg}: {len(paths)} APK(s)")
    for p in paths:
        size = dev.shell("stat", "-c", "%s", p).strip()
        print(f"  {p}  ({int(size) // 1024 if size.isdigit() else '?'} KB)")
    if not a.pull:
        print("\n(--pull DIR to copy them locally, hash them, and read the manifest)")
        return 0

    os.makedirs(a.pull, exist_ok=True)
    aapt2 = find_aapt2()
    for p in paths:
        local = os.path.join(a.pull, os.path.basename(p))
        dev.run("pull", p, local, timeout=600)
        if not os.path.isfile(local):
            print(f"  failed to pull {p}", file=sys.stderr)
            continue
        with open(local, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        print(f"\n{local}\n  sha256 {digest}")
        if aapt2:
            badging = subprocess.run(
                [aapt2, "dump", "badging", local], capture_output=True, text=True
            ).stdout
            for key in (
                "package:",
                "sdkVersion:",
                "targetSdkVersion:",
                "application-label:",
                "native-code:",
            ):
                for line in badging.splitlines():
                    if line.startswith(key):
                        print(f"  {line}")
                        break
    if not aapt2:
        print(
            "\n(aapt2 not found — install Android SDK build-tools to read the "
            "manifest; it is not part of platform-tools)"
        )
    return 0


DUMP_FIXTURE = """
Packages:
  Package [com.example.app] (abc123):
    appId=10207
    versionCode=1023052 minSdk=23 targetSdk=30
    versionName=1.2.3
    primaryCpuAbi=arm64-v8a
    pkgFlags=[ HAS_CODE ALLOW_BACKUP ]
    lastUpdateTime=2026-08-02 16:58:34
    installerPackageName=com.android.vending
    signatures=PackageSignatures{2acd2db version:3, signatures:[8b8a3ff5], past signatures:[]}
    declared permissions:
      com.example.app.MY_PERM: prot=signature
    requested permissions:
      android.permission.CAMERA
      android.permission.INTERNET
    install permissions:
      android.permission.INTERNET: granted=true
    User 0:
      dataDir=/data/user/0/com.example.app
      firstInstallTime=2026-08-01 10:00:00
      runtime permissions:
        android.permission.CAMERA: granted=false, flags=[ USER_SET ]
"""


def self_test() -> int:
    """Offline checks for dumpsys parsing. No device, no adb -- runs in CI."""
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    ident = identity(DUMP_FIXTURE)
    check("versionName parsed", ident["versionName"] == "1.2.3")
    check("versionCode parsed", ident["versionCode"] == "1023052")
    check("min/target sdk parsed", (ident["minSdk"], ident["targetSdk"]) == ("23", "30"))
    check("uid comes from appId, not userId", ident["uid"] == "10207")
    check("signer digest parsed", ident["signatureDigest"] == "8b8a3ff5")
    check("Play installer surfaced", ident["installer"] == "com.android.vending")
    check(
        "null installer reads as sideloaded",
        "sideloaded" in identity("installerPackageName=null")["installer"],
    )

    secs = parse_sections(DUMP_FIXTURE)
    check(
        "requested permissions collected",
        sorted(secs["requested permissions"])
        == ["android.permission.CAMERA", "android.permission.INTERNET"],
    )
    check("declared permissions kept separate", len(secs["declared permissions"]) == 1)
    check("runtime permissions collected", len(secs["runtime permissions"]) == 1)
    check(
        "granted=false is parsed as denied, not missing",
        parse_perm_line(secs["runtime permissions"][0]) == ("android.permission.CAMERA", False),
    )
    check(
        "install permission parsed as granted",
        parse_perm_line(secs["install permissions"][0])[1] is True,
    )
    check(
        "a permission with no granted= yields None",
        parse_perm_line("android.permission.FOO")[1] is None,
    )

    check("valid package accepted", bool(PKG_RE.match("com.example.app")))
    for bad in ("foo;touch /tmp/x", "com", "", "no spaces here"):
        check(f"package {bad!r} rejected", not PKG_RE.match(bad))

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(prog="pkgdiag", description="Non-root Android package diagnostics.")
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in [
        ("report", "one-shot summary: identity, provenance, permissions"),
        ("permissions", "every requested permission and its real state"),
        ("crashes", "crashes and ANRs for this package"),
        ("perf", "memory and frame-jank of the running process"),
        ("apk", "APK paths, sizes, hashes, manifest"),
    ]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("package")
        if name == "report":
            sp.add_argument("--json", action="store_true")
        if name == "crashes":
            sp.add_argument("--lines", type=int, default=60)
        if name == "apk":
            sp.add_argument("--pull", metavar="DIR", help="copy APKs here and hash them")

    a = p.parse_args(argv)
    dev = Device(a.serial)
    handlers = {
        "report": cmd_report,
        "permissions": cmd_permissions,
        "crashes": cmd_crashes,
        "perf": cmd_perf,
        "apk": cmd_apk,
    }
    try:
        return handlers[a.cmd](dev, a)
    except subprocess.TimeoutExpired:
        die("adb timed out — the device may be busy or disconnected.")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
