#!/usr/bin/env python3
"""Play Store link -> package name -> app on a device. Standard library only.

Handles the deterministic half of installation: parsing the link, checking the
device and Play preconditions, reporting install state, sideloading an APK the
user already has, waiting for an install to land, and launching. Driving the
Play Store's own Install button is UI work -- that belongs to the
android-ui-driver skill; this script gives it `open-listing` and
`wait-installed` to bracket it.

It never downloads an APK. Pulling APKs out of Play with an unofficial client
violates Play's terms and risks the account; third-party mirrors have no
provenance. Supply your own APK or install through Play.

Exit codes: 0 ok | 1 precondition unmet / not installed | 2 usage or device error.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

ADB_TIMEOUT = 120
PLAY_STORE_PKG = "com.android.vending"
# A package name: dot-separated Java identifiers, at least two segments.
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- link parsing


def resolve_package(ref: str) -> str:
    """Extract the package name from a Play URL, a market:// URI, or a bare id."""
    ref = ref.strip().strip('"\'')
    if PKG_RE.match(ref):
        return ref
    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https", "market"):
        qs = parse_qs(parsed.query)
        for key in ("id", "package"):
            if key in qs and qs[key]:
                cand = qs[key][0]
                if PKG_RE.match(cand):
                    return cand
                die(f"the link's ?{key}= is {cand!r}, which is not a package name")
        if "goo.gl" in (parsed.netloc or "") or "app.goo.gl" in (parsed.netloc or ""):
            die("that is a shortened Play link; it has to be opened in a browser to "
                "reveal the real URL. Open it and pass the resulting "
                "play.google.com/store/apps/details?id=... link (or the package name).")
    die(f"could not find a package name in {ref!r}. Expected something like "
        f"https://play.google.com/store/apps/details?id=com.example.app, "
        f"market://details?id=com.example.app, or a bare com.example.app.")


# ---------------------------------------------------------------- device


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
            die("no device. Boot an emulator (or connect a phone) first.", 1)
        if len(devs) > 1:
            die(f"{len(devs)} devices attached: {', '.join(devs)}. Pass --serial.")
        if not devs[0].startswith("emulator-"):
            self.serial = devs[0]
            if "emulator" not in self.shell("getprop", "ro.build.characteristics"):
                die(f"{devs[0]} looks like a physical device. Name it explicitly so "
                    f"nothing installs onto a real phone by accident: --serial {devs[0]}")
        return devs[0]

    def run(self, *args, binary=False, timeout=ADB_TIMEOUT):
        cmd = [self.adb, "-s", self.serial] + list(args)
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=not binary)

    def shell(self, *args, timeout=ADB_TIMEOUT) -> str:
        # `adb shell` joins its arguments and runs them through the DEVICE's sh,
        # so an unquoted `;` in any value executes on the device. Quote each one.
        quoted = [shlex.quote(str(a)) for a in args]
        return (self.run("shell", *quoted, timeout=timeout).stdout or "").replace("\r\n", "\n")

    def user(self) -> str:
        """Foreground Android user; a work profile makes a hardcoded 0 wrong."""
        if getattr(self, "_user", None) is None:
            out = self.shell("cmd", "activity", "get-current-user").strip()
            self._user = out if out.isdigit() else "0"
        return self._user

    def prop(self, name: str) -> str:
        return self.shell("getprop", name).strip()

    # -- package state ----------------------------------------------------

    def paths(self, pkg: str) -> list[str]:
        """APK paths, or [] if not installed. Exact -- unlike `pm list packages`,
        which substring-matches and reports com.foo.bar for a query of com.foo."""
        out = self.shell("pm", "path", "--user", self.user(), pkg)
        return [l.split(":", 1)[1].strip() for l in out.splitlines()
                if l.startswith("package:")]

    def version(self, pkg: str) -> tuple[str, str]:
        out = self.shell("dumpsys", "package", pkg)
        name = re.search(r"versionName=(\S+)", out)
        code = re.search(r"versionCode=(\d+)", out)
        return (name.group(1) if name else "?", code.group(1) if code else "?")

    def installer(self, pkg: str) -> str:
        out = self.shell("dumpsys", "package", pkg)
        m = re.search(r"installerPackageName=(\S+)", out)
        who = m.group(1) if m else "null"
        # Play-installed apps report com.android.vending; sideloaded/preinstalled
        # ones report null, which is itself a useful provenance signal.
        return "(none — sideloaded or preinstalled)" if who == "null" else who

    def accounts(self) -> int:
        return len(re.findall(r"Account \{", self.shell("dumpsys", "account")))

    def focus(self) -> str:
        m = re.search(r"mCurrentFocus[^\n]*?([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)",
                      self.shell("dumpsys", "window"))
        return m.group(1) if m else "?"


