#!/usr/bin/env python3
"""Agent-shaped Android UI driver over plain adb. Standard library only.

Emits a COMPACT screen snapshot (visible + interactable nodes, numbered, with a
tap point) instead of the raw `uiautomator dump` XML, which is ~25x larger and
mostly layout scaffolding. Every subcommand is one shot: no server, no session,
no device-side agent APK.

Safety: auto-selects a device ONLY when it is an emulator. A physical phone must
be named with --serial or ANDROID_SERIAL, so a stray `tap` can never land on
someone's real handset.

Exit codes: 0 ok | 1 assertion failed / not found / stale index | 2 usage or device error.
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
import tempfile
import time
import xml.etree.ElementTree as ET

DEFAULT_LIMIT = 50
DUMP_RETRIES = 3
ADB_TIMEOUT = 60

# ---------------------------------------------------------------- adb plumbing


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def find_adb() -> str:
    """Locate adb: PATH first, then the standard SDK locations."""
    exe = shutil.which("adb")
    if exe:
        return exe
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
             os.path.expanduser("~/Library/Android/sdk"),
             os.path.expanduser("~/Android/Sdk"),
             "/usr/local/share/android-sdk", "/opt/android-sdk"]
    for root in roots:
        if not root:
            continue
        cand = os.path.join(root, "platform-tools", "adb")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    die("adb not found. Install Android platform-tools, or set ANDROID_HOME to "
        "your SDK (e.g. export ANDROID_HOME=~/Library/Android/sdk).")


class Device:
    def __init__(self, serial: str | None):
        self.adb = find_adb()
        self.serial = serial or os.environ.get("ANDROID_SERIAL")
        if not self.serial:
            self.serial = self._autoselect()

    def _autoselect(self) -> str:
        online = self.list_devices()
        if not online:
            die("no device. Boot an emulator or connect a phone, then re-run "
                "(`adb devices` should list it as 'device').")
        if len(online) > 1:
            die(f"{len(online)} devices attached: {', '.join(online)}. "
                "Pass --serial <id> or set ANDROID_SERIAL.")
        only = online[0]
        if not only.startswith("emulator-"):
            self.serial = only  # needed for the getprop below
            chars = self.shell("getprop", "ro.build.characteristics").strip()
            if "emulator" not in chars:
                die(f"{only} looks like a physical device. Naming it is required so "
                    f"automation cannot touch a real phone by accident: "
                    f"--serial {only} (or export ANDROID_SERIAL={only}).")
        return only

    def list_devices(self) -> list[str]:
        out = subprocess.run([self.adb, "devices"], capture_output=True,
                             text=True, timeout=ADB_TIMEOUT).stdout
        return [ln.split("\t")[0] for ln in out.splitlines()[1:]
                if ln.strip() and ln.endswith("\tdevice")]

    def run(self, *args: str, binary: bool = False, timeout: int = ADB_TIMEOUT):
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=not binary)

    def shell(self, *args: str, timeout: int = ADB_TIMEOUT) -> str:
        # `adb shell` JOINS its arguments and runs the result through the
        # DEVICE's /system/bin/sh, so an unquoted `;` in any value executes on
        # the device. Quote every argument. (Verified: `app stop 'x;touch
        # /data/local/tmp/PWNED'` created that file before this was added.)
        r = self.run("shell", *(shlex.quote(str(a)) for a in args), timeout=timeout)
        return (r.stdout or "").replace("\r\n", "\n")

    # -- observation ------------------------------------------------------

    def dump_xml(self) -> str:
        """Raw hierarchy XML, with a temp-file fallback. Retries: dumps fail
        while the screen is animating."""
        last = ""
        for attempt in range(DUMP_RETRIES):
            xml = self._dump_once()
            if xml:
                return xml
            last = self._last_err
            time.sleep(0.6 * (attempt + 1))
        remote = "/data/local/tmp/uia-dump.xml"
        self.shell("uiautomator", "dump", remote)
        raw = (self.run("exec-out", "cat", remote, binary=True).stdout or b"")
        xml = self._trim(raw.decode("utf-8", "replace"))
        if xml:
            return xml
        hint = ""
        if "idle" in last.lower():
            hint = ("  The screen never went idle (animation, video, or a spinner). "
                    "Disable animations: adb shell settings put global "
                    "window_animation_scale 0 (also transition_animation_scale and "
                    "animator_duration_scale).")
        die(f"uiautomator dump failed after {DUMP_RETRIES} tries: {last[:300]}{hint}")

    def _dump_once(self) -> str:
        r = self.run("exec-out", "uiautomator", "dump", "/dev/tty", binary=True)
        raw = (r.stdout or b"").decode("utf-8", "replace")
        self._last_err = (raw + (r.stderr or b"").decode("utf-8", "replace")).strip()
        return self._trim(raw)

    @staticmethod
    def _trim(raw: str) -> str:
        """Keep only the XML. AOSP appends 'UI hierchary dumped to: ...' (its
        typo, not ours) after the closing tag."""
        start, end = raw.find("<?xml"), raw.rfind("</hierarchy>")
        if start == -1 or end == -1:
            return ""
        return raw[start: end + len("</hierarchy>")]

    def user(self) -> str:
        if getattr(self, "_user", None) is None:
            out = self.shell("cmd", "activity", "get-current-user").strip()
            self._user = out if out.isdigit() else "0"
        return self._user

    def screen_size(self) -> tuple[int, int]:
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", self.shell("wm", "size"))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    def current(self) -> str:
        """Focused package/activity. Much cheaper than a hierarchy dump."""
        for src in (("dumpsys", "window"), ("dumpsys", "activity", "activities")):
            out = self.shell(*src)
            m = re.search(r"(?:mCurrentFocus|mResumedActivity)[^\n]*?"
                          r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", out)
            if m:
                return m.group(1)
        return "?"


# ---------------------------------------------------------------- hierarchy

BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
CHECKABLE = ("CheckBox", "Switch", "RadioButton", "ToggleButton", "CheckedTextView")


class Node:
    __slots__ = ("text", "desc", "rid", "cls", "pkg", "clickable", "longclick",
                 "checkable", "checked", "scrollable", "editable", "password",
                 "enabled", "selected", "focused", "x1", "y1", "x2", "y2")

    def __init__(self, el: ET.Element):
        a = el.attrib
        self.text = (a.get("text") or "").strip()
        self.desc = (a.get("content-desc") or "").strip()
        rid = a.get("resource-id") or ""
        self.rid = rid.split("/", 1)[1] if "/" in rid else rid
        self.cls = (a.get("class") or "").rsplit(".", 1)[-1]
        self.pkg = a.get("package") or ""
        self.clickable = a.get("clickable") == "true"
        self.longclick = a.get("long-clickable") == "true"
        self.checkable = a.get("checkable") == "true"
        self.checked = a.get("checked") == "true"
        self.scrollable = a.get("scrollable") == "true"
        self.editable = self.cls.endswith("EditText")
        self.password = a.get("password") == "true"
        self.enabled = a.get("enabled") != "false"
        self.selected = a.get("selected") == "true"
        self.focused = a.get("focused") == "true"
        m = BOUNDS.search(a.get("bounds") or "")
        self.x1, self.y1, self.x2, self.y2 = (
            (int(m.group(i)) for i in (1, 2, 3, 4)) if m else (0, 0, 0, 0))

    @property
    def w(self) -> int: return self.x2 - self.x1

    @property
    def h(self) -> int: return self.y2 - self.y1

    @property
    def center(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    @property
    def actionable(self) -> bool:
        return self.clickable or self.editable or self.checkable or self.longclick

    @property
    def role(self) -> str:
        if self.editable:
            return "edit"
        if self.checkable or self.cls in CHECKABLE:
            return "chk"
        if self.scrollable and not self.clickable:
            return "scroll"
        if self.clickable or self.cls.endswith("Button"):
            return "btn"
        if self.cls.endswith("ImageView"):
            return "img"
        return "txt"

    @property
    def label(self) -> str:
        return self.text or self.desc


def parse(xml: str, w: int, h: int) -> list[Node]:
    """Flatten the hierarchy, keeping on-screen nodes only."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        die(f"could not parse the UI hierarchy: {e}")
    if root.tag != "hierarchy":
        die(f"unexpected dump root <{root.tag}> (expected <hierarchy>)")
    nodes: list[Node] = []
    for el in root.iter("node"):
        n = Node(el)
        if n.w > 0 and n.h > 0 and n.x1 < w and n.y1 < h and n.x2 > 0 and n.y2 > 0:
            nodes.append(n)
    return nodes


