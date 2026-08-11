#!/usr/bin/env python3
"""Sweep an Android app across a configurable device-configuration matrix.

Two questions, one tool:

  * does the layout hold across screen sizes, densities, orientations, font
    scales, dark mode and display cutouts?
  * does the app degrade gracefully when a capability it declares is taken
    away (permission denied, radio off, location off)?

Every cell is APPLIED, then VERIFIED by reading the configuration back off the
device (`dumpsys window`), so the report states the dp bucket the device
actually entered rather than the arithmetic this script intended. Findings are
split into HARD failures (crash/ANR attributable to the package, process death,
activity not resumed -- these gate) and SUSPECTED signals (blank screen,
off-screen or zero-area controls -- these need a human look), reusing the
evidence vocabulary of `android-ux-audit`.

Fidelity limit, stated up front: `wm size`/`wm density` resize the logical
display of ONE emulator. They do not reproduce a display cutout you did not
enable, a real device's aspect ratio, hinge/fold posture, or a manufacturer
skin. A pass here is weaker evidence than a pass on a purpose-built AVD or real
hardware -- see references/matrix-design.md.

Composition: snapshots come from `android-ui-driver` (`uia.py snap --json`) so
`android-ux-audit` can audit any cell without a second snapshot format.

Exit codes: 0 clean | 1 hard failure in some cell | 2 usage / device error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ADB_TIMEOUT = 120
SETTLE = 2.5          # seconds after a configuration change before observing
CUTOUT_SETTLE = 6.0   # a resource-overlay change restarts SystemUI; give it room
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

# Material 3 / Jetpack WindowSizeClass width buckets, in dp.
SIZE_CLASSES = ((600, "compact"), (840, "medium"), (1200, "expanded"),
                (1600, "large"), (10 ** 9, "extra-large"))

# Packages that legitimately take the foreground on the platform's behalf: a
# permission prompt, a "location is off" warning, a settings screen. When one of
# these is in front, the app under test has not failed -- the system interposed.
SYSTEM_UI_PKGS = ("com.android.systemui", "com.android.settings",
                  "com.android.permissioncontroller",
                  "com.google.android.permissioncontroller",
                  "com.google.android.gms", "android")


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def size_class(width_dp: float) -> str:
    for limit, name in SIZE_CLASSES:
        if width_dp < limit:
            return name
    return "extra-large"


def dp(px: int, density: int) -> int:
    return round(px * 160.0 / density)


# ---------------------------------------------------------------- device


def find_adb() -> str:
    exe = shutil.which("adb")
    if exe:
        return exe
    for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.path.expanduser("~/Library/Android/sdk"),
                 os.path.expanduser("~/Android/Sdk")):
        cand = os.path.join(root or "", "platform-tools", "adb")
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
            die("no device attached.", 2)
        if len(devs) > 1:
            die(f"{len(devs)} devices attached: {', '.join(devs)}. Pass --serial.")
        return devs[0]

    def run(self, *args, timeout=ADB_TIMEOUT):
        return subprocess.run([self.adb, "-s", self.serial] + list(args),
                              capture_output=True, text=True, timeout=timeout)

    def shell(self, *args, timeout=ADB_TIMEOUT) -> str:
        # `adb shell` joins its args and runs them through the DEVICE's sh -- quote
        # each one so a stray ';' in a package name cannot execute on the device.
        quoted = [shlex.quote(str(a)) for a in args]
        return (self.run("shell", *quoted, timeout=timeout).stdout or "").replace("\r\n", "\n")

    # -- configuration reads -------------------------------------------------

    def wm_size(self) -> tuple[int, int]:
        """The size the app lays out against: the override when one is set."""
        out = self.shell("wm", "size")
        m = (re.search(r"Override size:\s*(\d+)x(\d+)", out)
             or re.search(r"Physical size:\s*(\d+)x(\d+)", out))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    def wm_density(self) -> int:
        out = self.shell("wm", "density")
        m = (re.search(r"Override density:\s*(\d+)", out)
             or re.search(r"Physical density:\s*(\d+)", out))
        return int(m.group(1)) if m else 0

    def has_override(self) -> tuple[bool, bool]:
        return ("Override size:" in self.shell("wm", "size"),
                "Override density:" in self.shell("wm", "density"))

    def current_display(self) -> tuple[int, int]:
        """cur=WxH from dumpsys -- the post-rotation logical display."""
        m = re.search(r"cur=(\d+)x(\d+)", self.shell("dumpsys", "window", "displays"))
        return (int(m.group(1)), int(m.group(2))) if m else self.wm_size()

    def global_config(self) -> dict:
        """The framework's own current configuration: sw/w/h in dp, plus dpi.

        Must be read from `mGlobalConfiguration`. A bare search for `swNNNdp`
        across `dumpsys window` hits a stale `mFullConfiguration` from some window
        that has not been reconfigured yet, which reports the pre-resize bucket
        (verified: global said sw320dp while the first match said sw411dp).
        """
        out = self.shell("dumpsys", "window")
        m = re.search(r"mGlobalConfiguration=\{([^}]*)", out)
        if not m:
            return {}
        blob = m.group(1)
        got = {}
        for key, pat in (("smallestWidthDp", r"\bsw(\d+)dp\b"),
                         ("widthDp", r"\bw(\d+)dp\b"),
                         ("heightDp", r"\bh(\d+)dp\b"),
                         ("densityDpi", r"\b(\d+)dpi\b")):
            mm = re.search(pat, blob)
            if mm:
                got[key] = int(mm.group(1))
        return got

    def rotation(self) -> int:
        m = re.search(r"mRotation=ROTATION_(\d+)", self.shell("dumpsys", "window"))
        return {0: 0, 90: 1, 180: 2, 270: 3}.get(int(m.group(1)), 0) if m else 0

    def focused(self) -> str:
        m = re.search(r"mCurrentFocus=Window\{\S+ \S+ (\S+)\}",
                      self.shell("dumpsys", "window"))
        return m.group(1) if m else ""

    def top_activity(self) -> str:
        m = re.search(r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)",
                      self.shell("dumpsys", "activity", "activities"))
        return m.group(1) if m else ""

    def wait_foreground(self, pkg: str, timeout: float = 10.0) -> tuple[bool, str]:
        """Poll until the app owns the top resumed activity.

        A single read is not enough: during a resize or relaunch the focused
        window is briefly empty or belongs to another process, which reads as
        'the app died' when it is simply mid-transition.
        """
        deadline, last = time.time() + timeout, ""
        while time.time() < deadline:
            top = self.top_activity()
            if top.startswith(pkg + "/"):
                return True, top
            focus = self.focused()
            if focus.startswith(pkg + "/"):
                return True, focus
            last = top or focus or last
            time.sleep(0.5)
        return False, last

    def wait_config(self, want_size: str | None, want_density: int | None,
                    timeout: float = 12.0) -> bool:
        """Block until the display actually reconfigured.

        `wm size`/`wm density` return immediately but the display reconfigures
        asynchronously. Reading `dumpsys` straight afterwards can return the
        PREVIOUS cell's geometry, which silently mislabels every result -- a
        whole sweep can come back shifted by one cell. So poll until the target
        is observed (either orientation, since rotation swaps the axes), and
        otherwise until two consecutive reads agree.
        """
        target = None
        if want_size:
            w, h = (int(x) for x in want_size.split("x"))
            target = {(w, h), (h, w)}
        deadline, prev = time.time() + timeout, None
        while time.time() < deadline:
            cur = self.current_display()
            dens_ok = want_density is None or self.wm_density() == want_density
            if target is not None:
                if cur in target and dens_ok:
                    return True
            elif cur == prev and dens_ok:
                return True
            prev = cur
            time.sleep(0.4)
        return False

    def pid_of(self, pkg: str) -> str:
        return self.shell("pidof", pkg).strip()

    def setting(self, ns: str, key: str) -> str:
        return self.shell("settings", "get", ns, key).strip()


# ---------------------------------------------------------------- state safety


def state_path(serial: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", serial)
    return os.path.join(tempfile.gettempdir(), f"devmatrix-state-{safe}.json")


def snapshot_state(dev: Device) -> dict:
    """Everything this tool may mutate, recorded EXACTLY as the device reports it.

    Recording the literal prior string matters: an appops mode whose original
    value is `foreground` is NOT restored by setting it to `default`, and a
    device with no size override must be `reset`, not set back to its pixels.
    """
    size_over, dens_over = dev.has_override()
    night = dev.shell("cmd", "uimode", "night")
    return {
        "serial": dev.serial,
        "sizeOverride": dev.wm_size() if size_over else None,
        "densityOverride": dev.wm_density() if dens_over else None,
        "fontScale": dev.setting("system", "font_scale"),
        "userRotation": dev.setting("system", "user_rotation"),
        "accelerometerRotation": dev.setting("system", "accelerometer_rotation"),
        "night": "yes" if "Night mode: yes" in night else
                 ("auto" if "Night mode: auto" in night else "no"),
        "cutoutOverlay": enabled_cutout(dev),
        "airplane": dev.setting("global", "airplane_mode_on"),
        "capabilities": {},   # filled in as capability cells mutate things
    }


def enabled_cutout(dev: Device) -> str | None:
    for line in dev.shell("cmd", "overlay", "list").splitlines():
        line = line.strip()
        if line.startswith("[x]") and "cutout.emulation" in line:
            return line[3:].strip()
    return None


def restore_state(dev: Device, st: dict, verbose: bool = True) -> list[str]:
    """Put every recorded key back. Returns human-readable restore notes."""
    notes = []

    def say(msg):
        notes.append(msg)
        if verbose:
            print(f"  restored: {msg}")

    if st.get("sizeOverride"):
        w, h = st["sizeOverride"]
        dev.shell("wm", "size", f"{w}x{h}")
        say(f"wm size {w}x{h} (there was a prior override)")
    else:
        dev.shell("wm", "size", "reset")
        say("wm size reset")
    if st.get("densityOverride"):
        dev.shell("wm", "density", str(st["densityOverride"]))
        say(f"wm density {st['densityOverride']} (prior override)")
    else:
        dev.shell("wm", "density", "reset")
        say("wm density reset")

    fs = (st.get("fontScale") or "").strip()
    if fs and fs != "null":
        dev.shell("settings", "put", "system", "font_scale", fs)
    else:
        dev.shell("settings", "delete", "system", "font_scale")
    say(f"font_scale {fs or '<unset>'}")

    for key, val in (("user_rotation", st.get("userRotation")),
                     ("accelerometer_rotation", st.get("accelerometerRotation"))):
        v = (val or "").strip()
        if v and v != "null":
            dev.shell("settings", "put", "system", key, v)
        else:
            dev.shell("settings", "delete", "system", key)
    say(f"rotation (user={st.get('userRotation')}, auto={st.get('accelerometerRotation')})")

    dev.shell("cmd", "uimode", "night", st.get("night") or "no")
    say(f"uimode night {st.get('night') or 'no'}")

    now = enabled_cutout(dev)
    want = st.get("cutoutOverlay")
    if now and now != want:
        dev.shell("cmd", "overlay", "disable", now)
    if want and want != now:
        dev.shell("cmd", "overlay", "enable", want)
    say(f"cutout overlay {want or '<none>'}")

    if (st.get("airplane") or "0").strip() == "0":
        dev.shell("cmd", "connectivity", "airplane-mode", "disable")
    else:
        dev.shell("cmd", "connectivity", "airplane-mode", "enable")
    say(f"airplane_mode {st.get('airplane')}")

    for key, rec in (st.get("capabilities") or {}).items():
        kind = rec.get("kind")
        if kind == "appops":
            # The exact prior mode string, NOT `default` -- `default` is not a
            # restore for an op whose original mode was e.g. `foreground`.
            dev.shell("appops", "set", "--uid", rec["pkg"], rec["op"], rec["prior"])
            dev.shell("appops", "set", rec["pkg"], rec["op"], rec["prior"])
            say(f"appops {rec['op']} -> {rec['prior']}")
        elif kind == "permission" and rec.get("prior") == "granted":
            dev.shell("pm", "grant", rec["pkg"], rec["perm"])
            say(f"permission {rec['perm']} re-granted")
        elif kind == "svc":
            dev.shell("svc", rec["which"], rec["prior"])
            say(f"svc {rec['which']} {rec['prior']}")
        elif kind == "location":
            dev.shell("cmd", "location", "set-location-enabled", rec["prior"])
            say(f"location enabled={rec['prior']}")
    return notes


# ---------------------------------------------------------------- profiles

# Every cell is a dict of optional keys. `size` is WxH in PIXELS and `density`
# is dpi -- width_dp = px * 160 / dpi. Keeping both explicit (rather than asking
# for dp) is deliberate: it is what `wm` accepts, and the verified dp read back
# off the device is what gets reported.
PROFILES = {
    "quick": {
        "description": "Five cells that catch most layout breakage.",
        "cells": [
            {"id": "baseline", "note": "the device as configured"},
            {"id": "compact-sw320", "size": "720x1280", "density": 360,
             "note": "smallest realistic phone width (320dp)"},
            {"id": "landscape", "rotation": 1, "note": "rotation at native size"},
            {"id": "expanded-sw840", "size": "1680x2400", "density": 320,
             "note": "tablet-class width (840dp)"},
            {"id": "largest-font", "font_scale": 2.0,
             "note": "maximum accessibility text scale"},
        ],
    },
    "thorough": {
        "description": "Adds the medium bucket, dark mode, a cutout and the worst case.",
        "cells": [
            {"id": "baseline"},
            {"id": "compact-sw320", "size": "720x1280", "density": 360},
            {"id": "compact-sw360", "size": "1080x2400", "density": 480},
            {"id": "medium-sw600", "size": "1200x1920", "density": 320},
            {"id": "expanded-sw840", "size": "1680x2400", "density": 320},
            {"id": "tablet-landscape", "size": "1920x1200", "density": 240, "rotation": 1},
            {"id": "landscape", "rotation": 1},
            {"id": "dark-mode", "night": "yes"},
            {"id": "cutout-tall", "cutout": "tall"},
            {"id": "largest-font", "font_scale": 2.0},
            {"id": "worst-case", "size": "720x1280", "density": 360, "font_scale": 2.0,
             "note": "narrowest width with the largest text -- where clipping shows"},
        ],
    },
    "a11y": {
        "description": "Text-scaling and display-size sweep for accessibility review.",
        "cells": [
            {"id": "font-1.0", "font_scale": 1.0},
            {"id": "font-1.3", "font_scale": 1.3},
            {"id": "font-1.5", "font_scale": 1.5},
            {"id": "font-2.0", "font_scale": 2.0},
            {"id": "display-size-large", "density": 540,
             "note": "denser dpi = fewer dp = Android's 'display size: large'"},
            {"id": "large-text-small-window", "size": "720x1280", "density": 360,
             "font_scale": 2.0},
        ],
    },
    "capability": {
        "description": "Runtime capability denial -- what a stock emulator can actually take away.",
        "cells": [
            {"id": "baseline"},
            {"id": "airplane-mode", "deny": "airplane"},
            {"id": "wifi-off", "deny": "wifi"},
            {"id": "mobile-data-off", "deny": "data"},
            {"id": "location-off", "deny": "location"},
            {"id": "location-permission-revoked", "deny": "permission:ACCESS_FINE_LOCATION"},
            {"id": "camera-permission-revoked", "deny": "permission:CAMERA"},
            {"id": "location-appops-ignored", "deny": "appops:FINE_LOCATION",
             "note": "app believes it is granted but receives nothing"},
        ],
    },
}

# Capability absences that CANNOT be produced on a running stock emulator: they
# are baked into the AVD's config.ini and only take effect at boot. `plan` and
# `capabilities` print these as recipes instead of pretending to test them.
AVD_RECIPES = {
    "android.hardware.camera": "hw.camera.back=none\nhw.camera.front=none",
    "android.hardware.camera.any": "hw.camera.back=none\nhw.camera.front=none",
    "android.hardware.camera.front": "hw.camera.front=none",
    "android.hardware.camera.autofocus": "hw.camera.back=webcam0  # or none",
    "android.hardware.location.gps": "hw.gps=no",
    "android.hardware.sensor.accelerometer": "hw.accelerometer=no",
    "android.hardware.sensor.proximity": "hw.sensors.proximity=no",
    "android.hardware.sensor.compass": "hw.sensors.magnetic_field=no",
    "android.hardware.sensor.gyroscope": "hw.gyroscope=no",
    "android.hardware.sensor.light": "hw.sensors.light=no",
    "android.hardware.sensor.barometer": "hw.sensors.pressure=no",
    "android.hardware.microphone": "hw.audioInput=no",
    "android.hardware.touchscreen.multitouch": "hw.multiTouch=no",
    "android.hardware.telephony": "hw.gsmModem=no, or a non-telephony image",
    "android.hardware.wifi": "use an image without wifi; not a config.ini switch",
    "android.hardware.bluetooth": "hw.bluetooth=no",
    "android.hardware.nfc": "hw.nfc=no",
    "android.hardware.fingerprint": "hw.fingerprint=no",
    "android.software.leanback": "use a TV system image",
    "com.google.android.feature.PLAY_STORE": "use a google_apis or AOSP image "
                                             "(no Play Services) instead of google_apis_playstore",
}


def load_profile(name: str | None, path: str | None) -> dict:
    if path:
        try:
            prof = json.load(open(path))
        except (OSError, ValueError) as e:
            die(f"could not read the matrix profile {path!r}: {e}")
        if not isinstance(prof, dict) or not isinstance(prof.get("cells"), list):
            die(f"{path!r} is not a matrix profile: it needs a 'cells' list")
        if not prof["cells"]:
            die(f"{path!r} has an empty 'cells' list -- nothing to sweep")
        seen = set()
        for i, c in enumerate(prof["cells"]):
            cid = c.get("id") or f"cell{i}"
            c["id"] = cid
            if cid in seen:
                die(f"{path!r} has a duplicate cell id {cid!r}")
            seen.add(cid)
        return prof
    key = name or "quick"
    if key not in PROFILES:
        die(f"unknown profile {key!r}. Available: {', '.join(sorted(PROFILES))}")
    return PROFILES[key]


# ---------------------------------------------------------------- sibling skill


def find_uia() -> str | None:
    """`android-ui-driver`'s snapshotter -- one snapshot format for the toolkit."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.environ.get("ANDROID_UI_DRIVER"),
        os.path.join(here, "..", "..", "android-ui-driver", "scripts", "uia.py"),
        os.path.expanduser("~/.claude/skills/android-ui-driver/scripts/uia.py"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def snap(uia: str, serial: str, path: str) -> dict | None:
    r = subprocess.run([sys.executable, uia, "--serial", serial, "snap", "--json"],
                       capture_output=True, text=True, timeout=ADB_TIMEOUT)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1)
    return data


