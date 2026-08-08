#!/usr/bin/env python3
"""Record an Android app walkthrough to MP4 and composite explanation cards.

Three phases, so the narration is captured while you drive rather than guessed
afterwards:

    start   begin screenrecord, note t0
    mark    stamp an explanation card at "now" (call it as you drive the app)
    stop    end recording, pull, render the cards onto the video

`render` re-composites an existing session with edited card text -- no re-record.

You cannot inject an overlay into a third-party Android app the way a web
recorder injects DOM, so cards are burned in afterwards with ffmpeg `overlay`
against the timeline `mark` captured.

Standard library, plus Pillow (card rendering) and ffmpeg (compositing) -- both
needed only by `stop`/`render`, not by `start`/`mark`.

Exit codes: 0 ok | 1 nothing recorded / prerequisite missing | 2 usage or device error.
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

ADB_TIMEOUT = 120
FADE = 0.25
REMOTE = "/sdcard/walkthrough-rec.mp4"
SESSION = "session.json"

FONT_CANDIDATES = {
    "bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf"],
    "regular": ["/System/Library/Fonts/Supplemental/Arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
                "C:/Windows/Fonts/arial.ttf"],
}

THEMES = {
    # glass: minimal frosted overlay — a translucent dark panel (the app shows
    # through), a hairline highlight border, no accent stripe. Reads as glass over
    # both light and dark content without a real backdrop blur.
    "glass":  {"panel": (14, 16, 22, 184), "accent": (150, 204, 255, 255),
               "kicker": (150, 204, 255, 255), "title": (255, 255, 255, 255),
               "body": (226, 231, 240, 255), "border": (255, 255, 255, 48),
               "bar": False, "hero_alpha": 222},
    "coral":  {"panel": (16, 20, 24, 242), "accent": (255, 107, 74, 255),
               "kicker": (255, 138, 110, 255), "title": (255, 255, 255, 255),
               "body": (200, 205, 212, 255)},
    "indigo": {"panel": (18, 18, 34, 242), "accent": (124, 122, 255, 255),
               "kicker": (163, 161, 255, 255), "title": (255, 255, 255, 255),
               "body": (201, 201, 220, 255)},
    "paper":  {"panel": (247, 245, 240, 246), "accent": (34, 34, 34, 255),
               "kicker": (140, 96, 60, 255), "title": (20, 20, 20, 255),
               "body": (70, 70, 70, 255)},
}


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
                die(f"{devs[0]} looks like a physical device — name it explicitly "
                    f"with --serial {devs[0]} so nothing records a real phone by "
                    f"accident (recordings capture whatever is on screen).")
        return devs[0]

    def run(self, *args, binary=False, timeout=ADB_TIMEOUT):
        return subprocess.run([self.adb, "-s", self.serial] + list(args),
                              capture_output=True, timeout=timeout, text=not binary)

    def shell(self, *args, timeout=ADB_TIMEOUT) -> str:
        # `adb shell` joins args and runs them through the DEVICE's sh -- quote each.
        quoted = [shlex.quote(str(a)) for a in args]
        return (self.run("shell", *quoted, timeout=timeout).stdout or "").replace("\r\n", "\n")

    def size(self) -> tuple[int, int]:
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", self.shell("wm", "size"))
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)


# ---------------------------------------------------------------- session io


def session_path(out: str) -> str:
    return os.path.join(out, SESSION)


def load_session(out: str) -> dict:
    try:
        with open(session_path(out)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        die(f"no recording session in {out!r} — run `start --out {out}` first", 1)


def save_session(out: str, data: dict):
    os.makedirs(out, exist_ok=True)
    with open(session_path(out), "w") as fh:
        json.dump(data, fh, indent=1)


# ---------------------------------------------------------------- commands


def cmd_start(a) -> int:
    dev = Device(a.serial)
    if os.path.exists(session_path(a.out)) and not a.force:
        die(f"{session_path(a.out)} already exists — `stop` it, pick another "
            f"--out, or pass --force to overwrite")
    dev.shell("rm", "-f", REMOTE)

    prev_touches = dev.shell("settings", "get", "system", "show_touches").strip()
    if a.show_touches:
        # Android's own touch feedback: the closest thing to a rendered cursor,
        # and the only one available since we cannot draw inside the app.
        dev.shell("settings", "put", "system", "show_touches", "1")

    cmd = [dev.adb, "-s", dev.serial, "shell", "screenrecord",
           "--time-limit", "0", "--bit-rate", a.bitrate]
    if a.size:
        cmd += ["--size", a.size]
    cmd.append(REMOTE)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # t0 is when the file actually starts growing, not when Popen returned --
    # screenrecord takes a beat to spin up the encoder.
    t0, deadline = None, time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            die(f"screenrecord exited immediately: {err or 'no output'}", 1)
        out = dev.shell("ls", "-l", REMOTE)
        if REMOTE.split("/")[-1] in out and " 0 " not in out:
            t0 = time.time()
            break
        time.sleep(0.25)
    if t0 is None:
        proc.kill()
        die("screenrecord never produced a file — the device may not support "
            "recording at this resolution; try --size 720x1600.", 1)

    w, h = dev.size()
    save_session(a.out, {"serial": dev.serial, "t0": t0, "width": w, "height": h,
                         "pid": proc.pid, "cards": [], "theme": a.theme,
                         "prev_show_touches": prev_touches,
                         "show_touches": bool(a.show_touches)})
    print(f"recording {w}x{h} -> {a.out}/  (session started)")
    print("drive the app now; call `mark` for each explanation card, then `stop`.")
    return 0


def cmd_mark(a) -> int:
    s = load_session(a.out)
    t = round(time.time() - s["t0"], 2)
    card = {"t": max(t, 0.0), "kicker": a.kicker, "title": a.title,
            "body": a.body, "duration": a.duration, "style": a.style,
            "pos": a.pos}
    s["cards"].append(card)
    save_session(a.out, s)
    print(f"card @ {card['t']:.2f}s ({a.style}, {a.duration}s): {a.title!r}")
    return 0


def cmd_stop(a) -> int:
    s = load_session(a.out)
    dev = Device(s["serial"])
    dev.shell("pkill", "-SIGINT", "screenrecord")
    # screenrecord needs a moment to finalise the MP4 moov atom after SIGINT;
    # pulling too early yields an unplayable file.
    time.sleep(a.settle)
    if s.get("show_touches"):
        prev = s.get("prev_show_touches") or "0"
        dev.shell("settings", "put", "system", "show_touches",
                  prev if prev.isdigit() else "0")

    raw = os.path.join(a.out, "raw.mp4")
    dev.run("pull", REMOTE, raw, timeout=600)
    if not os.path.isfile(raw) or os.path.getsize(raw) < 1024:
        die("nothing was recorded — the device produced no usable file.", 1)
    dev.shell("rm", "-f", REMOTE)
    s["raw"] = raw
    save_session(a.out, s)
    print(f"pulled {raw} ({os.path.getsize(raw) // 1024} KB)")
    return render(a.out, a.name, gif=a.gif, speed=a.speed, fps=a.fps,
                  tighten=a.tighten, gap_speed=a.gap_speed, srt=a.srt)


def cmd_render(a) -> int:
    load_session(a.out)
    return render(a.out, a.name, gif=a.gif, speed=a.speed, fps=a.fps,
                  tighten=a.tighten, gap_speed=a.gap_speed, srt=a.srt)


# ---------------------------------------------------------------- timeline


def warp_plan(duration: float, cards: list, gap_speed: float = 4.0,
              min_gap: float = 1.5, pre: float = 0.6, post: float = 0.6):
    """Split the take into segments, speeding up only the stretches with no card.

    A global --speed distorts touch animations and transitions alike. The card
    timestamps already say which stretches carry meaning, so protect those at
    1.0x and compress the driving dead air (mostly ~2s hierarchy dumps) between
    them. Returns (segments, remap) where segments is [(start, end, speed)] and
    remap maps an original timestamp onto the tightened timeline.
    """
    if duration <= 0 or not cards:
        return [], (lambda x: x)  # nothing to protect => nothing to compress
    keep = []
    for c in sorted(cards, key=lambda c: c["t"]):
        s = max(0.0, c["t"] - pre)
        e = min(duration, c["t"] + c.get("duration", 3.0) + post)
        if keep and s <= keep[-1][1]:
            keep[-1][1] = max(keep[-1][1], e)
        else:
            keep.append([s, e])

    segments, cur = [], 0.0
    for s, e in keep:
        if s > cur:
            gap = s - cur
            segments.append((cur, s, gap_speed if gap >= min_gap else 1.0))
        segments.append((s, e, 1.0))
        cur = e
    if cur < duration:
        gap = duration - cur
        segments.append((cur, duration, gap_speed if gap >= min_gap else 1.0))

    merged = []
    for seg in segments:
        if merged and merged[-1][2] == seg[2] and abs(merged[-1][1] - seg[0]) < 1e-6:
            merged[-1] = (merged[-1][0], seg[1], seg[2])
        else:
            merged.append(seg)
    segments = merged

    def remap(t: float) -> float:
        out = 0.0
        for s, e, sp in segments:
            if t >= e:
                out += (e - s) / sp
            elif t > s:
                return out + (t - s) / sp
            else:
                break
        return out

    return segments, remap


# ---------------------------------------------------------------- rendering


def probe_duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path: str, cards: list, remap) -> None:
    """A sidecar caption track: accessible, searchable, and editable without
    re-rendering the video."""
    with open(path, "w") as fh:
        for i, c in enumerate(cards, 1):
            start = remap(c["t"])
            end = start + c.get("duration", 3.0)
            text = c["title"] + (f"\n{c['body']}" if c.get("body") else "")
            fh.write(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")


def load_font(kind: str, size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES[kind]:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def wrap(text: str, font, max_w: int, draw) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def wrap_multi(text: str, font, max_w: int, draw) -> list[str]:
    """Wrap, but honour explicit newlines in the card body so a card can be
    structured into short lines (e.g. `● 2 dex · 4 ABIs` on its own row). A blank
    source line becomes a blank spacer line."""
    out: list[str] = []
    for para in text.split("\n"):
        out.extend(wrap(para, font, max_w, draw) if para.strip() else [""])
    return out


def kicker_text(k: str) -> str:
    # A slash-prefixed skill name (`/android-ux-audit`) is a command — keep its
    # case; a plain kicker is a label — upper-case it.
    return k if k.startswith("/") else k.upper()


def _render_hero(card: dict, width: int, height: int, theme: dict,
                 path: str) -> tuple[int, int]:
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (width, height),
                    theme["panel"][:3] + (theme.get("hero_alpha", 216),))
    d = ImageDraw.Draw(img)
    inner = int(width * 0.82)
    ft = load_font("bold", max(46, width // 13))
    fb = load_font("regular", max(28, width // 25))
    fk = load_font("bold", max(22, width // 34))
    tl = wrap(card["title"], ft, inner, d)
    bl = wrap_multi(card["body"], fb, inner, d) if card.get("body") else []
    lh_t, lh_b = int(ft.size * 1.2), int(fb.size * 1.35)
    block = ((int(fk.size * 1.9) if card.get("kicker") else 0)
             + len(tl) * lh_t + (int(fb.size * 0.7) if bl else 0) + len(bl) * lh_b)
    y = (height - block) // 2
    if card.get("kicker"):
        k = kicker_text(card["kicker"])
        d.text(((width - d.textlength(k, font=fk)) / 2, y), k, font=fk,
               fill=theme["accent"])
        y += int(fk.size * 1.9)
    for line in tl:
        d.text(((width - d.textlength(line, font=ft)) / 2, y), line, font=ft,
               fill=theme["title"])
        y += lh_t
    if bl:
        y += int(fb.size * 0.7)
    for line in bl:
        d.text(((width - d.textlength(line, font=fb)) / 2, y), line, font=fb,
               fill=theme["body"])
        y += lh_b
    img.save(path)
    return width, height


def render_card(card: dict, width: int, theme: dict, path: str,
                height: int = 0) -> tuple[int, int]:
    """Card PNG with alpha. A `hero` fills the frame with a scrim so a title
    card reads as a title card instead of a tooltip floating over busy content."""
    from PIL import Image, ImageDraw
    hero = card.get("style") == "hero"
    if hero and height:
        return _render_hero(card, width, height, theme, path)
    margin = int(width * 0.05)
    cw = width - margin * 2
    pad = int(width * 0.041)
    bar = max(8, width // 90) if (not hero and theme.get("bar", True)) else 0
    inner = cw - pad * 2 - bar

    fk = load_font("bold", max(20, width // 36))
    ft = load_font("bold", max(34, width // (16 if hero else 19)))
    fb = load_font("regular", max(26, width // 27))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tl = wrap(card["title"], ft, inner, probe)
    bl = wrap_multi(card["body"], fb, inner, probe) if card.get("body") else []

    lh_t, lh_b = int(ft.size * 1.22), int(fb.size * 1.3)
    h = (pad * 2 + (int(fk.size * 1.5) if card.get("kicker") else 0)
         + len(tl) * lh_t + (int(fb.size * 0.45) if bl else 0) + len(bl) * lh_b)

    img = Image.new("RGBA", (cw, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = int(width * 0.026)
    d.rounded_rectangle([0, 0, cw - 1, h - 1], radius=radius, fill=theme["panel"])
    if theme.get("border"):   # hairline highlight — the glass edge
        d.rounded_rectangle([0, 0, cw - 1, h - 1], radius=radius,
                            outline=theme["border"], width=max(2, width // 400))
    if bar:
        d.rounded_rectangle([0, 0, bar - 1, h - 1], radius=bar // 2,
                            fill=theme["accent"])
    x, y = pad + bar, pad
    if card.get("kicker"):
        d.text((x, y), kicker_text(card["kicker"]), font=fk, fill=theme["kicker"])
        y += int(fk.size * 1.5)
    for line in tl:
        d.text((x, y), line, font=ft, fill=theme["title"])
        y += lh_t
    if bl:
        y += int(fb.size * 0.45)
    for line in bl:
        d.text((x, y), line, font=fb, fill=theme["body"])
        y += lh_b
    img.save(path)
    return cw, h


def render(out: str, name: str | None, gif: bool = False, speed: float = 1.0,
           fps: int = 30, tighten: bool = True, gap_speed: float = 4.0,
           srt: bool = False) -> int:
    if not shutil.which("ffmpeg"):
        die("ffmpeg not found — install it (brew install ffmpeg / apt install ffmpeg).", 1)
    try:
        import PIL  # noqa: F401
    except ImportError:
        die("Pillow is required to render cards: pip install pillow. (Most ffmpeg "
            "builds, including Homebrew's, ship WITHOUT the drawtext filter, so "
            "cards are rendered as PNGs and composited with `overlay`.)", 1)

    s = load_session(out)
    raw = s.get("raw") or os.path.join(out, "raw.mp4")
    if not os.path.isfile(raw):
        die(f"{raw} is missing — run `stop` before rendering.", 1)

    # screenrecord emits VARIABLE frame rate: a static stretch (an idle feed, an
    # empty screen) encodes almost no frames. overlay composites a card only onto
    # frames that exist, so a card whose window lands on a static stretch has
    # nothing to draw on and silently never appears -- forcing CFR on OUTPUT is too
    # late (overlay already ran). Normalise to CFR up front so every card window
    # has dense frames to composite onto.
    cfr = os.path.join(out, "raw_cfr.mp4")
    rc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", raw, "-r", str(fps),
                         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                         "-pix_fmt", "yuv420p", "-an", cfr],
                        capture_output=True, text=True)
    if rc.returncode == 0 and os.path.isfile(cfr) and os.path.getsize(cfr) > 1024:
        raw = cfr
    else:
        print(f"warning: CFR normalise failed, cards over static stretches may "
              f"drop: {(rc.stderr or '').strip()[:200]}", file=sys.stderr)

    name = name or "walkthrough"
    final = os.path.join(out, f"{name}.mp4")
    theme = THEMES.get(s.get("theme") or "glass", THEMES["glass"])
    width, height = s.get("width", 1080), s.get("height", 1920)
    cards = sorted(s.get("cards", []), key=lambda c: c["t"])

    inputs, filters, cards_dir = ["-i", raw], [], os.path.join(out, "cards")
    os.makedirs(cards_dir, exist_ok=True)
    chain = "[0:v]"

    src_dur = probe_duration(raw)
    remap = (lambda x: x / (speed or 1.0))
    out_dur = src_dur / (speed or 1.0)
    if tighten and cards:
        dur = src_dur
        segments, warp = warp_plan(dur, cards, gap_speed=gap_speed)
        if len(segments) > 1:
            parts = []
            for i, (s, e, sp) in enumerate(segments):
                lbl = f"[w{i}]"
                filters.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},"
                               f"setpts=(PTS-STARTPTS)/{sp:.4f}{lbl}")
                parts.append(lbl)
            filters.append(f"{''.join(parts)}concat=n={len(parts)}:v=1:a=0[tw]")
            chain = "[tw]"
            saved = dur - warp(dur)
            print(f"tightened: {dur:.1f}s -> {warp(dur):.1f}s "
                  f"({len(segments)} segments, {saved:.1f}s of dead air removed)")
            remap = warp
            out_dur = warp(dur)
    if speed and speed != 1.0:
        base, prev = remap, chain
        remap = lambda x: base(x) / speed
        out_dur = out_dur / speed
        filters.append(f"{prev}setpts={1/speed:.4f}*PTS[sp]")
        chain = "[sp]"

    for i, c in enumerate(cards):
        png = os.path.join(cards_dir, f"card{i:02d}.png")
        cw, ch = render_card(c, width, theme, png, height)
        # fade the PNG in/out so cards do not pop
        inputs += ["-loop", "1", "-t", f"{c.get('duration', 3.0) + 1:.2f}", "-i", png]
        start = remap(c["t"])
        end = start + c.get("duration", 3.0)
        hero = c.get("style") == "hero"
        margin = 0 if hero else int(width * 0.05)
        pos = "top" if hero else (c.get("pos") or "bottom")
        y = {"top": "0" if hero else f"{int(height * 0.08)}",
             "center": "(H-overlay_h)/2",
             "bottom": f"H-overlay_h-{int(height * 0.06)}"}.get(pos, "H-overlay_h-100")
        nxt, fl = f"[v{i}]", f"[cf{i}]"
        filters.append(f"[{i+1}:v]format=rgba,"
                       f"fade=t=in:st=0:d={FADE}:alpha=1,"
                       f"fade=t=out:st={max(c.get('duration', 3.0) - FADE, 0):.2f}"
                       f":d={FADE}:alpha=1,setpts=PTS-STARTPTS+{start:.3f}/TB{fl}")
        filters.append(f"{chain}{fl}overlay=x={margin}:y={y}:"
                       f"enable='between(t,{start:.2f},{end:.2f})'{nxt}")
        chain = nxt

    cmd = ["ffmpeg", "-y", "-v", "error"] + inputs
    if filters:
        cmd += ["-filter_complex", ";".join(filters), "-map", chain]
    # screenrecord emits VARIABLE frame rate -- it only encodes frames when the
    # screen changes, so a mostly-static tour can land at ~1 fps and play like a
    # slideshow. Force CFR on the way out.
    if out_dur > 0:
        cmd += ["-t", f"{out_dur:.3f}"]
    cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-r", str(fps),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", final]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(final):
        die(f"ffmpeg failed: {(r.stderr or '').strip()[:400]}", 1)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,nb_frames:format=duration", "-of", "default=nw=1", final],
        capture_output=True, text=True).stdout.replace("\n", " ").strip()
    print(f"{final}")
    print(f"mp4 check: {probe}  cards: {len(cards)}")
    if srt and cards:
        spath = os.path.join(out, f"{name}.srt")
        write_srt(spath, cards, remap)
        print(f"{spath} ({len(cards)} captions)")

    if gif:
        gpath = os.path.join(out, f"{name}.gif")
        pal = os.path.join(out, "palette.png")
        vf = "fps=12,scale=480:-1:flags=lanczos"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", final, "-vf",
                        f"{vf},palettegen=max_colors=128", pal], check=False)
        rg = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", final, "-i", pal,
                             "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer",
                             gpath], capture_output=True, text=True)
        if rg.returncode == 0 and os.path.isfile(gpath):
            print(f"{gpath} ({os.path.getsize(gpath) // 1024} KB)")
        else:
            print(f"gif export failed: {(rg.stderr or '').strip()[:200]}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    """Offline checks. Needs Pillow but no device, no adb, no ffmpeg."""
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("SKIP card rendering (Pillow not installed)")
        print("RESULT: ok")
        return 0

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "c.png")
        cw, ch = render_card({"kicker": "Step 1", "title": "Open Settings",
                              "body": "Every preference lives behind the gear tab.",
                              "duration": 3, "style": "card"}, 1080,
                             THEMES["coral"], p)
        check("card png written", os.path.isfile(p) and os.path.getsize(p) > 0)
        check("card width is inset from the frame", cw == 1080 - 2 * int(1080 * 0.05))
        check("card height grows with content", ch > 100)
        check("card has alpha", Image.open(p).mode == "RGBA")

        _, h_short = render_card({"title": "Hi", "duration": 3}, 1080,
                                 THEMES["coral"], p)
        _, h_long = render_card({"title": "Hi", "body": " ".join(["word"] * 40),
                                 "duration": 3}, 1080, THEMES["coral"], p)
        check("more body text yields a taller card", h_long > h_short)

        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        f = load_font("regular", 40)
        lines = wrap(" ".join(["word"] * 30), f, 400, probe)
        check("wrap splits long text", len(lines) > 1)
        check("wrap keeps every word",
              " ".join(lines).split() == ["word"] * 30)
        check("wrap never drops an unbreakable word",
              wrap("supercalifragilistic", f, 10, probe) == ["supercalifragilistic"])
        ml = wrap_multi("one\n\ntwo", f, 400, probe)
        check("wrap_multi honours newlines and blank spacer",
              ml == ["one", "", "two"])
        check("slash kicker keeps its case", kicker_text("/ux-audit") == "/ux-audit")
        check("plain kicker is upper-cased", kicker_text("step") == "STEP")

        _, h_glass = render_card({"kicker": "/x", "title": "T",
                                  "body": "a\nb", "style": "card"}, 1080,
                                 THEMES["glass"], p)
        check("glass card renders with a border", os.path.getsize(p) > 0 and h_glass > 0)

    cards = [{"t": 3.0, "duration": 3.0}, {"t": 20.0, "duration": 3.0}]
    segs, remap = warp_plan(30.0, cards, gap_speed=4.0)
    check("warp protects every card window at 1.0x",
          all(sp == 1.0 for s, e, sp in segs
              if any(c["t"] >= s and c["t"] < e for c in cards)))
    check("warp compresses the card-free stretches",
          any(sp > 1.0 for _, _, sp in segs))
    check("warp shortens the timeline", remap(30.0) < 30.0)
    check("warp is monotonic", all(remap(x) <= remap(x + 0.5) for x in range(0, 29)))
    check("warp starts at zero", remap(0.0) == 0.0)
    check("warp keeps a card's own duration intact",
          abs((remap(6.0) - remap(3.0)) - 3.0) < 0.05)
    check("no cards means no segmentation", warp_plan(30.0, [])[0] == [])
    close = warp_plan(5.0, [{"t": 1.0, "duration": 1.0},
                            {"t": 3.5, "duration": 1.0}], min_gap=1.5)[0]
    check("a gap shorter than min_gap is left alone",
          all(sp == 1.0 for _, _, sp in close))
    check("srt timestamps format correctly",
          srt_time(3661.5) == "01:01:01,500" and srt_time(0) == "00:00:00,000")

    check("themes carry every colour key",
          all({"panel", "accent", "kicker", "title", "body"} <= set(t)
              for t in THEMES.values()))
    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


# ---------------------------------------------------------------- cli


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(
        prog="walkthrough",
        description="Record an Android walkthrough and burn in explanation cards.")
    p.add_argument("--serial")
    p.add_argument("--out", default="./walkthrough-out", help="session directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("start", help="begin recording")
    st.add_argument("--bitrate", default="8M")
    st.add_argument("--size", help="e.g. 720x1600 if native recording fails")
    st.add_argument("--theme", default="glass", choices=sorted(THEMES))
    st.add_argument("--no-show-touches", dest="show_touches", action="store_false",
                    help="do not render Android's touch indicators")
    st.add_argument("--force", action="store_true", help="overwrite an existing session")

    mk = sub.add_parser("mark", help="stamp an explanation card at the current moment")
    mk.add_argument("title")
    mk.add_argument("--body", default="")
    mk.add_argument("--kicker", default="")
    mk.add_argument("--duration", type=float, default=3.0)
    mk.add_argument("--style", default="card", choices=["card", "hero"])
    mk.add_argument("--pos", choices=["top", "center", "bottom"])

    sp = sub.add_parser("stop", help="end recording, pull, and render")
    sp.add_argument("--name", help="output basename (default: walkthrough)")
    sp.add_argument("--gif", action="store_true", help="also export a gif")
    sp.add_argument("--speed", type=float, default=1.0,
                    help="playback speed multiplier, e.g. 1.5 to tighten dead air")
    sp.add_argument("--no-tighten", dest="tighten", action="store_false",
                    help="keep the dead air between cards (default: compress it)")
    sp.add_argument("--gap-speed", type=float, default=4.0,
                    help="how hard to compress card-free stretches")
    sp.add_argument("--srt", action="store_true",
                    help="also write a sidecar .srt caption track")
    sp.add_argument("--fps", type=int, default=30,
                    help="constant output frame rate (screenrecord is VFR)")
    sp.add_argument("--settle", type=float, default=3.0)

    rd = sub.add_parser("render", help="re-render an existing session (edit cards first)")
    rd.add_argument("--name")
    rd.add_argument("--gif", action="store_true")
    rd.add_argument("--speed", type=float, default=1.0)
    rd.add_argument("--no-tighten", dest="tighten", action="store_false",
                    help="keep the dead air between cards (default: compress it)")
    rd.add_argument("--gap-speed", type=float, default=4.0,
                    help="how hard to compress card-free stretches")
    rd.add_argument("--srt", action="store_true",
                    help="also write a sidecar .srt caption track")
    rd.add_argument("--fps", type=int, default=30)

    a = p.parse_args(argv)
    handlers = {"start": cmd_start, "mark": cmd_mark, "stop": cmd_stop,
                "render": cmd_render}
    try:
        return handlers[a.cmd](a)
    except subprocess.TimeoutExpired:
        die("adb timed out — the device may be busy or disconnected.")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