def _contains(outer: Node, inner: Node) -> bool:
    return (outer.x1 <= inner.x1 and outer.y1 <= inner.y1
            and outer.x2 >= inner.x2 and outer.y2 >= inner.y2)


def compact(nodes: list[Node], limit: int) -> tuple[list[Node], int]:
    """Keep what an agent can act on or read; drop layout scaffolding.

    Follows the AndroidWorld/M3A heuristic (visible interactable nodes plus
    text-bearing nodes), then folds text-only children into their actionable
    parent so a button appears once, with the parent's correct tap target.
    Returns (rows, total_before_cap) so truncation is never silent.
    """
    interesting = [n for n in nodes
                   if n.label or n.actionable or n.scrollable]

    for n in interesting:
        if n.label or not n.actionable:
            continue
        inner = [c for c in interesting
                 if c is not n and c.label and _contains(n, c) and not c.actionable]
        if inner:
            n.text = " / ".join(dict.fromkeys(c.label for c in inner))[:120]
            for c in inner:
                c.text = c.desc = ""  # absorbed into the parent row

    out = [n for n in interesting if n.label or n.actionable or n.scrollable]

    # Drop an outer container whose label is already carried by an inner row.
    kept: list[Node] = []
    for n in sorted(out, key=lambda n: n.w * n.h):
        if n.label and any(n.label == k.label and _contains(n, k) for k in kept):
            continue
        kept.append(n)

    kept.sort(key=lambda n: (n.y1, n.x1))
    total = len(kept)
    return (kept[:limit] if limit else kept), total