# ---------------------------------------------------------------- detectors


def detect(dev: Device, pkg: str, snapshot: dict | None, pid_before: str,
           disp: tuple[int, int], foreground: tuple[bool, str] = (True, ""),
           rotation_want: int | None = None, rotation_got: int | None = None) -> list[dict]:
    """Findings for one cell.

    HARD findings gate the run: the framework itself said the app crashed or hung,
    the process is gone, or the app never returned to the foreground. SUSPECTED
    findings are proxies from the accessibility tree that need a human look --
    same evidence vocabulary as android-ux-audit. Partially-clipped views and
    text truncation are deliberately NOT gated: their false-positive rate on
    animating or edge-anchored UI is too high to fail a build on.
    """
    out, n = [], 0

    def add(kind, severity, evidence_type, detail):
        nonlocal n
        n += 1
        out.append({"id": f"DM-{n:03d}", "kind": kind, "severity": severity,
                    "evidenceType": evidence_type, "detail": detail})

    # Crashes land in the crash buffer; ANRs and process deaths are only in the
    # events buffer; native crashes are in neither and only reach dropbox.
    crash = dev.shell("logcat", "-d", "-b", "crash")
    if pkg in crash and "FATAL EXCEPTION" in crash:
        first = next((l for l in crash.splitlines() if "FATAL EXCEPTION" in l), "").strip()
        add("crash", "hard", "measured",
            f"FATAL EXCEPTION in the crash buffer naming {pkg}: {first[:160]}")
    events = dev.shell("logcat", "-d", "-b", "events")
    for line in events.splitlines():
        if "am_anr" in line and pkg in line:
            add("anr", "hard", "measured", f"am_anr for {pkg}: {line.strip()[:160]}")
            break
    drop = dev.shell("dumpsys", "dropbox", "--print", "data_app_native_crash")
    if pkg in drop:
        add("native_crash", "hard", "measured",
            f"dropbox recorded a native crash for {pkg} "
            f"(a native crash never appears as a FATAL EXCEPTION)")

    pid_now = dev.pid_of(pkg)
    if pid_before and not pid_now:
        add("process_death", "hard", "measured",
            f"process {pid_before} is gone and did not come back")
    elif pid_before and pid_now and pid_now != pid_before:
        add("process_restart", "suspected", "measured",
            f"pid changed {pid_before} -> {pid_now}; the process was recreated "
            f"(normal for some config changes, a bug if state was lost)")

    reached, saw = foreground
    if not reached:
        owner = saw.split("/", 1)[0] if saw else ""
        if owner and any(owner == s or owner.startswith(s + ".") for s in SYSTEM_UI_PKGS):
            # The platform interposed -- a permission prompt, a "location is off"
            # warning, a settings screen. That is the system asking, not the app
            # breaking, and in a capability-denial cell it is often the correct
            # behaviour. Never gate on it.
            add("system_dialog", "suspected", "measured",
                f"the system put {saw!r} in front of {pkg}; the app did not crash. "
                f"In a capability cell this is usually the platform prompting -- "
                f"confirm on the screenshot that the app handles the return path")
        else:
            add("foreground_lost", "hard", "measured",
                f"{pkg} never returned to the foreground in this configuration"
                + (f"; the top activity was {saw!r}" if saw else ""))

    # A rotation the app refuses is information, not a silent pass: a
    # portrait-locked activity resets user_rotation the moment it starts. Judge it
    # by the geometry actually laid out, not by `mRotation` -- that field appears
    # several times in dumpsys and the first match can belong to another display.
    if rotation_want is not None and rotation_got is not None:
        want_landscape = rotation_want in (1, 3)
        if want_landscape != bool(rotation_got):
            add("orientation_locked", "suspected", "measured",
                f"rotation {rotation_want} was requested but the window is still "
                f"{'portrait' if want_landscape else 'landscape'} -- the activity "
                f"is orientation-locked, so this cell did NOT test "
                f"{'landscape' if want_landscape else 'portrait'}")

    if snapshot is None:
        add("no_snapshot", "suspected", "measured",
            "the accessibility snapshot could not be taken in this configuration")
        return out

    els = snapshot.get("elements", [])
    actionable = [e for e in els if e.get("role") in {"btn", "edit", "chk"}
                  and e.get("enabled", True)]
    # An icon-only screen is legitimate, so content-description counts as content;
    # only a screen with essentially nothing on it is worth flagging.
    labelled = [e for e in els if (e.get("text") or e.get("desc") or "").strip()]
    if len(els) <= 1 or not labelled:
        add("blank_screen", "suspected", "observed",
            f"the screen has {len(els)} nodes, {len(labelled)} carrying text or a "
            f"description -- it may have failed to lay out")
    if not actionable:
        add("no_actionable_control", "suspected", "observed",
            "no enabled button, field or checkbox is exposed on this screen")

    dw, dh = disp
    for e in actionable:
        b = e.get("bounds")
        if not b or len(b) != 4:
            continue
        x0, y0, x1, y1 = b
        label = (e.get("text") or e.get("desc") or e.get("id") or "?")[:40]
        if x1 <= x0 or y1 <= y0:
            add("zero_area_control", "suspected", "observed",
                f"actionable {label!r} has empty bounds {b} -- laid out but not visible")
        elif dw and dh and (x0 < 0 or y0 < 0 or x1 > dw or y1 > dh):
            add("offscreen_control", "suspected", "observed",
                f"actionable {label!r} at {b} extends outside the {dw}x{dh} display")
    return out


