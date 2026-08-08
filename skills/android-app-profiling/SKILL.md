---
name: android-app-profiling
description: Profiles an installed Android app's runtime performance with no root - process-cold startup vs Android-vitals thresholds, frame jank and percentiles, memory (PSS), baseline-profile status, and Perfetto traces. Use when asked how fast an app starts, whether it's janky or slow, to measure frame rate or memory, or to capture a trace. Not for crash logs, a quick memory glance, or static APK analysis.
argument-hint: "<package name> [startup|frames|memory|dexopt|trace]"
---

# android-app-profiling

Black-box runtime performance profiling of an installed app — everything here
works on a **stock release APK, no root, no source, no debuggable/profileable
flag**. `scripts/profile.py` (standard library) wraps the no-root-safe half of
the Android profiling toolkit and benchmarks results against published
thresholds.

For a **quick** one-shot memory + jank number, `android-package-diagnostics perf`
is enough; this skill is the deep, benchmarked version (startup medians, jank
percentiles, Perfetto capture, baseline-profile status).

## When NOT to use

- Crashes / ANRs / a quick perf glance / permissions → `android-package-diagnostics`.
- What's inside the APK file (baseline.prof presence, size, native ABIs) →
  `android-apk-analysis` (static; this skill measures the *running* app).
- Network timing/hosts → `android-network-trace`. UI driving → `android-ui-driver`.
- Heap dumps, allocation tracking, Studio Profiler, simpleperf app profiling →
  need a profileable/debuggable app or root; out of scope (see references).

## The emulator caveat — read first

An emulator has **no real GPU, thermal, or power** behavior. These numbers are
good for finding regressions, gross problems, and relative comparisons, but they
are **not device-grade benchmarks**. Confirm anything that matters on a physical
device. Every command says so in its output.

## Workflow

Run as `python3 "${CLAUDE_SKILL_DIR}/scripts/profile.py" <cmd> <pkg>`.

- **Startup** — `am start -W`, median of N, classified against Android vitals
  (cold excessive ≥5 s). It measures PROCESS-COLD only (force-stopped),
  reports every sample + the count over the excessive line, and says "fast/
  acceptable/exceeds" rather than grading a median "good":

  ```bash
  profile.py startup com.example.app --runs 5   # process-cold: force-stops each run
  ```

- **Frames / jank** — reset, exercise, then read the janky % and frame-time
  percentiles (budget 16.6 ms @60 Hz; <1% janky is good):

  ```bash
  profile.py frames com.example.app --reset            # zero the counters
  #   ... scroll and navigate the app (android-ui-driver swipe) ...
  profile.py frames com.example.app                    # janky %, 50/90/95/99th
  ```

- **Memory** — PSS breakdown (Java/Native heap PSS; app must be running); re-run to watch for a leak:

  ```bash
  profile.py memory com.example.app
  ```

- **Baseline profile / compilation** — `speed-profile` means startup is optimized:

  ```bash
  profile.py dexopt com.example.app
  ```

- **Perfetto trace** — a full system trace, no root, for deep analysis:

  ```bash
  profile.py trace com.example.app --secs 15 --out startup.perfetto-trace
  #   exercise the app during the window; then open the file at ui.perfetto.dev
  ```

## Output spec

- Each metric with a plain **verdict** tied to a named threshold, not a vibe
  (startup: fast/acceptable/exceeds-excessive; frames: good/ok/poor).
- `--json` on startup/frames/memory/dexopt for machine use.
- The Perfetto trace as a `.perfetto-trace` file plus how to analyze it — this
  skill does **not** parse the protobuf; it hands it to the Perfetto UI /
  trace_processor.
- The emulator caveat on any timing/frame number.

## Gotchas

- **Emulator numbers aren't device numbers** (see above). Use them for
  regressions and gross problems; benchmark for real on hardware.
- **"Cold" here is process-cold, not first-install-cold.** The skill force-stops
  before each run, but page cache, app data, and compilation persist — so it's a
  TTID-like proxy (`am start -W TotalTime`), not TTFD or field Vitals. It reports
  all samples, not just the median (a low median can hide slow outliers).
- **Frame stats need frames, and they're HWUI stats.** A freshly launched app has
  rendered a handful; the janky % over 7 frames is noise (`frames --reset`,
  exercise, then read). They're HWUI render stats (may miss SurfaceView/Compose-
  to-Surface); the 16.6 ms budget assumes 60 Hz — on 90/120 Hz use a Perfetto
  FrameTimeline trace, not this.
- **`status=verify` only means "not currently profile-guided AOT compiled"** — it
  does NOT prove a profile is missing or queued (especially for sideloaded apps).
  `speed-profile` means profile-guided AOT is active, but the profile's source
  (shipped baseline / Play cloud / runtime) can't be told apart here. For a real
  baseline-profile signal, check `assets/dexopt/baseline.prof` in the APK with
  `android-apk-analysis`.
- **Perfetto is captured, not parsed here.** The trace is a protobuf; read it in
  `ui.perfetto.dev` (FrameTimeline shows per-frame jank attributed to the app vs
  SurfaceFlinger) or query it with `trace_processor_shell` — see references. A
  non-empty trace isn't proof of useful data (sources can be denied), and it
  embeds package/activity/thread strings — treat it as sensitive.
- **`am start -W` needs the right launcher activity.** The skill auto-resolves it;
  pass `--activity pkg/.Comp` if the app has an unusual entry point.
- **PSS vs RSS:** the memory numbers are PSS categories from `dumpsys meminfo`
  (Java/Native are PSS heap buckets, not allocated-heap or USS); PSS is the
  standard per-app RAM cost, RSS over-counts shared memory. Watch PSS growth
  across runs for a leak.
- **Deep profiling needs more than this** — heap dumps (`am dumpheap`),
  allocation tracking, and simpleperf app profiling require the app to ship
  `profileable`/`debuggable` or a rooted image. `references/deep-profiling.md`.

## Files

- `scripts/profile.py` — startup, frames, memory, dexopt, trace, `--self-test`
  (offline: parses/classifies `am start -W`, gfxinfo, meminfo, dexopt output — no
  device).
- `references/deep-profiling.md` — Perfetto trace analysis (trace_processor SQL),
  the profileable/debuggable/root matrix, and what each unlocks.
