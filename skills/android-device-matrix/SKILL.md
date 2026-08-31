---
name: android-device-matrix
license: MIT
description: Tests an Android app across screen sizes, densities, orientations, font scales and dark mode, and checks it degrades gracefully when a permission, radio or sensor is missing. Use for tablets, rotation and landscape, large text, small screens, running offline or on mobile data, or what happens with no camera or permission. Not for end-to-end reviews, heuristic usability scoring, or startup performance.
argument-hint: "<package to sweep>"
---

# android-device-matrix

Runs the same app through many device configurations and reports which ones
break. `scripts/devmatrix.py` applies each cell, **verifies it by reading the
configuration back off the device**, captures evidence, and restores everything
it touched.

Two questions in one sweep:

- **Layout** — does the UI hold at 320dp and at 840dp, in landscape, at 200%
  text, in dark mode, behind a cutout?
- **Capability absence** — the app declares features and permissions it can live
  without. Does it actually survive losing them?

**The fidelity limit, stated first:** `wm size`/`wm density` remap the logical
metrics of one emulator's panel. They cannot fake a display cutout you did not
enable, a real device's aspect ratio, a hinge, or a vendor skin. A pass here
means *"it fits this rectangle"*, not *"it fits that device"*. Cells that need a
real AVD are reported as recipes rather than silently faked.

## When NOT to use

- Touch-target sizes and unlabelled controls → `android-ux-audit` (this skill
  produces the per-cell snapshots that skill reads; it does not re-implement it).
- A full written review → `android-app-review`, which orchestrates this one.
- Startup, jank, memory → `android-app-profiling`.
- Reading the manifest for its own sake → `android-apk-analysis`.
- Web-page responsive breakpoints — this drives adb, not a browser.

## Prerequisites

`adb`, and `android-ui-driver` alongside it (snapshots come from its `uia.py`, so
every cell is readable by `android-ux-audit` without a second format). `aapt2`
(SDK build-tools) is optional but recommended: `dumpsys package` does **not**
report `uses-feature` on API 35, so without aapt2 the capability surface is
reported as unknown rather than as empty.

## Workflow

Run as `python3 "${CLAUDE_SKILL_DIR}/scripts/devmatrix.py" <cmd>`.

1. **See the cells before touching the device.**

   ```bash
   devmatrix.py profiles                 # quick · thorough · a11y · capability
   devmatrix.py plan --profile thorough  # dp arithmetic per cell, mutates nothing
   ```

2. **Ask what this app can actually lose.** The matrix should be app-specific:
   a feature declared `required="false"` is a promise the app still works
   without it, and that promise is what the capability cells test.

   ```bash
   devmatrix.py capabilities com.example.app
   ```

   It splits the surface three ways: testable now at runtime, needs a
   purpose-built AVD (printed as `config.ini` lines), and already absent.

3. **Sweep.**

   ```bash
   devmatrix.py run com.example.app --profile quick --out ./matrix
   ```

   Per cell: apply → cold-start the app → wait for it to reach the foreground →
   rotate if asked → verify → snapshot + screenshot → detect → undo. Exits
   non-zero if any cell had a hard failure.

4. **Audit any cell you care about** — the snapshots are ordinary
   `android-ui-driver` snapshots:

   ```bash
   uxcheck.py audit ./matrix/cells/compact-sw320.json --density 360
   ```

   Target-size findings are density-dependent, so a control that passes at
   420dpi can fail at 360dpi. That is the point of running the audit per cell.

5. **If a run is interrupted**, the device is not left wrecked:

   ```bash
   devmatrix.py doctor    # what is non-default right now
   devmatrix.py restore   # put back exactly what was saved
   ```

## Configuring the matrix

Profiles are plain JSON. Start from a built-in one and edit:

```bash
devmatrix.py profiles --dump thorough > my-matrix.json
devmatrix.py run com.example.app --matrix my-matrix.json
```

A cell is a dict; every key is optional:

```json
{"id": "tablet-portrait", "size": "1440x2560", "density": 320,
 "rotation": 0, "font_scale": 1.0, "night": "yes", "cutout": "tall",
 "deny": "permission:CAMERA", "note": "why this cell exists"}
```

`size` is **pixels** and `density` is **dpi** because that is what `wm` accepts;
the dp the device actually entered is measured and reported. The arithmetic is
`dp = px × 160 ÷ dpi`, so 720px at 360dpi is 320dp. Width dp picks the
WindowSizeClass: compact <600, medium 600–839, expanded 840–1199, large
1200–1599, extra-large ≥1600.