def clip(s: str, n: int = 80) -> str:
    """Real apps put whole object dumps in content-desc (one F-Droid button
    carries ~2000 chars of every locale). Uncapped, a single row can dwarf the
    entire snapshot. Matching still runs against the full string."""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render(n: Node, i: int) -> str:
    parts = [f"{i} {n.role}"]
    parts.append(json.dumps(clip(n.text or n.desc), ensure_ascii=False))
    if n.desc and n.text and n.desc != n.text:
        parts.append(f"desc={json.dumps(clip(n.desc, 40), ensure_ascii=False)}")
    x, y = n.center
    parts.append(f"@{x},{y}")
    if n.rid:
        parts.append(f"#{n.rid}")
    if n.checkable:
        parts.append(f"checked={'true' if n.checked else 'false'}")
    if n.selected:
        parts.append("selected")
    if n.password:
        parts.append("password")
    if n.scrollable:
        parts.append("scrollable")
    if not n.enabled:
        parts.append("disabled")
    return " ".join(parts)


# ---------------------------------------------------------------- snapshot i/o


def cache_path(serial: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", serial or "default")
    return os.path.join(tempfile.gettempdir(), f"uia-{safe}.json")


def snapshot(dev: Device, limit: int) -> tuple[list[Node], int, int, int, str]:
    w, h = dev.screen_size()
    nodes = parse(dev.dump_xml(), w or 10 ** 6, h or 10 ** 6)
    els, total = compact(nodes, limit)
    act = dev.current()
    try:
        path = cache_path(dev.serial)
        # holds on-screen text, which can include personal data
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"act": act,
                       "els": [{"i": i, "text": n.text, "desc": n.desc,
                                "rid": n.rid, "role": n.role, "xy": list(n.center)}
                               for i, n in enumerate(els)]}, fh)
    except OSError:
        pass
    return els, w, h, total, act