# ---------------------------------------------------------------- cell apply


def apply_cell(dev: Device, cell: dict, state: dict, pkg: str) -> list[str]:
    """Mutate the device for one cell. Returns notes about what was skipped."""
    skipped = []
    if cell.get("size"):
        if not re.fullmatch(r"\d+x\d+", str(cell["size"])):
            die(f"cell {cell['id']!r}: size must look like 1080x2400")
        dev.shell("wm", "size", str(cell["size"]))
    if cell.get("density"):
        dev.shell("wm", "density", str(int(cell["density"])))
    # NOTE: rotation is deliberately NOT applied here -- see apply_rotation().
    if cell.get("font_scale") is not None:
        dev.shell("settings", "put", "system", "font_scale", str(float(cell["font_scale"])))
    if cell.get("night"):
        dev.shell("cmd", "uimode", "night", str(cell["night"]))
    if cell.get("cutout"):
        ov = f"com.android.internal.display.cutout.emulation.{cell['cutout']}"
        if ov not in dev.shell("cmd", "overlay", "list"):
            skipped.append(f"cutout {cell['cutout']!r} is not available on this image")
        else:
            dev.shell("cmd", "overlay", "enable", ov)
            # A resource-overlay change restarts SystemUI and can tear down the
            # foreground task a moment later. Launching before that settles makes
            # the app look like it fell back to the launcher; wait it out first.
            time.sleep(CUTOUT_SETTLE)

    deny = cell.get("deny")
    if deny:
        skipped += apply_denial(dev, deny, state, pkg)
    return skipped