# ---------------------------------------------------------------- commands


def cmd_resolve(a) -> int:
    print(resolve_package(a.ref))
    return 0


ANIMATION_KEYS = ("window_animation_scale", "transition_animation_scale",
                  "animator_duration_scale")


def emit(component: str, state: str, detail: str = "") -> None:
    """Machine-readable so an agent can grep a single line and self-correct."""
    print(f"STATUS {component}: {state}{(' ' + detail) if detail else ''}")


def cmd_preflight(a) -> int:
    dev = Device(a.serial)

    booted = dev.prop("sys.boot_completed") == "1"
    emit("device", "OK" if booted else "FAIL",
         dev.serial if booted else f"{dev.serial} has not finished booting "
         f"(sys.boot_completed != 1) — wait, or cold-boot the AVD")

    api = dev.prop("ro.build.version.sdk")
    emit("api_level", "OK" if api else "WARN",
         f"API {api} / Android {dev.prop('ro.build.version.release')}" if api else "unknown")

    abi = dev.prop("ro.product.cpu.abi")
    emit("cpu_abi", "OK" if abi else "WARN",
         f"{abi} — an APK without this ABI fails with INSTALL_FAILED_NO_MATCHING_ABIS"
         if abi else "unknown")

    tags = dev.prop("ro.build.tags")
    emit("build_tags", "OK", f"{tags} "
         f"({'release-keys: adb root unavailable' if 'release' in tags else 'debuggable image'})")

    has_play = bool(dev.paths(PLAY_STORE_PKG))
    emit("play_store", "OK" if has_play else "MISSING",
         "com.android.vending" if has_play else
         "this image is AOSP or google_apis, not google_apis_playstore. Installing "
         "from a Play link needs a Play-enabled image; install-apk still works.")

    n_acct = dev.accounts()
    signed_in = n_acct > 0
    emit("play_account", "OK" if signed_in else "MISSING",
         f"{n_acct} account(s)" if signed_in else
         "no Google account — a human must sign in to Play once, BY HAND. Do not "
         "automate Google sign-in (2FA/CAPTCHA, and it breaches Play's terms).")

    anim = {k: dev.shell("settings", "get", "global", k).strip() for k in ANIMATION_KEYS}
    anim_off = all(v in ("0", "0.0") for v in anim.values())
    if a.fix and not anim_off:
        for k in ANIMATION_KEYS:
            dev.shell("settings", "put", "global", k, "0")
        anim = {k: dev.shell("settings", "get", "global", k).strip() for k in ANIMATION_KEYS}
        anim_off = all(v in ("0", "0.0") for v in anim.values())
    emit("animations", "OK" if anim_off else "WARN",
         "disabled" if anim_off else
         f"on ({', '.join(f'{k.split(chr(95))[0]}={v}' for k, v in anim.items())}) — "
         f"animating screens make `uiautomator dump` fail with 'could not get idle "
         f"state'. Re-run with --fix to switch them off.")

    ok = booted and has_play and signed_in
    print(f"RESULT: {'ok' if ok else 'fail'}")
    if not booted:
        return 1
    if not (has_play and signed_in):
        print("Play installs are blocked (see MISSING above). `install-apk` with an "
              "APK you lawfully have still works.", file=sys.stderr)
        return 1
    return 0


def cmd_status(a) -> int:
    dev = Device(a.serial)
    pkg = resolve_package(a.ref)
    paths = dev.paths(pkg)
    if not paths:
        print(json.dumps({"package": pkg, "installed": False}, indent=1))
        return 1
    name, code = dev.version(pkg)
    print(json.dumps({"package": pkg, "installed": True, "versionName": name,
                      "versionCode": code, "installer": dev.installer(pkg),
                      "apks": paths, "splits": len(paths) > 1}, indent=1))
    return 0