`deny` accepts `airplane`, `wifi`, `data`, `location`,
`permission:<NAME>` and `appops:<OP>`.

## What counts as a failure

Only mechanically unambiguous things gate the run:

| Hard (fails, exit 1) | How it is detected |
| --- | --- |
| Crash | `FATAL EXCEPTION` naming the package in the `crash` buffer |
| ANR | `am_anr` in the **events** buffer (ANRs are not in the crash buffer) |
| Native crash | `dumpsys dropbox data_app_native_crash` (never a FATAL EXCEPTION) |
| Process death | the pid is gone and does not come back |
| Never foregrounded | the app never owns the top resumed activity |

Everything from the accessibility tree is **suspected** and never gates: blank
screen, no actionable control, off-screen or zero-area controls, a refused
rotation, a process restart. A system prompt taking the foreground
(`permissioncontroller`, the GMS "location is off" warning) is suspected too —
that is the platform interposing, not the app breaking, and in a capability cell
it is often the correct behaviour.

**Text truncation is deliberately not detected.** The accessibility tree carries
the full string even when the view ellipsises it, so any check would be a guess.
Look at the `largest-font` and `worst-case` screenshots instead.

## Output spec

- `<out>/report.json` — the machine-readable contract other skills read:
  per-cell `requested` vs `verified` (`widthDp`, `heightDp`, `smallestWidthDp`,
  `sizeClass`, `rotation`), `status`, `findings[]` with `severity` +
  `evidenceType`, and `artifacts` paths.
- `<out>/cells/<id>.json` — an `android-ui-driver` snapshot per cell.
- `<out>/cells/<id>.png` — a screenshot per cell.
- A verdict line, plus the restore log of every key put back.

## Gotchas

- **Rotation must be applied after the app is running.** An orientation-locked
  activity rewrites `user_rotation` back to its own orientation as it starts, so
  a rotation set before launch silently reads back as portrait. The script
  rotates post-launch and reports `orientation_locked` when the app refuses,
  rather than labelling a portrait result "landscape".
- **`appops set … default` is not a restore.** An op whose original mode is
  `foreground` is left broken by `default`; the exact prior mode string is
  recorded and written back, to both the uid and package mode.
- **`wm size reset` restores the AVD's panel, not the previous cell.** If a size
  override already existed before the run, it is re-applied rather than reset.
- **A single focus read is not evidence.** During a resize the focused window is
  briefly empty or owned by another process; the script polls for the app to
  reach the top resumed activity before concluding anything.
- **`wm size` returns before the display reconfigures.** Reading `dumpsys`
  straight after can hand back the *previous* cell's geometry — an 11-cell sweep
  came back shifted by one cell, every bucket mislabelled. Each cell now polls
  until the requested geometry is observed, and a cell that never applies is
  reported as `config_not_applied` instead of passing under a bucket it never
  entered.
- **Read the dp bucket from `mGlobalConfiguration`.** A bare search for `swNNNdp`
  in `dumpsys window` matches a stale `mFullConfiguration` belonging to a window
  that has not been reconfigured, so every cell reports the *native* bucket.
- **Orientation is judged by geometry, not `mRotation`.** That field also appears
  more than once, and the first match can belong to another display.
- **Enabling a cutout overlay restarts SystemUI**, which can tear the app off the
  foreground a moment after launch and look like a crash. The cutout cell waits
  for that to settle before starting the app.
- **`dumpsys package` does not list `uses-feature`** on API 35 — verified across
  several packages. The manifest is read with aapt2 from the pulled APK; without
  aapt2 the feature list is reported UNKNOWN, never as "none declared".
- **Landscape on a phone is often the expanded width class** (a 411dp-wide phone
  becomes 914dp wide), so the landscape cell exercises tablet layout paths too.
- **Cells are undone between cells**, so a failure in one does not contaminate
  the next. The full restore runs again at the end, and on Ctrl-C.
- **You cannot remove a hardware feature at runtime.** `pm list features` comes
  from the system image and AVD config. Anything needing that is emitted as a
  `config.ini` recipe by `capabilities`, not faked.

## Files

- `scripts/devmatrix.py` — profiles / plan / capabilities / run / restore /
  doctor, plus `--self-test` (offline; no device needed).
- `references/matrix-design.md` — why these breakpoints, the full runtime-vs-AVD
  capability split, AVD recipes, and the detector false-positive rationale.