def apply_rotation(dev: Device, rotation: int) -> None:
    """Rotate AFTER the activity is up.

    Applying rotation before launch does not survive: an orientation-locked
    activity rewrites `user_rotation` back to its own orientation as it starts,
    so a pre-launch rotation silently reads back as portrait. Verified on API 35.
    """
    dev.shell("settings", "put", "system", "accelerometer_rotation", "0")
    dev.shell("settings", "put", "system", "user_rotation", str(int(rotation)))


def apply_denial(dev: Device, deny: str, state: dict, pkg: str) -> list[str]:
    """Take a capability away, recording the exact prior value for restore."""
    caps = state.setdefault("capabilities", {})
    if deny == "airplane":
        dev.shell("cmd", "connectivity", "airplane-mode", "enable")
        return []
    if deny in ("wifi", "data"):
        prior = "enable"   # both default on for a booted emulator; recorded anyway
        caps[deny] = {"kind": "svc", "which": deny, "prior": prior}
        dev.shell("svc", deny, "disable")
        return []
    if deny == "location":
        prior = "true" if "true" in dev.shell("cmd", "location", "is-location-enabled") else "false"
        caps["location"] = {"kind": "location", "prior": prior}
        dev.shell("cmd", "location", "set-location-enabled", "false")
        return []
    if deny.startswith("permission:"):
        perm = deny.split(":", 1)[1]
        full = perm if perm.startswith("android.permission.") else f"android.permission.{perm}"
        granted = _perm_granted(dev, pkg, full)
        if granted is None:
            return [f"{pkg} does not request {full} -- nothing to revoke"]
        caps[full] = {"kind": "permission", "pkg": pkg, "perm": full,
                      "prior": "granted" if granted else "denied"}
        dev.shell("pm", "revoke", pkg, full)
        return []
    if deny.startswith("appops:"):
        op = deny.split(":", 1)[1]
        prior = _appops_mode(dev, pkg, op)
        if prior is None:
            return [f"appops op {op!r} is not tracked for {pkg}"]
        caps[f"appops:{op}"] = {"kind": "appops", "pkg": pkg, "op": op, "prior": prior}
        dev.shell("appops", "set", pkg, op, "ignore")
        return []
    return [f"unknown denial {deny!r}"]


def _perm_granted(dev: Device, pkg: str, perm: str) -> bool | None:
    out = dev.shell("dumpsys", "package", pkg)
    m = re.search(re.escape(perm) + r": granted=(true|false)", out)
    if m:
        return m.group(1) == "true"
    return True if perm in out else None


def _appops_mode(dev: Device, pkg: str, op: str) -> str | None:
    out = dev.shell("cmd", "appops", "get", pkg, op)
    m = re.search(r"Uid mode:\s*" + re.escape(op) + r":\s*(\w+)", out)
    if m:
        return m.group(1)
    m = re.search(re.escape(op) + r":\s*(\w+)", out)
    return m.group(1) if m else None