def cmd_open_listing(a) -> int:
    """Open the Play listing on-device, then report what the agent must do next."""
    dev = Device(a.serial)
    pkg = resolve_package(a.ref)
    if not dev.paths(PLAY_STORE_PKG):
        die("no Play Store on this device (needs a google_apis_playstore image).", 1)
    if dev.paths(pkg):
        name, code = dev.version(pkg)
        print(f"already installed: {pkg} {name} (code {code}) — nothing to do")
        return 0
    dev.shell("am", "start", "-a", "android.intent.action.VIEW",
              "-d", f"market://details?id={pkg}")
    time.sleep(a.settle)
    focus = dev.focus()
    print(f"opened market://details?id={pkg}\nfocus={focus}")
    if "unauthenticated" in focus.lower() or dev.accounts() == 0:
        print("NOT SIGNED IN — Play opened its unauthenticated screen. A human must "
              "sign in to a Google account on this device once, by hand. Do not "
              "automate Google sign-in.", file=sys.stderr)
        return 1
    if PLAY_STORE_PKG not in focus:
        print(f"Play Store did not come to the foreground (focus={focus}). The link "
              f"may be malformed or the listing unavailable in this account's country.",
              file=sys.stderr)
        return 1
    print("next: use the android-ui-driver skill to find and tap Install "
          "(the button may read Install / Update / Open / Buy), then run "
          f"`wait-installed {pkg}`.")
    return 0


def cmd_install_apk(a) -> int:
    dev = Device(a.serial)
    for f in a.apks:
        if not os.path.isfile(f):
            die(f"no such file: {f}")
    args = ["install-multiple"] if len(a.apks) > 1 else ["install"]
    args += ["-r", "-g"] + list(a.apks)
    r = dev.run(*args, timeout=600)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if "Success" in out:
        print(out.splitlines()[-1])
        return 0
    print(f"install failed: {out}", file=sys.stderr)
    for code, why in INSTALL_ERRORS.items():
        if code in out:
            print(f"  {code}: {why}", file=sys.stderr)
    return 1


INSTALL_ERRORS = {
    "INSTALL_FAILED_NO_MATCHING_ABIS":
        "the APK's native libraries do not match the device CPU. On Apple Silicon "
        "use an arm64-v8a system image, or get an APK with matching ABIs.",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
        "a copy signed with a different key is installed. Uninstall it first "
        "(this deletes its data).",
    "INSTALL_FAILED_MISSING_SPLIT":
        "only the base APK was supplied. Pass every split to install-multiple.",
    "INSTALL_FAILED_OLDER_SDK":
        "the app's minSdkVersion is above this device's API level.",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE":
        "the device is out of space; wipe the AVD or free storage.",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES":
        "the APK is unsigned.",
}


def cmd_wait_installed(a) -> int:
    dev = Device(a.serial)
    pkg = resolve_package(a.ref)
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        if dev.paths(pkg):
            name, code = dev.version(pkg)
            print(f"installed: {pkg} {name} (code {code}) via {dev.installer(pkg)}")
            return 0
        time.sleep(2)
    print(f"error: {pkg} still not installed after {a.timeout}s. Common causes: the "
          f"Install button was never tapped, Play needs payment or an age check, the "
          f"app is unavailable in this account's country, or the device failed a Play "
          f"Integrity check (banking/DRM apps refuse emulators).", file=sys.stderr)
    return 1


def cmd_launch(a) -> int:
    dev = Device(a.serial)
    pkg = resolve_package(a.ref)
    if not dev.paths(pkg):
        die(f"{pkg} is not installed", 1)
    brief = dev.shell("cmd", "package", "resolve-activity", "--brief",
                      "--user", dev.user(),
                      "-a", "android.intent.action.MAIN",
                      "-c", "android.intent.category.LAUNCHER", pkg)
    comp = next((l.strip() for l in brief.splitlines()
                 if "/" in l and not l.startswith("No ")), "")
    if not comp:
        dev.shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
        print(f"launched {pkg} via monkey (no launcher activity — it may be a "
              f"service, widget, or library package)")
        return 0
    dev.shell("am", "start", "-W", "-n", comp)
    time.sleep(a.settle)
    focus = dev.focus()
    print(f"launched {comp}\nfocus={focus}")
    if pkg not in focus:
        print(f"warning: {pkg} is not in the foreground after launch — it may have "
              f"crashed on start, or shown a splash that redirected. Check "
              f"`adb logcat -b crash -d`.", file=sys.stderr)
        return 1
    return 0