def load_cache(dev: Device) -> dict:
    try:
        with open(cache_path(dev.serial)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def match(els: list[Node], text=None, rid=None, desc=None) -> list[int]:
    """Exact match first, then case-insensitive substring."""
    def hits(pred) -> list[int]:
        return [i for i, n in enumerate(els) if pred(n)]

    if rid:
        return hits(lambda n: n.rid == rid) or hits(lambda n: rid.lower() in n.rid.lower())
    if desc:
        return hits(lambda n: n.desc == desc) or hits(lambda n: desc.lower() in n.desc.lower())
    if text:
        return (hits(lambda n: n.text == text or n.desc == text)
                or hits(lambda n: text.lower() in n.text.lower()
                        or text.lower() in n.desc.lower()))
    return []


# ---------------------------------------------------------------- commands


def cmd_snap(dev: Device, a) -> int:
    limit = 0 if a.all else a.limit
    els, w, h, total, act = snapshot(dev, limit)
    if a.json:
        print(json.dumps({"activity": act, "size": [w, h], "total": total,
                          "shown": len(els), "truncated": total > len(els),
                          "elements": [{"i": i, "role": n.role, "text": n.text,
                                        "desc": n.desc, "id": n.rid,
                                        "center": list(n.center),
                                        "bounds": [n.x1, n.y1, n.x2, n.y2],
                                        "enabled": n.enabled, "checked": n.checked,
                                        "selected": n.selected,
                                        "password": n.password,
                                        "scrollable": n.scrollable}
                                       for i, n in enumerate(els)]}, indent=1))
        return 0
    print(f"act={act} size={w}x{h} n={len(els)}"
          + (f" TRUNCATED(of {total}, --all for the rest)" if total > len(els) else ""))
    for i, n in enumerate(els):
        print(render(n, i))
    if not els:
        print("(nothing readable — the app may draw to a canvas (game/Flutter without "
              "semantics) or the screen may be FLAG_SECURE. Take a screenshot and "
              "read it visually.)")
    return 0


def cmd_text(dev: Device, a) -> int:
    els, _, _, _, _ = snapshot(dev, 0)
    seen = []
    for n in els:
        for v in (n.text, n.desc):
            if v and v not in seen:
                seen.append(clip(v, 200))
    print("\n".join(seen) if seen else
          "(no text exposed — canvas-drawn app or FLAG_SECURE screen; screenshot it instead)")
    return 0


def _resolve(dev: Device, a) -> tuple[int, int, str]:
    """Resolve --at / index / --text|--id|--desc to a tap point."""
    if getattr(a, "at", None):
        try:
            x, y = (int(v) for v in a.at.split(","))
        except ValueError:
            die("--at wants X,Y (e.g. --at 540,1200)")
        return x, y, f"({x},{y})"

    sel = a.text or a.id or a.desc
    if getattr(a, "target", None) is not None and not sel:
        cache = load_cache(dev)
        els = cache.get("els") or []
        if not els:
            die("no cached snapshot — run `snap` before tapping by index")
        now = dev.current()
        if cache.get("act") and now != cache["act"]:
            print(f"error: the screen changed since that snapshot "
                  f"({cache['act']} -> {now}); indices are stale. Re-run `snap`.",
                  file=sys.stderr)
            sys.exit(1)
        try:
            e = els[int(a.target)]
        except (ValueError, IndexError):
            die(f"index {a.target} not in the last snapshot (it had {len(els)} "
                f"elements); re-run `snap`")
        return e["xy"][0], e["xy"][1], f'{e["role"]} "{e["text"] or e["desc"]}"'

    els, _, _, _, _ = snapshot(dev, 0)
    idx = match(els, a.text, a.id, a.desc)
    if not idx:
        labels = [n.label for n in els if n.label][:15]
        print(f"error: no element matching {sel!r}. On screen now: "
              f"{', '.join(repr(x) for x in labels) or '(nothing readable)'}",
              file=sys.stderr)
        sys.exit(1)
    nth = getattr(a, "nth", None)
    if nth is not None and nth >= len(idx):
        die(f"--nth {nth} but only {len(idx)} elements match {sel!r}")
    if len(idx) > 1 and nth is None and not getattr(a, "first", False):
        # Mutating actions fail closed on ambiguity: a warning does not stop the
        # wrong "Delete" from being tapped. Queries (assert/wait) stay permissive.
        opts = "; ".join(f"--nth {k}: {els[i].role} "
                         f"{json.dumps(clip(els[i].label, 40), ensure_ascii=False)} "
                         f"@{els[i].center[0]},{els[i].center[1]}"
                         for k, i in enumerate(idx[:6]))
        print(f"error: {len(idx)} elements match {sel!r} — refusing to guess which "
              f"to act on. Pick one with --nth N, or --first to take the first. "
              f"Matches: {opts}", file=sys.stderr)
        sys.exit(1)
    n = els[idx[nth or 0]]
    x, y = n.center
    return x, y, f'{n.role} "{n.label}"'


def cmd_tap(dev: Device, a) -> int:
    x, y, what = _resolve(dev, a)
    if a.long:
        dev.shell("input", "swipe", str(x), str(y), str(x), str(y), "800")
        print(f"long-pressed {what} at {x},{y}")
    else:
        dev.shell("input", "tap", str(x), str(y))
        print(f"tapped {what} at {x},{y}")
    return 0


def cmd_type(dev: Device, a) -> int:
    non_ascii = [c for c in a.value if ord(c) > 127]
    if non_ascii:
        die(f"`adb shell input text` cannot send non-ASCII "
            f"({''.join(sorted(set(non_ascii)))!r}) — on Android 15 it throws and "
            f"types NOTHING. There is no adb-only workaround (no `cmd clipboard` "
            f"either). Escalate: pip install uiautomator2, then "
            f"u2.connect().send_keys(text), or install an IME such as ADBKeyBoard.")
    if a.into is not None or a.text_sel or a.id or a.desc or a.at:
        a.target, a.text = a.into, a.text_sel
        x, y, what = _resolve(dev, a)
        dev.shell("input", "tap", str(x), str(y))
        time.sleep(1.2)  # a field that has not focused yet swallows the keystrokes
        print(f"focused {what}")
    # `input text` splits on spaces and interprets %; encode both.
    payload = a.value.replace("%", "%%").replace(" ", "%s")
    out = dev.shell("input", "text", payload)
    if "Exception" in out:
        die(f"input text failed on device: {out.strip().splitlines()[0]}")
    print(f"typed {a.value!r}")
    return 0


KEYS = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER",
        "tab": "KEYCODE_TAB", "delete": "KEYCODE_DEL", "escape": "KEYCODE_ESCAPE",
        "recents": "KEYCODE_APP_SWITCH", "menu": "KEYCODE_MENU",
        "search": "KEYCODE_SEARCH", "power": "KEYCODE_POWER",
        "volup": "KEYCODE_VOLUME_UP", "voldown": "KEYCODE_VOLUME_DOWN",
        "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT"}