# ---------------------------------------------------------------- commands


def cmd_profiles(dev, a) -> int:
    if a.dump:
        prof = load_profile(a.dump, None)
        print(json.dumps({"name": a.dump, **prof}, indent=1))
        return 0
    for name, p in sorted(PROFILES.items()):
        print(f"{name:12s} {len(p['cells']):2d} cells  {p['description']}")
    print("\nCustomise: `profiles --dump thorough > my.json`, edit, then "
          "`run PKG --matrix my.json`.")
    return 0


def cmd_plan(dev, a) -> int:
    prof = load_profile(a.profile, a.matrix)
    print(f"profile: {a.matrix or a.profile or 'quick'} -- {len(prof['cells'])} cells "
          f"(nothing is applied by `plan`)")
    base_d = dev.wm_density()
    for c in prof["cells"]:
        bits = []
        if c.get("size") and c.get("density"):
            w, h = (int(x) for x in c["size"].split("x"))
            wd, hd = dp(w, c["density"]), dp(h, c["density"])
            bits.append(f"{c['size']}@{c['density']}dpi -> {wd}x{hd}dp [{size_class(wd)}]")
        elif c.get("size"):
            w, h = (int(x) for x in c["size"].split("x"))
            bits.append(f"{c['size']}@{base_d}dpi -> {dp(w, base_d)}x{dp(h, base_d)}dp")
        elif c.get("density"):
            bits.append(f"density {c['density']}dpi")
        for k in ("rotation", "font_scale", "night", "cutout", "deny"):
            if c.get(k) is not None:
                bits.append(f"{k}={c[k]}")
        print(f"  {c['id']:28s} {' · '.join(bits) or 'as configured'}")
        if c.get("note"):
            print(f"  {'':28s} {c['note']}")
    print("\nFidelity: `wm size` resizes ONE emulator's logical display. It does not "
          "reproduce a cutout you did not enable, a real aspect ratio, a hinge, or a "
          "vendor skin -- see references/matrix-design.md.")
    return 0


def cmd_capabilities(dev, a) -> int:
    """What this app could lose, and where each loss can actually be staged."""
    pkg = a.pkg
    device_features = {l.split(":", 1)[1].strip()
                       for l in dev.shell("pm", "list", "features").splitlines()
                       if l.startswith("feature:") and ":" in l}
    declared = _manifest_features(dev, pkg, a.apk)
    known = declared.get("featuresKnown", True)
    feat_str = (f"{len(declared['features'])} uses-feature" if known
                else "uses-feature UNKNOWN (no aapt2 -- dumpsys does not report them)")
    print(f"{pkg}: {feat_str}, {len(declared['permissions'])} uses-permission "
          f"[{declared.get('source', 'device')}]")
    runtime, avd, absent = [], [], []
    for feat, required in sorted(declared["features"].items()):
        if feat not in device_features:
            absent.append(feat)
        elif feat in AVD_RECIPES:
            avd.append((feat, required))
    for perm in sorted(declared["permissions"]):
        short = perm.rsplit(".", 1)[-1]
        if short in ("CAMERA", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
                     "RECORD_AUDIO", "READ_CONTACTS", "POST_NOTIFICATIONS"):
            runtime.append(perm)

    print("\nTestable now (runtime, no reboot):")
    for p in runtime:
        print(f"  permission:{p.rsplit('.', 1)[-1]:24s} pm revoke / appops ignore")
    print("  airplane / wifi / data / location            connectivity + location cells")
    if not runtime:
        print("  (no runtime-permission surface found in the manifest)")

    print("\nNeeds a purpose-built AVD (config.ini, applied at boot):")
    if not known:
        print("  cannot tell -- the manifest's uses-feature list was not readable. "
              "Install SDK build-tools (aapt2) or pass --apk.")
    elif avd:
        for feat, required in avd:
            print(f"  {feat}  (required={str(required).lower()})")
            print(f"      {AVD_RECIPES[feat].replace(chr(10), chr(10) + '      ')}")
    else:
        print("  (none of the declared features have a config.ini switch here)")
    if absent:
        print("\nAlready absent on this device (the app runs without them today):")
        for f in absent:
            print(f"  {f}")
    print("\nA feature declared required=false is a PROMISE the app still works "
          "without it. That promise is what the capability cells test.")
    return 0


def find_aapt() -> str | None:
    exe = shutil.which("aapt2") or shutil.which("aapt")
    if exe:
        return exe
    for root in (os.environ.get("ANDROID_HOME"),
                 os.path.expanduser("~/Library/Android/sdk"),
                 os.path.expanduser("~/Android/Sdk")):
        bt = os.path.join(root or "", "build-tools")
        if os.path.isdir(bt):
            for ver in sorted(os.listdir(bt), reverse=True):
                cand = os.path.join(bt, ver, "aapt2")
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return None


def _badging(aapt: str, apk: str) -> dict:
    out = subprocess.run([aapt, "dump", "badging", apk],
                         capture_output=True, text=True).stdout
    feats = {}
    for m in re.finditer(r"uses-feature(-not-required)?:\s*name='([^']+)'", out):
        feats[m.group(2)] = m.group(1) is None
    perms = {m.group(1) for m in re.finditer(r"uses-permission:\s*name='([^']+)'", out)}
    return {"features": feats, "permissions": perms}


def _manifest_features(dev: Device, pkg: str, apk: str | None) -> dict:
    """uses-feature (required or not) and uses-permission for the app.

    `dumpsys package` reports permissions reliably but does NOT report
    uses-feature at all (verified on API 35: no package exposes a features
    section). Reading 'no features' out of dumpsys would silently claim an app
    declares no hardware requirements, so features come from the manifest via
    aapt2 -- pulling the installed APK when no --apk is given. If aapt2 is
    missing, features are reported as UNKNOWN rather than as none.
    """
    aapt = find_aapt()
    if apk:
        if not aapt:
            die("aapt2 not found -- needed to read a manifest from an APK. "
                "Install SDK build-tools or set ANDROID_HOME.")
        return {**_badging(aapt, apk), "featuresKnown": True, "source": apk}

    out = dev.shell("dumpsys", "package", pkg)
    if "Unable to find package" in out or not out.strip():
        die(f"{pkg} is not installed (and no --apk given)", 2)
    perms = set(re.findall(r"(android\.permission\.[A-Z_]+):\s*granted=", out))
    perms |= set(re.findall(r"^\s*(android\.permission\.[A-Z_]+)$", out, re.M))

    if not aapt:
        return {"features": {}, "featuresKnown": False, "permissions": perms,
                "source": "dumpsys package (permissions only)"}
    remote = ""
    for line in dev.shell("pm", "path", pkg).splitlines():
        if line.startswith("package:"):
            remote = line.split(":", 1)[1].strip()
            break
    if not remote:
        return {"features": {}, "featuresKnown": False, "permissions": perms,
                "source": "dumpsys package (APK path not resolvable)"}
    tmp = os.path.join(tempfile.gettempdir(), f"devmatrix-{pkg}.apk")
    r = dev.run("pull", remote, tmp, timeout=600)
    if r.returncode != 0 or not os.path.isfile(tmp):
        return {"features": {}, "featuresKnown": False, "permissions": perms,
                "source": "dumpsys package (could not pull the APK)"}
    try:
        b = _badging(aapt, tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"features": b["features"], "featuresKnown": True,
            "permissions": perms | b["permissions"], "source": f"pulled {remote}"}


