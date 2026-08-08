# Changelog

All notable changes to this repository's skills are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org) on the plugin manifest
(breaking skill-interface change → major, new skill → minor, fix → patch).

## [Unreleased]

### Changed
- `android-walkthrough-video` — new default `glass` theme: a minimal frosted
  overlay (translucent panel the app shows through, hairline border, no accent
  stripe) instead of the coral accent bar. Card bodies now honour newlines so a
  card can be structured into short rows, and a `/skill-name` kicker keeps its
  case. The README worked-example clip was re-recorded with this look, one
  `/slash`-named card per skill for all ten.

### Fixed
- `android-walkthrough-video` — cards that landed on a **static** stretch of the
  recording silently never appeared: `screenrecord` is variable-frame-rate, so an
  idle screen emits almost no frames and `overlay` had nothing to composite onto
  (forcing CFR on output happened after overlay, too late). The renderer now
  normalises the take to CFR *before* compositing, so every card window has
  frames. Found while re-recording the ten-skill walkthrough.
- `android-network-trace` — `proxy clear` now restores/removes the split
  `global_http_proxy_host`/`_port`/`_exclusion_list` keys, not just the composite
  `http_proxy`. Clearing only the composite left the split keys routing traffic (a
  proxy split-brain that broke an app after a capture — reproduced live). `proxy
  set` stashes and clears the split keys too, so set/clear are symmetric. Covered
  by a fake-device self-test.

### Added
- README **Worked example**: all ten skills run against the real Wikipedia app on
  a stock Play emulator, with a self-recorded captioned walkthrough
  (`docs/assets/wiki-walkthrough.gif`), a real-results table, and the two findings
  the run surfaced (the F-Droid re-sign, two unlabelled drawer buttons).

### Security
- All three CLIs quote every argument passed to `adb shell`. `adb shell` joins its
  arguments and runs the result through the *device's* shell, so a `;` in a package
  name or typed string executed on the device — reproduced with a canary before the
  fix, confirmed dead after. Package names are also validated against a strict
  pattern.