def cmd_key(dev: Device, a) -> int:
    code = KEYS.get(a.name.lower(), a.name.upper())
    if not code.startswith("KEYCODE_") and not code.isdigit():
        die(f"unknown key {a.name!r}. Known: {', '.join(sorted(KEYS))}, "
            "or any KEYCODE_* / raw keycode number.")
    dev.shell("input", "keyevent", code)
    print(f"pressed {code}")
    return 0


def cmd_swipe(dev: Device, a) -> int:
    w, h = dev.screen_size()
    cx, cy = w // 2, h // 2
    dy, dx = int(h * 0.35), int(w * 0.35)
    moves = {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
             "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy)}
    x1, y1, x2, y2 = moves[a.direction]
    dev.shell("input", "swipe", *map(str, (x1, y1, x2, y2, a.ms)))
    print(f"swiped {a.direction} ({x1},{y1} -> {x2},{y2})")
    return 0


def cmd_shot(dev: Device, a) -> int:
    path = a.path or f"screen-{int(time.time())}.png"
    data = dev.run("exec-out", "screencap", "-p", binary=True).stdout or b""
    if not data.startswith(b"\x89PNG"):
        die("screencap returned no PNG — the device may be asleep or the adb "
            "transport wedged. (A FLAG_SECURE screen returns a valid but BLACK "
            "PNG, not an error.)")
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"{path} ({len(data)} bytes)")
    return 0