def cmd_run(dev, a) -> int:
    pkg = a.pkg
    if not PKG_RE.match(pkg):
        die(f"{pkg!r} does not look like a package name")
    if not dev.shell("pm", "path", pkg).strip():
        die(f"{pkg} is not installed on {dev.serial}", 2)
    uia = find_uia()
    if not uia:
        die("android-ui-driver's uia.py not found -- this skill reuses its snapshot "
            "format so android-ux-audit can read any cell. Set ANDROID_UI_DRIVER=/path/uia.py.", 2)

    prof = load_profile(a.profile, a.matrix)
    out_dir = a.out or f"./devmatrix-{pkg}"
    cells_dir = os.path.join(out_dir, "cells")
    os.makedirs(cells_dir, exist_ok=True)

    sf = state_path(dev.serial)
    if os.path.exists(sf) and not a.force:
        die(f"a previous run left state at {sf} -- run `restore` first "
            f"(or pass --force to overwrite and lose the original values).", 2)
    state = snapshot_state(dev)
    with open(sf, "w") as fh:
        json.dump(state, fh, indent=1)

    interrupted = {"flag": False}

    def on_signal(signum, frame):
        interrupted["flag"] = True
        raise KeyboardInterrupt

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, on_signal)
        except (ValueError, OSError):
            pass

    report = {"pkg": pkg, "serial": dev.serial,
              "profile": a.matrix or a.profile or "quick",
              "baseline": {"size": "x".join(map(str, dev.wm_size())),
                           "density": dev.wm_density()},
              "cells": [], "verdict": "pass", "restored": False,
              "fidelity": "wm size/density resize one emulator's logical display; "
                          "they do not reproduce display cutouts you did not enable, "
                          "real device aspect ratios, hinges, or vendor skins."}
    hard = 0
    try:
        for cell in prof["cells"]:
            print(f"[cell] {cell['id']}")
            # Clean per-cell anchor. BOTH buffers: ANRs are read from `events`
            # (am_anr), so clearing only `crash` leaves one real ANR to re-fire in
            # every later cell and fail the whole matrix off a single event.
            dev.shell("logcat", "-b", "crash", "-b", "events", "-c")
            skipped = apply_cell(dev, cell, state, pkg)
            # `wm size`/`wm density` return before the display reconfigures. Reading
            # straight afterwards can return the PREVIOUS cell's geometry, which
            # mislabels the result -- an 11-cell sweep came back shifted by a cell.
            applied = dev.wait_config(cell.get("size"), cell.get("density"))
            with open(sf, "w") as fh:                      # persist capability priors
                json.dump(state, fh, indent=1)
            for s in skipped:
                print(f"  skipped: {s}")

            dev.shell("am", "force-stop", pkg)
            dev.shell("monkey", "-p", pkg, "-c",
                      "android.intent.category.LAUNCHER", "1")
            foreground = dev.wait_foreground(pkg)
            pid = dev.pid_of(pkg)

            # Rotation only sticks on a running activity, so it goes here and the
            # app gets a second settle to finish the configuration change.
            want_rot = cell.get("rotation")
            if want_rot is not None:
                apply_rotation(dev, int(want_rot))
                time.sleep(a.settle)
                foreground = dev.wait_foreground(pkg)
            time.sleep(a.settle)

            disp = dev.current_display()
            dens = dev.wm_density()
            # Prefer the framework's own dp numbers; fall back to the arithmetic.
            gc = dev.global_config()
            wd = gc.get("widthDp", dp(disp[0], dens))
            hd = gc.get("heightDp", dp(disp[1], dens))
            verified = {"display": f"{disp[0]}x{disp[1]}", "density": dens,
                        "widthDp": wd, "heightDp": hd,
                        "smallestWidthDp": gc.get("smallestWidthDp", min(wd, hd)),
                        "sizeClass": size_class(wd), "landscape": wd > hd,
                        "rotation": dev.rotation()}
            print(f"  verified: {disp[0]}x{disp[1]} @{dens}dpi = {wd}x{hd}dp "
                  f"[{verified['sizeClass']}] sw{verified['smallestWidthDp']}dp")

            snap_path = os.path.join(cells_dir, f"{cell['id']}.json")
            data = snap(uia, dev.serial, snap_path)
            shot = os.path.join(cells_dir, f"{cell['id']}.png")
            with open(shot, "wb") as fh:
                r = subprocess.run([dev.adb, "-s", dev.serial, "exec-out", "screencap",
                                    "-p"], capture_output=True, timeout=ADB_TIMEOUT)
                fh.write(r.stdout or b"")

            findings = detect(dev, pkg, data, pid, disp, foreground=foreground,
                              rotation_want=(int(want_rot) if want_rot is not None else None),
                              rotation_got=(verified["landscape"] if want_rot is not None else None))
            if not applied:
                # Never let a cell that did not take effect be reported as a pass
                # under a bucket it never entered.
                findings.insert(0, {
                    "id": "DM-000", "kind": "config_not_applied", "severity": "suspected",
                    "evidenceType": "measured",
                    "detail": f"requested {cell.get('size') or 'native'}"
                              f"@{cell.get('density') or 'native'} but the display "
                              f"settled at {verified['display']}@{verified['density']} -- "
                              f"this cell did NOT test the intended configuration"})
                print(f"  [SUSPECTED] config_not_applied: settled at "
                      f"{verified['display']}@{verified['density']}")
            status = ("fail" if any(f["severity"] == "hard" for f in findings)
                      else "suspect" if findings else "pass")
            hard += sum(1 for f in findings if f["severity"] == "hard")
            for f in findings:
                print(f"  [{f['severity'].upper()}] {f['kind']}: {f['detail']}")
            if status == "pass":
                print("  ok")

            report["cells"].append({
                "id": cell["id"], "requested": {k: v for k, v in cell.items()
                                                if k not in ("id", "note")},
                "note": cell.get("note"), "verified": verified, "skipped": skipped,
                "status": status, "findings": findings,
                "artifacts": {"snapshot": snap_path if data else None,
                              "screenshot": shot}})

            # Undo this cell before the next one so cells stay independent.
            restore_state(dev, state, verbose=False)
            state["capabilities"] = {}
    except KeyboardInterrupt:
        print("\ninterrupted -- restoring the device", file=sys.stderr)
    finally:
        notes = restore_state(dev, state, verbose=True)
        report["restored"] = True
        report["restoreNotes"] = notes
        if os.path.exists(sf):
            os.remove(sf)

    report["verdict"] = "fail" if hard else "pass"
    rpath = os.path.join(out_dir, "report.json")
    with open(rpath, "w") as fh:
        json.dump(report, fh, indent=1)

    print(f"\n{len(report['cells'])} cells -> {rpath}")
    for c in report["cells"]:
        v = c["verified"]
        print(f"  {c['status']:8s} {c['id']:28s} {v['widthDp']}x{v['heightDp']}dp "
              f"[{v['sizeClass']}]"
              + (f"  {len(c['findings'])} finding(s)" if c["findings"] else ""))
    print(f"verdict: {report['verdict'].upper()}  (hard failures: {hard})")
    print("Suspected findings are proxies from the accessibility tree -- confirm them "
          "on the screenshots. Run android-ux-audit against any cell snapshot for the "
          "touch-target and label checks.")
    if a.json:
        print(json.dumps(report, indent=1))
    return 1 if hard else 0


