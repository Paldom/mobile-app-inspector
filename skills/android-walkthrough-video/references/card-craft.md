# Card craft and export budgets

What separates a walkthrough that reads as polished from one that reads as
frantic. The recorder enforces none of this — it is judgment, and it lives here
so the skill body stays short.

**Contents:** [Beat structure](#beat-structure) · [Card copy](#card-copy) ·
[Placement](#placement) · [Themes](#themes) · [Pacing](#pacing) ·
[Export budgets](#export-budgets) · [session.json](#sessionjson)

## Beat structure

One narrative level per video. Mixing "here is the whole app" with "here is how
to do one task" is what makes tours ramble.

| Beat | Style | Duration | Purpose |
| --- | --- | --- | --- |
| Hook | `hero` | 2.0–2.5 s | Name the payoff. "Add an alarm in three taps." Dims the frame and centres the text. |
| Step 1..N | `card` | 3–4 s | One action, one card. 3–6 of these, no more. |
| Result | `card` or `hero` | 2.5–3 s | Show the end state that proves it worked. |

Show the payoff early — a viewer who does not know where this is going by second
three has already left. Write the closing card first; it forces the tour to have
a destination.

## Card copy

| Element | Limit | Notes |
| --- | --- | --- |
| `--kicker` | 1–3 words | "Step 2", "Result". Rendered uppercase. Optional. |
| `--title` | 3–8 words | The action, in the user's words. Not "Tap the FAB". |
| `--body` | 8–20 words, one sentence | The *why*, not a restatement of the title. Omit it rather than pad it. |

If the body needs two sentences, it is two beats. Cards wrap automatically and
grow taller, so over-long copy silently eats the screen instead of erroring.

Write titles as outcomes ("Browse by category"), not mechanics ("Tap Categories
tab"). The footage already shows the mechanics.

## Placement

`--pos bottom` (default for `card`) sits above the gesture bar; `--pos top`
clears a bottom nav bar; `--pos center` floats it mid-screen. `hero` ignores
`--pos` — it covers the whole frame with a scrim and centres its text.

The rule: a card must never cover the thing it describes. If the action is at
the bottom of the screen, put the card at the top. Check with a frame extract —
this is the single most common defect and it is invisible until you look.

## Themes

`start --theme coral|indigo|paper`.

- `coral` — dark panel, warm accent. Default; works over most light app UIs.
- `indigo` — dark panel, violet accent. Good for brand-neutral product clips.
- `paper` — light panel, near-black text. Use over **dark** apps; a dark card on
  a dark app disappears.

Panels are drawn at ~95% opacity so a little of the app shows through, which
keeps the card feeling attached to the screen rather than pasted on. Contrast of
title on panel is ≥ 4.5:1 in all three presets — if you hand-edit colours in
`session.json`, keep it there.

## Pacing

- A card needs ~0.35 s of reading time per word. A 15-word body wants 4 s, not 2.
- Leave ~1 s of quiet footage after an action before the next card appears, so
  the viewer sees the result before being told about it.
- Total 30–90 s for a demo or README clip. 3–7 minutes only for training
  material, and then chapter it.
- Dead air from `--text` selector resolution (~2 s per action) is the usual
  reason a tour drags. Rendering compresses it automatically: card windows (plus
  0.6 s either side) stay at 1.0x, everything else runs at `--gap-speed` (4x).
- **Do not reach for `--speed` first.** A global multiplier speeds the touch
  animations and screen transitions along with the dead air, which is what makes
  a demo read as fake. Tightening only touches the stretches where nothing is
  being explained. Use `--speed` on top only if the whole take is still slow.
- Cards fade in and out over 0.25 s so they do not pop.

## Planning without clocks

Plan the *copy* ahead of time, never the timings. Write an untimed beat sheet —
intent, kicker/title/body, and the tap target for each step — and let `mark`
stamp the real clock as you go. Android gives you no control over app launch
latency, animation length or network jitter, so any pre-written timing is
fiction that drifts a little differently on every take. `session.json` is the
scenario, authored by execution.

If cards consistently land slightly late or early, calibrate once:
`adb shell screenrecord --bugreport` burns a frame-timestamp overlay into the
video, so you can measure the true offset between `t0` and the first frame.

## Export budgets

| Destination | Target | How |
| --- | --- | --- |
| GitHub README gif | < 5 MB, ideally < 2 MB | `--gif` (480 px, 12 fps, 128 colours). Over budget? Record a shorter take — trimming beats one long gif. |
| Slack / Discord | < 8 MB mp4 | default CRF 20 is usually fine at 30–60 s |
| Product page / socials | mp4, 1080 wide | keep native resolution; do not upscale |
| Docs site | mp4 + poster frame | extract a poster with `ffmpeg -ss <t> -frames:v 1` |

A phone-shaped 1080×2400 video is very tall for a README. Either accept the
letterboxing, or record with `start --size 720x1600` for a smaller file.

## session.json

The editable record of a take:

```json
{ "serial": "emulator-5554", "t0": 1754150000.12,
  "width": 1080, "height": 2400, "theme": "coral",
  "cards": [ { "t": 3.34, "kicker": "Step 1", "title": "F-Droid opens on Latest",
               "body": "The catalogue lands on recently updated apps.",
               "duration": 4.0, "style": "card", "pos": null } ] }
```

Edit `title`, `body`, `kicker`, `duration`, `style`, `pos`, or nudge `t`, then
`render` again. Everything except `t0`/`serial` is safe to change. This is the
loop to use for copy edits — re-recording to fix a typo throws away a good take.