def cmd_wait(dev: Device, a) -> int:
    deadline = time.time() + a.timeout
    want = a.text or a.id or a.desc
    while True:
        els, _, _, _, _ = snapshot(dev, 0)
        if bool(match(els, a.text, a.id, a.desc)) != bool(a.gone):
            print(f"{'gone' if a.gone else 'found'}: {want!r}")
            return 0
        if time.time() >= deadline:
            print(f"error: timed out after {a.timeout}s waiting for {want!r} to "
                  f"{'disappear' if a.gone else 'appear'}", file=sys.stderr)
            return 1
        time.sleep(0.5)


def cmd_assert(dev: Device, a) -> int:
    if a.activity:
        cur = dev.current()
        ok = a.activity in cur
        print(f"{'PASS' if ok else 'FAIL'} activity contains {a.activity!r} "
              f"(actual: {cur})", file=None if ok else sys.stderr)
        return 0 if ok else 1
    want = a.text or a.id or a.desc
    if not want:
        die("assert needs --text, --id, --desc or --activity")
    els, _, _, _, _ = snapshot(dev, 0)
    ok = bool(match(els, a.text, a.id, a.desc)) != bool(a.absent)
    if ok:
        print(f"PASS {want!r} {'absent' if a.absent else 'present'}")
        return 0
    labels = [n.label for n in els if n.label][:20]
    print(f"FAIL expected {want!r} to be {'absent' if a.absent else 'present'}. "
          f"On screen: {', '.join(repr(x) for x in labels) or '(nothing readable)'}",
          file=sys.stderr)
    return 1


def cmd_app(dev: Device, a) -> int:
    if a.action == "current":
        print(dev.current())
        return 0
    if a.action == "list":
        out = dev.shell("pm", "list", "packages", "-3")
        pkgs = sorted(l.split(":", 1)[1] for l in out.splitlines() if ":" in l)
        if a.pkg:
            pkgs = [p for p in pkgs if a.pkg.lower() in p.lower()]
        print("\n".join(pkgs) or
              "(no user-installed packages; -3 excludes system apps)")
        return 0
    if not a.pkg:
        die(f"`app {a.action}` needs a package name")
    if a.action == "start":
        brief = dev.shell("cmd", "package", "resolve-activity", "--brief",
                          "--user", dev.user(),
                          "-a", "android.intent.action.MAIN",
                          "-c", "android.intent.category.LAUNCHER", a.pkg)
        comp = next((l.strip() for l in brief.splitlines()
                     if "/" in l and not l.startswith("No ")), "")
        if comp:
            dev.shell("am", "start", "-W", "-n", comp)
            print(f"started {comp}")
        else:
            dev.shell("monkey", "-p", a.pkg, "-c",
                      "android.intent.category.LAUNCHER", "1")
            print(f"started {a.pkg} (via monkey; no launcher activity resolved — "
                  f"the app may be a service, a widget, or not installed)")
        return 0
    verbs = {"stop": ("am", "force-stop"), "clear": ("pm", "clear")}
    print(dev.shell(*verbs[a.action], a.pkg).strip() or f"{a.action} {a.pkg}: ok")
    return 0


