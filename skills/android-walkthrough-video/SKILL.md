---
name: android-walkthrough-video
license: MIT
description: Records a demo video of an Android app to MP4 or GIF with explanation cards burned in - starts screenrecord, stamps captioned beats while you drive the app, then composites them with ffmpeg. Use to record a demo, screen recording, tutorial video, product clip or captioned walkthrough of an Android app on a device or emulator. Not for single screenshots, UI assertions, browser recordings, or written reviews.
argument-hint: "<what flow to record>"
---

# android-walkthrough-video

Turns driving an app into a captioned MP4: `screenrecord` captures the screen,
you stamp an explanation card at each beat *while it happens*, and ffmpeg burns
the cards in afterwards. `scripts/walkthrough.py` runs the three phases.

**The constraint that shapes everything:** you cannot inject an overlay into a
third-party Android app the way a web recorder injects DOM into a page. So cards
are composited **post-hoc** against a timeline captured live — which is why
`mark` exists instead of a scenario file with guessed timings.

## When NOT to use

- A still screenshot, or tap/read/assert → `android-ui-driver` (this skill drives
  nothing; you drive with that one while recording).
- A written review with evidence → `android-app-review`.
- Getting the app onto the device → `play-store-app-install`.
- Recording a **web** app → that is a browser-recorder job, not adb.
- Generic video editing or format conversion → plain ffmpeg.

## Prerequisites

`adb`, plus **ffmpeg** and **Pillow** (`pip install pillow`) for rendering — both
only needed at render time. Cards are drawn as PNGs and composited with ffmpeg's
`overlay`, deliberately **not** `drawtext`: a stock `brew install ffmpeg` has no
freetype, so `drawtext` is simply absent on many machines. `overlay` is universal.

## Workflow

Run as `python3 "${CLAUDE_SKILL_DIR}/scripts/walkthrough.py" --out DIR <cmd>`.

1. **Plan the beats first.** 3–6 steps, each one sentence. Write the ending
   first: what the viewer should be able to do. Keep titles 3–8 words and bodies
   8–20 words — a card that needs two lines of body is two beats.

2. **Rehearse, then start.** Walk the flow once with `android-ui-driver` so you
   know the taps work and, ideally, their coordinates. Then:

   ```bash
   walkthrough.py --out ./demo start --theme glass
   ```

   `glass` (default) is a minimal frosted overlay: a translucent panel the app
   shows through, a hairline border, no accent stripe. `coral`/`indigo`/`paper`
   are solid-panel alternatives.

   Enables Android's touch indicators (`show_touches`) so taps are visible — the
   only "cursor" available — and records `t0` from when the file actually starts
   growing, not when the command returned.

3. **Drive and mark.** Use `android-ui-driver` for the actions. At each beat, in
   the moment:

   ```bash
   walkthrough.py --out ./demo mark "Browse by category" \
       --kicker "Step 2" --body "Each row is a category carousel." --duration 4
   ```

   `--style hero` dims the frame for an opening/closing title; `--pos
   top|center|bottom` moves a normal card off whatever it would cover. A `--body`
   may carry newlines to structure it into short rows (e.g. `● 2 dex · 4 ABIs`),
   and a kicker that starts with `/` (a skill name like `/android-ux-audit`) keeps
   its case instead of being upper-cased.

4. **Stop and render.**

   ```bash
   walkthrough.py --out ./demo stop --name app-tour --gif
   ```

   Sends SIGINT to `screenrecord`, waits for the MP4 to finalise, pulls it,
   restores `show_touches`, then composites every card at its recorded timestamp.

   Rendering **tightens the take by default**: stretches with no card on screen
   are compressed (4x), while every card window plus a beat either side stays at
   1.0x. A typical take loses 25–30% of its length — all of it hierarchy-dump
   dead air — without touching the pace of the actual interactions.
   `--no-tighten` keeps the raw timing, `--gap-speed N` tunes it, `--srt` also
   writes a sidecar caption track.

5. **Verify before claiming success.** Read the `mp4 check:` line, then look at a
   frame inside a card's window:

   ```bash
   ffmpeg -y -ss 15 -i demo/app-tour.mp4 -frames:v 1 /tmp/f.png
   ```

   Confirm the card is readable, inside the frame, and not covering the thing it
   describes.

6. **Iterate without re-recording.** Card text wrong? Edit `demo/session.json`
   and re-render — the footage and timings are already captured:

   ```bash
   walkthrough.py --out ./demo render --name app-tour
   ```

## Output spec

- `<out>/<name>.mp4` — H.264, CRF 20, yuv420p, faststart, constant frame rate.
- `<out>/<name>.gif` with `--gif` (two-pass palette, 480 px, 12 fps).
- `<out>/session.json` — the timeline; the editable source of truth for re-renders.
- `<out>/raw.mp4` and `<out>/cards/*.png` — intermediates, kept for iteration.
- A verification note: the probe line plus which frame you actually looked at.

## Gotchas

- **`screenrecord` is variable frame rate.** It only encodes frames when the
  screen changes, so a static stretch (an idle feed, an empty screen) has almost
  no frames — and `overlay` composites a card only onto frames that exist, so a
  card whose window lands on a static stretch would silently never appear. The
  renderer normalises the take to CFR (`--fps`, default 30) **before** compositing
  so every card window has frames; if playback still stutters, re-render — do not
  re-record.
- **Driving with `--text` inserts ~2 s of dead air per action**, because
  resolving a selector needs a hierarchy dump. Default tightening removes most of
  it automatically. For the tightest takes, collect coordinates with `snap`
  during the rehearsal and drive with `tap --at X,Y`. Prefer tightening over
  `--speed`: a global speed-up distorts the touch animations and screen
  transitions too, which is exactly what a viewer reads as "fake".
- **Cards are drawn over live footage** — there is no separate title slide. A
  `hero` dims the whole frame and centres its text, so it reads as a title card;
  a plain `card` is a corner panel that must not cover the control it describes.
  Still put a hero on a quiet moment: the footage keeps playing underneath.
- **The 3-minute default limit** is removed here (`--time-limit 0`), but long
  takes make big files and slow renders. Keep tours 30–90 s.
- **Rotating the device mid-recording ends the recording.** Set the orientation
  before `start`.
- **`FLAG_SECURE` screens record as black**, exactly as they screenshot black.
  There is no adb-only way around it — plan the route to avoid them.
- **Recording captures whatever is on screen**, including notifications and
  personal data. Use a disposable emulator; the script refuses to auto-select a
  physical device for this reason.
- **No audio.** `screenrecord` does not capture it; add narration in an editor.
- **A `stop` that finds no file** means `screenrecord` died early — usually an
  unsupported recording size. Retry with `start --size 720x1600`.

## Files

- `scripts/walkthrough.py` — start / mark / stop / render, plus `--self-test`
  (offline card-rendering checks; no device, adb or ffmpeg needed).
- `references/card-craft.md` — beat structure, card copy limits, theming, pacing,
  and the export budgets for README/socials.