def cmd_uninstall(a) -> int:
    dev = Device(a.serial)
    pkg = resolve_package(a.ref)
    out = (dev.run("uninstall", pkg).stdout or "").strip()
    print(out or "no output")
    return 0 if "Success" in out else 1


def self_test() -> int:
    """Offline checks for link parsing. No device, no adb -- runs in CI."""
    fails = []
    good = {
        "https://play.google.com/store/apps/details?id=com.spotify.music": "com.spotify.music",
        "https://play.google.com/store/apps/details?id=com.x.y&hl=en_US&gl=US": "com.x.y",
        "https://play.google.com/store/apps/details/Signal?id=org.thoughtcrime.securesms":
            "org.thoughtcrime.securesms",
        "market://details?id=com.whatsapp": "com.whatsapp",
        "com.google.android.deskclock": "com.google.android.deskclock",
        "  'com.quoted.pkg'  ": "com.quoted.pkg",
    }
    for ref, want in good.items():
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                got = resolve_package(ref)
        except SystemExit:
            got = "<rejected>"
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'} resolve {ref[:52]!r} -> {got}")
        if not ok:
            fails.append(ref)

    bad = ["https://example.com/app", "https://play.app.goo.gl/?link=abcd",
           "notapackage", "com", "", "foo;touch /tmp/x",
           "https://play.google.com/store/apps/details?id=not a package"]
    for ref in bad:
        try:  # the rejection message goes to stderr by design; keep the run readable
            with contextlib.redirect_stderr(io.StringIO()):
                resolve_package(ref)
            ok = False
        except SystemExit:
            ok = True
        print(f"{'PASS' if ok else 'FAIL'} reject   {ref[:52]!r}")
        if not ok:
            fails.append(ref)

    known = set(INSTALL_ERRORS)
    ok = "INSTALL_FAILED_NO_MATCHING_ABIS" in known and len(known) >= 5
    print(f"{'PASS' if ok else 'FAIL'} install-error table populated ({len(known)} codes)")
    if not ok:
        fails.append("errors")

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(prog="playapp",
                                description="Play Store link -> installed app.")
    p.add_argument("--serial", help="target device (required for physical phones)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def ref_arg(sp):
        sp.add_argument("ref", help="Play URL, market:// URI, or package name")

    ref_arg(sub.add_parser("resolve", help="print the package name from a link"))
    pf = sub.add_parser("preflight",
                        help="check device, Play image, sign-in and animation state")
    pf.add_argument("--fix", action="store_true",
                    help="switch off UI animations (the only blocker that is "
                         "safely scriptable; sign-in and image choice are not)")
    ref_arg(sub.add_parser("status", help="is it installed? version, installer, APKs"))

    o = sub.add_parser("open-listing", help="open the Play listing on the device")
    ref_arg(o)
    o.add_argument("--settle", type=float, default=4)

    i = sub.add_parser("install-apk", help="sideload an APK you already have")
    i.add_argument("apks", nargs="+", help="base.apk [split1.apk ...]")

    w = sub.add_parser("wait-installed", help="poll until the package appears")
    ref_arg(w)
    w.add_argument("--timeout", type=float, default=180)

    l = sub.add_parser("launch", help="resolve the launcher activity and start it")
    ref_arg(l)
    l.add_argument("--settle", type=float, default=3)

    ref_arg(sub.add_parser("uninstall", help="remove the package"))

    a = p.parse_args(argv)
    handlers = {"resolve": cmd_resolve, "preflight": cmd_preflight,
                "status": cmd_status, "open-listing": cmd_open_listing,
                "install-apk": cmd_install_apk, "wait-installed": cmd_wait_installed,
                "launch": cmd_launch, "uninstall": cmd_uninstall}
    try:
        return handlers[a.cmd](a)
    except subprocess.TimeoutExpired:
        die("adb timed out — the device may be busy, asleep, or disconnected.")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