# ---------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uia",
                                description="Compact Android UI driver over adb.")
    p.add_argument("--serial", help="target device (required for physical phones)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snap", help="compact numbered snapshot of the screen")
    s.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    s.add_argument("--all", action="store_true", help="no element cap")
    s.add_argument("--json", action="store_true")

    sub.add_parser("text", help="just the readable text, in reading order")
    sub.add_parser("devices", help="list attached devices")

    def selectors(sp, index=True):
        if index:
            sp.add_argument("target", nargs="?", help="index from the last snap")
        sp.add_argument("--text", help="match visible text or content-desc")
        sp.add_argument("--id", help="match resource-id (short form)")
        sp.add_argument("--desc", help="match content-desc")
        sp.add_argument("--at", help="raw X,Y (last resort)")
        sp.add_argument("--nth", type=int, help="pick the Nth match (0-based)")

    t = sub.add_parser("tap", help="tap (or --long press) an element")
    selectors(t)
    t.add_argument("--long", action="store_true", help="long-press instead")
    t.add_argument("--first", action="store_true",
                   help="accept the first of several matches (default: refuse)")

    ty = sub.add_parser("type", help="type ASCII text, optionally focusing a field")
    ty.add_argument("value")
    ty.add_argument("--into", type=int, help="index of the field to focus first")
    ty.add_argument("--text", dest="text_sel", help="focus the field matching this")
    ty.add_argument("--id", help="focus the field with this resource-id")
    ty.add_argument("--desc", help="focus the field with this content-desc")
    ty.add_argument("--at", help="focus this X,Y first")
    ty.add_argument("--nth", type=int, help="pick the Nth match (0-based)")
    ty.add_argument("--first", action="store_true",
                    help="accept the first of several matches (default: refuse)")

    k = sub.add_parser("key", help="press a key (back, home, enter, ...)")
    k.add_argument("name")

    sw = sub.add_parser("swipe", help="swipe; direction = the way the FINGER moves "
                                      "(swipe up scrolls the page down)")
    sw.add_argument("direction", choices=["up", "down", "left", "right"])
    sw.add_argument("--ms", type=int, default=300)

    sh = sub.add_parser("shot", help="save a PNG screenshot")
    sh.add_argument("path", nargs="?")

    w = sub.add_parser("wait", help="poll until an element appears (or --gone)")
    selectors(w, index=False)
    w.set_defaults(first=True)   # a query, not a mutation: any match counts
    w.add_argument("--timeout", type=float, default=15)
    w.add_argument("--gone", action="store_true")

    a = sub.add_parser("assert", help="assert screen state; exits 1 on failure")
    selectors(a, index=False)
    a.set_defaults(first=True)   # a query, not a mutation: any match counts
    a.add_argument("--activity", help="assert the focused activity contains this")
    a.add_argument("--absent", action="store_true", help="assert NOT present")

    ap = sub.add_parser("app", help="lifecycle: start/stop/clear/current/list")
    ap.add_argument("action", choices=["start", "stop", "clear", "current", "list"])
    ap.add_argument("pkg", nargs="?")
    return p


HANDLERS = {"snap": cmd_snap, "text": cmd_text, "tap": cmd_tap, "type": cmd_type,
            "key": cmd_key, "swipe": cmd_swipe, "shot": cmd_shot, "wait": cmd_wait,
            "assert": cmd_assert, "app": cmd_app}


