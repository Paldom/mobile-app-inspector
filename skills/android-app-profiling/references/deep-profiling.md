# Deeper profiling: what needs profileable, debuggable, or root

The skill's `profile.py` covers everything you get on a **stock release app on a
stock device** — no root, no source, no manifest flags. This page is the map of
what lies beyond that line and what it costs, plus how to actually read a Perfetto
trace.

**Contents:** [The capability matrix](#the-capability-matrix) ·
[Reading a Perfetto trace](#reading-a-perfetto-trace) · [Startup depth](#startup-depth) ·
[Making an app profileable](#making-an-app-profileable) · [Thresholds](#thresholds)

## The capability matrix

| Want | Works no-root on release? | What it needs |
| --- | --- | --- |
| Startup time (`am start -W`) | **yes** | nothing |
| Frame jank (`gfxinfo`) | **yes** | nothing |
| Memory PSS/USS (`meminfo`/`procstats`) | **yes** | nothing |
| Compilation status (`dumpsys package dexopt`) | **yes** | nothing |
| Perfetto **system** trace | **yes** | nothing (captured by the skill) |
| Battery (`batterystats` → Battery Historian) | **yes** | reset + unplug for meaningful data |
| Per-uid network bytes (`dumpsys netstats`) | **yes** | nothing |
| Studio Profiler "low overhead" | no | app ships `<profileable android:shell="true"/>` (API 29+) |
| Java heap dump (`am dumpheap`) | no | debuggable app **or** root |
| Allocation tracking / Studio "complete data" | no | debuggable app |
| Native heap (heapprofd / malloc_debug) | no | profileable/debuggable or root |
| simpleperf **app** profiling | no | profileable/debuggable (system-wide needs root) |
| Controlled recompile (`cmd package compile`) | no | root |

Check the target's flag early: `android-apk-analysis report <apk>` shows
`debuggable` in its posture; grep the decoded manifest for `profileable`. Many
first-party Google apps ship `profileable=true`; a random third-party app usually
does not, and you can't add the flag without repackaging (which breaks the
signature and Play Integrity — see `android-network-trace`'s deeper-capture ref).

## Reading a Perfetto trace

`profile.py trace` captures the `.perfetto-trace`; analysis is separate because
the trace is a protobuf, not something to parse by hand.

- **Perfetto UI** — open <https://ui.perfetto.dev> and load the file (local, no
  upload to a server). The tracks that matter:
  - **FrameTimeline** — Expected vs Actual frame timeline; janky frames are red,
    and SurfaceFlinger's classification tells you whether the **app** or the
    **compositor** caused each miss.
  - The app's **main thread** — `Choreographer#doFrame`, `activityResume`,
    inflate/measure/layout slices; long slices here are your jank source.
  - **binder** transactions (IPC latency) and **sched** states (lock contention,
    blocked-on-I/O), and **CPU frequency**.
- **trace_processor_shell** (SQL, scriptable) — from the Perfetto releases:
  ```bash
  trace_processor_shell startup.perfetto-trace
  # e.g. slowest slices on the main thread:
  > SELECT name, dur FROM slice ORDER BY dur DESC LIMIT 20;
  # janky frames from the frame timeline:
  > SELECT * FROM actual_frame_timeline_slice WHERE jank_type != 'None';
  ```
  It also powers `--run-metrics android_startup` for a structured startup report.

Capture with the right categories for the question: `sched freq idle am wm gfx
view binder_driver` (the skill's default) covers jank + startup; add `dalvik` for
GC pauses, `disk` for I/O-bound startup.

## Startup depth

- **cold / warm / hot** are different questions: cold = process created from
  scratch (force-stop first — the skill does), warm = process alive/activity
  recreated, hot = brought to foreground. Report which you measured.
- **`am start -W`** gives `TotalTime` (up to first frame). **Time to full
  display** is later: an app that calls `reportFullyDrawn()` emits a second
  `Displayed … +Xms (total)` in logcat and a marker in the trace — that's the
  honest "usable" time for a content app.
- **Macrobenchmark** can target a *third-party* package (`measureRepeated`
  drives it via UI Automator in a separate process) with COLD/WARM/HOT modes —
  but only if the app ships `profileable=true`. Otherwise `am start -W` is the
  tool.

## Making an app profileable

Only when authorized and the question demands heap/allocation data. Repackaging
adds `<profileable android:shell="true"/>` (or `android:debuggable="true"`) but
**breaks the publisher signature, Play Integrity, and license checks**, may trip
anti-tamper, and requires re-signing every split with one key. Treat it as a last
resort; the network skill's `references/deeper-capture.md` covers the repackage
mechanics and hazards.

## Thresholds

From Android vitals (the "excessive" boundaries) and rendering budgets — the
verdicts `profile.py` prints come from these:

| Metric | Good | Excessive |
| --- | --- | --- |
| Cold startup | < ~1.5 s | ≥ 5 s |
| Warm startup | < ~1 s | ≥ 2 s |
| Hot startup | < ~0.8 s | ≥ 1.5 s |
| Janky frames | < 1% | (watch ≥ 5%, and the 95/99th percentile) |
| Frame time | ≤ 16.6 ms @60 Hz, ≤ 8.3 ms @120 Hz | over budget = a dropped frame |
| Baseline profile | `speed-profile` compiled | absent / stuck at `verify` |

These are general guidance, not hard standards — acceptable numbers depend on
device tier and app category, and (again) an emulator is not a device.