### Added
- `android-ux-audit` — `scripts/uxcheck.py`, the UX/UI heuristic-audit step. Owns
  the deterministic checks from an android-ui-driver snapshot — actionable
  controls with no accessible label (screen-reader-invisible; the a11y tree is
  what TalkBack sees), and touch targets below the 48dp Android target in dp (from
  reported bounds, with a TouchDelegate caveat; no raw-pixel WCAG floor — WCAG
  2.5.8's 24px is CSS px, not device px) — each a SUSPECTED signal with a
  provisional priority, bounds evidence, and an acceptance-criterion fix. The
  12-category rubric (first-run, IA, forms, states, dark patterns, contrast,
  cross-platform parity), the release-gate verdict (worst gate wins, never a
  laundered average), and the finding register live in references/assets. Does not
  compute contrast (false precision on a black-box app — judged by eye). Composes
  as the UX step inside android-app-review.
- `android-app-profiling` — `scripts/profile.py`, black-box runtime performance
  profiling with no root/source/profileable flag: `am start -W` cold/hot startup
  (median of N, force-stopped for cold) classified against Android-vitals
  thresholds; `dumpsys gfxinfo` jank rate + frame-time percentiles (reset →
  exercise → report, warns on too-few-frames); `dumpsys meminfo` PSS/USS;
  `dumpsys package dexopt` baseline-profile/compilation status; and a no-root
  Perfetto system-trace capture handed to the Perfetto UI / trace_processor (not
  parsed inline). Every timing carries the emulator-is-not-device caveat.
  `references/deep-profiling.md` maps the profileable/debuggable/root escalations.
- `android-apk-analysis` — `scripts/apkscan.py`, static analysis of an APK/AAB
  *file* (no device). A stdlib inventory core it fully owns — composition (dex,
  native ABIs, splits via zipfile), manifest/permissions (aapt2), exported-
  component + cleartext/backup/debuggable posture (aapt2 xmltree), signer cert
  SHA-256 + schemes (apksigner), and a heuristic packer check — plus detect-and-
  degrade orchestration of jadx/apkid/apkleaks/exodus that runs each only when
  installed and marks skipped layers `status: skipped` with the install command.
  Normalizes AAB/.apks to a universal APK via bundletool. Correctness guards:
  signer is `not_applicable` for AAB input (bundletool re-signs with a debug key),
  no global verdict field, and an `analysisCaveats` list so a skipped layer is
  never read as a clean result. MobSF/Ghidra/diffuse documented as escalations.
- `play-store-listing` — `scripts/listing.py`, public Play market data (rating,
  ratings count, star histogram, install range, updated date, what's-new,
  paginated reviews) for any app by link/package. Deliberately wraps the
  maintained `google-play-scraper` (pinned, lazy-imported) instead of
  reimplementing its private-endpoint scraping; fails LOUD on Play layout drift
  (empty required fields) rather than reporting nulls; records lang/country and
  retries one alternate country; throttles reviews to dodge the 503/CAPTCHA IP
  ban. Notes that App-Bundle listings hide the exact version (use
  android-package-diagnostics for that).
- `android-network-trace` — `scripts/nettrace.py`, sees what an app talks to on a
  NON-rootable Play image: device-wide pcap via `adb emu network capture` (no
  relaunch, no root, no CA) with a stdlib parser extracting DNS names, TLS SNI and
  IP peers as separate evidence (never a false "complete" list); static http(s)
  URL extraction from a pulled APK; a safe mitmproxy proxy lifecycle (saves and
  restores the prior setting) + a metadata-only JSONL addon for HTTP(S) bodies
  where the app trusts a user CA. Discloses the device-wide/attribution and
  no-decrypt limits; `references/deeper-capture.md` documents the APK-repackage,
  PCAPdroid, and rooted+Frida escalations.
- `android-walkthrough-video` — `scripts/walkthrough.py`, a start/mark/stop/render
  recorder that captures the screen with `screenrecord`, stamps explanation cards
  at live-measured timestamps while you drive, and composites them with ffmpeg
  `overlay`. Cards are Pillow-rendered PNGs, not `drawtext`, because a stock
  `brew install ffmpeg` ships without freetype. Forces constant frame rate
  (`screenrecord` is VFR and a static tour otherwise plays at ~1 fps), restores
  `show_touches`, and supports re-rendering edited copy without re-recording.
  Rendering tightens the take by default — card windows hold at 1.0x while
  card-free stretches compress 4x, removing the hierarchy-dump dead air without
  the animation-distorting side effect of a global speed-up — fades cards in and
  out, and can emit a sidecar `.srt` caption track.
- `docs/authorized-use.md` — the authorization / dual-use policy for driving
  third-party apps, linked from the README, the install skill, and the setup prompt.
- Offline `--self-test` in all three CLIs (hierarchy compaction, Play-link parsing,
  `dumpsys` parsing) with a `make selftest` target wired into `make check` and CI.
  Needs no device or adb, so the pure logic is gated on every PR.
- `playapp.py preflight` now emits machine-readable `STATUS <component>: OK|WARN|
  MISSING|FAIL` lines and a final `RESULT: ok|fail`, and takes `--fix` to switch off
  the UI animation scales that make `uiautomator dump` fail.
- `play-store-app-install` — resolves a Play URL / `market://` URI / package name
  to a package, preflights the device (Play image present, Google account signed
  in), reports install state via `pm path`, sideloads single or split APKs with
  plain-language `INSTALL_FAILED_*` explanations, waits for an install to land,
  and launches the resolved launcher activity. Bundles `scripts/playapp.py` and an
  emulator-setup reference.
- `android-ui-driver` — `scripts/uia.py`, a stdlib-only compact-snapshot driver:
  numbered visible/interactable elements with tap points (~25× smaller than the
  raw hierarchy XML), semantic tap/type/swipe/key targeting, polling waits, and
  assertions that exit non-zero. Refuses to auto-select a physical device, scopes
  indices to one snapshot, discloses truncation, and rejects non-ASCII input up
  front. References cover escalation (uiautomator2 / Appium / Maestro) and
  per-framework selector behaviour.
- `android-package-diagnostics` — `scripts/pkgdiag.py`, distilling `dumpsys
  package` (700+ lines) plus logcat, meminfo and gfxinfo into a short report:
  version, signer, install provenance, requested vs granted permissions with
  device-sourced dangerous classification, APK paths and SHA-256, crashes/ANRs,
  memory and frame jank. Documents what is impossible without root.
- `android-app-review` — orchestrates the other three into a bounded, evidence-
  backed review, with a safety policy (untrusted on-screen text, no
  destructive/financial taps, no real credentials, stay in-package) and a report
  template.
- `docs/setup-prompt.md` — paste-ready `/goal` running install → drive → diagnose
  → report with per-phase verifier gates.