FIXTURE = """<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="p"
 content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,2400]">
 <node index="0" text="" resource-id="p:id/row" class="android.widget.LinearLayout"
  content-desc="" clickable="true" enabled="true" bounds="[0,100][1080,300]">
  <node index="0" text="Wi-Fi" resource-id="p:id/title" class="android.widget.TextView"
   content-desc="" clickable="false" enabled="true" bounds="[20,120][500,200]" />
  <node index="1" text="Connected" resource-id="p:id/sum" class="android.widget.TextView"
   content-desc="" clickable="false" enabled="true" bounds="[20,210][500,280]" />
 </node>
 <node index="1" text="" resource-id="p:id/email" class="android.widget.EditText"
  content-desc="Email" clickable="true" enabled="true" password="false"
  bounds="[0,400][1080,500]" />
 <node index="2" text="Secret" resource-id="p:id/pw" class="android.widget.EditText"
  content-desc="" clickable="true" enabled="true" password="true"
  bounds="[0,520][1080,620]" />
 <node index="3" text="Offscreen" resource-id="" class="android.widget.TextView"
  content-desc="" clickable="false" enabled="true" bounds="[0,9000][1080,9100]" />
 <node index="4" text="Off" resource-id="p:id/sw" class="android.widget.Switch"
  content-desc="" checkable="true" checked="false" clickable="true" enabled="true"
  bounds="[900,700][1050,760]" />
</node></hierarchy>"""


def self_test() -> int:
    """Offline checks for the pure logic. No device, no adb -- runs in CI."""
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    nodes = parse(FIXTURE, 1080, 2400)
    check("offscreen node dropped", not any(n.text == "Offscreen" for n in nodes))

    els, total = compact(nodes, 0)
    labels = [n.text or n.desc for n in els]
    check("clickable parent absorbs its text children",
          any(l == "Wi-Fi / Connected" for l in labels))
    check("absorbed children are not emitted separately",
          "Wi-Fi" not in labels and "Connected" not in labels)
    check("EditText gets the edit role",
          any(n.role == "edit" and n.desc == "Email" for n in els))
    check("password field is flagged", any(n.password for n in els))
    check("checkable keeps its state",
          any(n.role == "chk" and not n.checked for n in els))
    check("rows are in reading order",
          [n.y1 for n in els] == sorted(n.y1 for n in els))

    _, total_all = compact(parse(FIXTURE, 1080, 2400), 0)
    capped, total_capped = compact(parse(FIXTURE, 1080, 2400), 2)
    check("cap limits rows but reports the true total",
          len(capped) == 2 and total_capped == total_all)

    check("clip caps long labels", len(clip("x" * 500)) == 80)
    check("clip leaves short labels alone", clip("Sign in") == "Sign in")
    check("clip collapses whitespace", clip("a\n  b") == "a b")

    check("match is exact-first",
          match(els, text="Wi-Fi / Connected") == [i for i, n in enumerate(els)
                                                   if n.text == "Wi-Fi / Connected"])
    check("match falls back to substring", bool(match(els, text="connected")))
    check("match on absorbed child text still resolves", bool(match(els, text="Wi-Fi")))
    check("resource-id is shortened past the slash",
          any(n.rid == "email" for n in els))

    print(f"RESULT: {'ok' if not fails else 'fail'} "
          f"({len(fails)} failed)" if fails else "RESULT: ok")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    args = build_parser().parse_args(argv)
    if args.cmd == "devices":
        adb = find_adb()
        print(subprocess.run([adb, "devices", "-l"], capture_output=True,
                             text=True, timeout=ADB_TIMEOUT).stdout.strip())
        return 0
    dev = Device(args.serial)
    try:
        return HANDLERS[args.cmd](dev, args)
    except subprocess.TimeoutExpired:
        die(f"adb timed out after {ADB_TIMEOUT}s — the device may be busy, asleep, "
            f"or disconnected.")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