def cmd_restore(dev, a) -> int:
    sf = state_path(dev.serial)
    if not os.path.exists(sf):
        print(f"no saved state for {dev.serial} -- nothing to restore. "
              f"(`doctor` reports the current configuration.)")
        return 0
    try:
        st = json.load(open(sf))
    except ValueError:
        die(f"{sf} is corrupt; delete it and reset manually with "
            f"`adb shell wm size reset && adb shell wm density reset`")
    restore_state(dev, st, verbose=True)
    os.remove(sf)
    print("device restored.")
    return 0


def cmd_doctor(dev, a) -> int:
    """Is the device in a clean state, and does a saved run still match it?"""
    cur = snapshot_state(dev)
    dirty = []
    if cur["sizeOverride"]:
        dirty.append(f"wm size override {cur['sizeOverride']} is set")
    if cur["densityOverride"]:
        dirty.append(f"wm density override {cur['densityOverride']} is set")
    if (cur["fontScale"] or "1.0") not in ("1.0", "1", "null"):
        dirty.append(f"font_scale is {cur['fontScale']}")
    if cur["night"] == "yes":
        dirty.append("dark mode is forced on")
    if cur["cutoutOverlay"]:
        dirty.append(f"cutout overlay {cur['cutoutOverlay']} is enabled")
    if (cur["accelerometerRotation"] or "1") == "0":
        dirty.append(f"auto-rotate is off (user_rotation={cur['userRotation']})")
    if (cur["airplane"] or "0") != "0":
        dirty.append("airplane mode is on")

    print(f"device {dev.serial}: {cur['sizeOverride'] or 'native'} size, "
          f"density {cur['densityOverride'] or 'native'}, font_scale "
          f"{cur['fontScale']}, night {cur['night']}")
    sf = state_path(dev.serial)
    if os.path.exists(sf):
        print(f"a run is in flight or crashed: {sf} -- `restore` puts it back")
    if dirty:
        print("non-default configuration:")
        for d in dirty:
            print(f"  - {d}")
        print("`restore` (with saved state) or `adb shell wm size reset` clears these.")
        return 1
    print("configuration looks clean.")
    return 0


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    check("dp arithmetic 720px @360dpi is 320dp", dp(720, 360) == 320)
    check("dp arithmetic 1080px @420dpi is 411dp", dp(1080, 420) == 411)
    check("dp arithmetic is 1:1 at 160dpi", dp(600, 160) == 600)

    check("compact below 600dp", size_class(411) == "compact")
    check("medium at exactly 600dp", size_class(600) == "medium")
    check("expanded at exactly 840dp", size_class(840) == "expanded")
    check("large at 1200dp", size_class(1200) == "large")
    check("extra-large at 1600dp", size_class(1600) == "extra-large")

    for name, prof in PROFILES.items():
        ids = [c["id"] for c in prof["cells"]]
        check(f"profile {name} has unique cell ids", len(ids) == len(set(ids)))
        check(f"profile {name} has a description", bool(prof.get("description")))
        for c in prof["cells"]:
            if c.get("size"):
                assert re.fullmatch(r"\d+x\d+", c["size"]), c
    check("every profile cell size parses", True)

    # the quick profile must actually span more than one size class
    classes = set()
    for c in PROFILES["thorough"]["cells"]:
        if c.get("size") and c.get("density"):
            w = int(c["size"].split("x")[0])
            classes.add(size_class(dp(w, c["density"])))
    check("thorough profile spans several size classes", len(classes) >= 3)

    # detector logic, with a fake device. Each buffer is addressed separately so a
    # test can put an ANR in `events` without it also appearing in `crash`.
    class FakeDev:
        def __init__(self, crash="", events="", dropbox="", pid="123"):
            self._crash, self._events = crash, events
            self._dropbox, self._pid = dropbox, pid

        def shell(self, *a, **k):
            if "logcat" in a:
                return self._events if "events" in a else self._crash
            if "dropbox" in a:
                return self._dropbox
            return ""

        def pid_of(self, pkg):
            return self._pid

        def focused(self):
            return ""

    good_snap = {"elements": [
        {"role": "btn", "text": "Go", "bounds": [0, 0, 100, 100], "enabled": True},
        {"role": "txt", "text": "Hello", "bounds": [0, 200, 300, 240]}]}

    f = detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400))
    check("a healthy cell yields no findings", f == [])

    f = detect(FakeDev(crash="FATAL EXCEPTION: main\nProcess: org.x"), "org.x",
               good_snap, "123", (1080, 2400))
    check("crash in the buffer is a HARD finding",
          any(x["kind"] == "crash" and x["severity"] == "hard" for x in f))

    f = detect(FakeDev(events="am_anr  [0,123,org.x,...]"), "org.x",
               good_snap, "123", (1080, 2400))
    check("an ANR is caught from the events buffer, not the crash buffer",
          any(x["kind"] == "anr" and x["severity"] == "hard" for x in f))

    f = detect(FakeDev(dropbox="data_app_native_crash org.x ..."), "org.x",
               good_snap, "123", (1080, 2400))
    check("a native crash is caught from dropbox",
          any(x["kind"] == "native_crash" and x["severity"] == "hard" for x in f))

    f = detect(FakeDev(pid=""), "org.x", good_snap, "123", (1080, 2400))
    check("process death is a HARD finding",
          any(x["kind"] == "process_death" and x["severity"] == "hard" for x in f))

    f = detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
               foreground=(False, "com.other/.Main"))
    check("never returning to the foreground is a HARD finding",
          any(x["kind"] == "foreground_lost" and x["severity"] == "hard" for x in f))

    f = detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
               foreground=(False, "com.google.android.gms/.location.settings."
                                  "LocationOffWarningActivity"))
    check("a system prompt in front is SUSPECTED, not an app failure",
          any(x["kind"] == "system_dialog" for x in f)
          and all(x["severity"] != "hard" for x in f))
    check("a permission dialog in front does not fail a capability cell",
          all(x["severity"] != "hard" for x in
              detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
                     foreground=(False, "com.android.permissioncontroller/.Grant"))))
    check("a transient non-app window alone does not fail a cell",
          detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
                 foreground=(True, "org.x/.Main")) == [])

    f = detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
               rotation_want=1, rotation_got=False)
    check("a refused rotation is reported, not silently passed",
          any(x["kind"] == "orientation_locked" for x in f))
    check("a refused rotation is suspected, not a hard failure",
          all(x["severity"] != "hard" for x in f))
    check("an honoured landscape rotation produces no finding",
          detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
                 rotation_want=1, rotation_got=True) == [])
    check("rotation 3 also counts as landscape",
          detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
                 rotation_want=3, rotation_got=True) == [])
    check("a requested portrait that stays landscape is flagged",
          any(x["kind"] == "orientation_locked" for x in
              detect(FakeDev(), "org.x", good_snap, "123", (1080, 2400),
                     rotation_want=0, rotation_got=True)))

    f = detect(FakeDev(), "org.x", {"elements": []}, "123", (1080, 2400))
    check("an empty screen is SUSPECTED, never hard",
          any(x["kind"] == "blank_screen" for x in f)
          and all(x["severity"] != "hard" for x in f))

    off = {"elements": [{"role": "btn", "text": "Save",
                         "bounds": [900, 0, 1300, 100], "enabled": True}]}
    f = detect(FakeDev(), "org.x", off, "123", (1080, 2400))
    check("a control past the display edge is flagged",
          any(x["kind"] == "offscreen_control" for x in f))
    check("off-screen findings are suspected, not hard",
          all(x["severity"] != "hard" for x in f))

    zero = {"elements": [{"role": "btn", "text": "X",
                          "bounds": [10, 10, 10, 10], "enabled": True}]}
    check("a zero-area control is flagged",
          any(x["kind"] == "zero_area_control"
              for x in detect(FakeDev(), "org.x", zero, "123", (1080, 2400))))

    f = detect(FakeDev(), "org.x", None, "123", (1080, 2400))
    check("a missing snapshot is reported, not silently passed",
          any(x["kind"] == "no_snapshot" for x in f))

    check("crash detection does not fire for another package",
          not any(x["kind"] == "crash" for x in
                  detect(FakeDev(crash="FATAL EXCEPTION: main\nProcess: com.other"),
                         "org.x", good_snap, "123", (1080, 2400))))

    # restore must use the recorded string, never `default`
    calls = []

    class RecDev:
        serial = "t"

        def shell(self, *a, **k):
            calls.append(" ".join(str(x) for x in a))
            return ""

    st = {"sizeOverride": None, "densityOverride": None, "fontScale": "1.0",
          "userRotation": "0", "accelerometerRotation": "1", "night": "no",
          "cutoutOverlay": None, "airplane": "0",
          "capabilities": {"appops:FINE_LOCATION": {
              "kind": "appops", "pkg": "org.x", "op": "FINE_LOCATION",
              "prior": "foreground"}}}
    restore_state(RecDev(), st, verbose=False)
    joined = "\n".join(calls)
    check("restore resets wm size when there was no override",
          "wm size reset" in joined and "wm density reset" in joined)
    check("restore puts an appops op back to its exact prior mode",
          "appops set org.x FINE_LOCATION foreground" in joined)
    check("restore never uses `default` as an appops restore",
          "FINE_LOCATION default" not in joined)
    check("restore sets both the uid and package appops mode",
          "appops set --uid org.x FINE_LOCATION foreground" in joined)

    calls.clear()
    restore_state(RecDev(), {**st, "sizeOverride": [720, 1280],
                             "densityOverride": 360, "capabilities": {}}, verbose=False)
    joined = "\n".join(calls)
    check("restore re-applies a pre-existing size override instead of resetting",
          "wm size 720x1280" in joined and "wm size reset" not in joined)

    check("profile loader rejects a profile with no cells",
          _expect_exit(lambda: load_profile(None, _tmpjson({"cells": []}))))
    check("profile loader rejects duplicate cell ids",
          _expect_exit(lambda: load_profile(
              None, _tmpjson({"cells": [{"id": "a"}, {"id": "a"}]}))))
    check("profile loader accepts a valid custom profile",
          load_profile(None, _tmpjson(
              {"cells": [{"id": "a", "size": "720x1280", "density": 360}]}))["cells"][0]["id"] == "a")
    check("unknown profile name is rejected",
          _expect_exit(lambda: load_profile("nope", None)))

    check("every AVD recipe names a config.ini key or an image",
          all(("=" in v or "image" in v.lower()) for v in AVD_RECIPES.values()))

    # wait_config must not accept the previous cell's geometry as "applied"
    class SeqDev:
        """Reports a stale size first, then the requested one."""

        def __init__(self, seq, density=360):
            self.seq, self.i, self.density = seq, 0, density

        def current_display(self):
            v = self.seq[min(self.i, len(self.seq) - 1)]
            self.i += 1
            return v

        def wm_density(self):
            return self.density

    d = SeqDev([(1200, 1920), (1200, 1920), (720, 1280)])
    check("wait_config waits through a stale read for the requested size",
          Device.wait_config(d, "720x1280", 360, timeout=5.0) is True)
    check("wait_config accepts the rotated axes of the requested size",
          Device.wait_config(SeqDev([(1280, 720)]), "720x1280", 360, timeout=5.0) is True)
    check("wait_config gives up rather than reporting a wrong config as applied",
          Device.wait_config(SeqDev([(1200, 1920)]), "720x1280", 360, timeout=1.5) is False)
    check("wait_config rejects a matching size at the wrong density",
          Device.wait_config(SeqDev([(720, 1280)], density=420), "720x1280", 360,
                             timeout=1.5) is False)

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def _tmpjson(obj) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(obj, fh)
    fh.close()
    return fh.name


def _expect_exit(fn) -> bool:
    try:
        fn()
    except SystemExit:
        return True
    return False


# ---------------------------------------------------------------- cli


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--self-test" in argv:
        return self_test()
    p = argparse.ArgumentParser(
        prog="devmatrix",
        description="Sweep an app across a device-configuration matrix and check "
                    "it degrades gracefully when a capability is missing.")
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("profiles", help="list the built-in matrix profiles")
    pr.add_argument("--dump", help="print one profile as JSON to customise")

    pl = sub.add_parser("plan", help="show the cells and their dp buckets, change nothing")
    pl.add_argument("--profile", help=f"one of: {', '.join(sorted(PROFILES))}")
    pl.add_argument("--matrix", help="a custom profile JSON file")

    rn = sub.add_parser("run", help="apply every cell, capture evidence, restore")
    rn.add_argument("pkg")
    rn.add_argument("--profile")
    rn.add_argument("--matrix")
    rn.add_argument("--out", help="output directory (default ./devmatrix-<pkg>)")
    rn.add_argument("--settle", type=float, default=SETTLE,
                    help="seconds to wait after each configuration change")
    rn.add_argument("--json", action="store_true", help="also print the report JSON")
    rn.add_argument("--force", action="store_true",
                    help="overwrite a leftover state file (loses the original values)")

    cp = sub.add_parser("capabilities",
                        help="what this app declares, and where each loss can be staged")
    cp.add_argument("pkg")
    cp.add_argument("--apk", help="read the manifest from an APK instead of the device")

    sub.add_parser("restore", help="put the device back from the saved state file")
    sub.add_parser("doctor", help="report non-default configuration on the device")

    a = p.parse_args(argv)
    handlers = {"profiles": cmd_profiles, "plan": cmd_plan, "run": cmd_run,
                "capabilities": cmd_capabilities, "restore": cmd_restore,
                "doctor": cmd_doctor}
    needs_device = a.cmd not in ("profiles",) or False
    dev = Device(a.serial) if needs_device else None
    try:
        return handlers[a.cmd](dev, a)
    except subprocess.TimeoutExpired:
        die("adb timed out -- the device may be busy or disconnected. "
            "If a sweep was in flight, run `restore`.")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
