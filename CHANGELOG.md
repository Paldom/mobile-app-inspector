# Changelog

All notable changes to this repository's skills are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org) on the plugin manifest
(breaking skill-interface change → major, new skill → minor, fix → patch).

## [0.3.0] - 2026-08-31

### Added
- Adopted the current skillskit gate: executed trigger evals scoring every trigger
  prompt against every skill description (rank-1 routing accuracy 81.1%, up from
  65.6%), a security scan, ruff lint and format, README-shape validation.

### Changed
- Skill descriptions sharpened across the catalogue. With twelve same-domain
  `android-*` skills the shared vocabulary made siblings outrank each other on
  their own trigger prompts; each fix moved the scope boundary rather than
  stuffing keywords.

### Fixed
- `android-app-review/scripts/report.py` used Python 3.12-only f-string syntax
  (PEP 701 multi-line replacement fields) while the repo supports 3.10 - it would
  have failed to parse on 3.10 and 3.11. Literals hoisted out of the f-strings.
- Unclosed file handles, ambiguous `l` identifiers and other findings the new
  lint gate surfaced in this repo's scripts.

## [0.2.0] - 2026-08-11

### Added
- `android-intent-probe` — `scripts/intentprobe.py`, the dynamic half of the
  exported-component question. `android-apk-analysis` says what the manifest
  declares; this fires each exported activity, receiver, service, provider and
  deep link at the running app and records **launched / accepted / denied /
  not-found / crashed**, turning the highest-volume static finding class from a
  candidate into a reproduced observation or a dismissal. Safety is the design:
  `plan` is the default and sends nothing, `run` refuses without `--go`,
  destructive-sounding component names (delete/purchase/logout/wipe…) are skipped
  and reported **not-assessed** rather than passed, the payload is one inert
  token, and the crash buffer is cleared per probe so a crash is attributed to the
  probe that caused it. Honours declared `pathPrefix` filters and rewrites wildcard
  hosts, because a probe that fails a filter would read as "unreachable" and say
  nothing about the app. Reaching a component is never itself reported as a
  vulnerability.
- `android-apk-analysis` — **Play release-readiness gates** (`playReadiness`):
  targetSdk against the API 35 floor, 64-bit ABI presence, 16 KB ELF page
  alignment read straight from the program headers with stdlib `struct`, signing
  scheme, `debuggable`, and the debug certificate. Deterministic pass/fail with no
  score, and it suppresses the known Janus false positive on v1+v2/v3 builds. Also
  a new `exportedInventory` naming each component with its permission, provider
  authority, URI schemes, hosts and path filters — the input `android-intent-probe`
  consumes.
- `android-app-review` report generator — **coverage and confidence ceiling**
  section (static/dynamic status, packer, whether HTTPS was decrypted, and the
  emulator ceiling that caps findings at medium confidence), a **Play release
  readiness** section rendered ahead of the security triage, an **entry points**
  section from the intent probe, per-finding **confidence / status / evidence
  strength / standards mapping / false-positive-if / retest** fields kept separate
  from severity, a secret-redaction helper, and a **machine-readable twin**
  (`<out>.json`, plus `<out>.sarif` with `--sarif`) so CI reads the same run as
  the human.

- `android-app-review` — `scripts/report.py`, a consolidated **HTML report**. It
  discovers each skill's `--json` in one run directory by filename and renders a
  single self-contained file (inline CSS, no CDN, no webfont, no JavaScript, print
  stylesheet) that you can attach to an email. Two halves, kept apart on purpose:
  MEASURED tool output, auto-discovered, and JUDGED coverage/findings/limits from
  `review.json` (`report.py init` scaffolds it, `sources` prints the contract).
  A skill that did not run gets a **not run** row naming the command that would
  fill it, rather than being silently omitted — a gap you cannot see reads as a
  clean bill of health. Emits **no overall score**: severity counts and the worst
  unresolved finding, never an average. Every caveat the tools raised (skipped APK
  layers, truncated capture, no idle baseline, emulator timings, suspected UX
  proxies) is aggregated into one closing section. All app-supplied text — store
  copy, review text, hostnames, crash lines — is HTML-escaped, so a listing
  containing `<script>` renders as text. `assets/report-template.html` is the
  shell (edit its CSS to rebrand, or pass `--template`);
  `assets/review.example.json` is a filled judgment half from a real run.
- `docs/full-review-prompt.md` — paste-ready `/goal` running every skill in
  dependency order into one run directory and ending with the HTML report, with a
  verifier gate per phase and the fixed filenames the generator expects.
- README worked example gains the rendered report
  (`docs/assets/wikipedia-review-report.html`) from an unedited Wikipedia run.

### Changed
- `android-app-review` report — de-bloated. One-to-two-word section titles
  (`Device matrix`, not "Other screen sizes and missing capabilities"), leads cut
  to a single clause, and the duplicated ones deleted. Findings are now one line
  each with the record behind a disclosure triangle, and the coloured left border
  is gone. Dense tables became visuals: a severity bar, dot rows for skills,
  release gates, matrix cells and intent probes, and horizontal bars for the star
  histogram. Long key/value tables, user reviews and the crash buffer collapse by
  default, so only 30% of the page's words face you before you expand anything.
  Counts read `4 screens`, not `4 screen(s)`.
- `android-app-review` report — rebuilt around **breadth-first reading**. A sticky
  table of contents, an *At a glance* index that states every topic in one line,
  and per-section summaries that lead before any table; depth stays behind
  disclosure triangles. Every summary is computed from the artifacts, never
  written by hand. Visually it is a minimal card layout — neutral palette, HSL
  design tokens, light/dark, print stylesheet — still one self-contained file with
  no CDN, no webfont and no JavaScript. Default output is now `index.html`, and
  the worked example lives at `docs/assets/example-report/index.html` beside its
  `.json` and `.sarif` twins.

### Fixed
- `android-device-matrix` — the per-cell log anchor cleared only the `crash`
  buffer, but ANRs are read from `events`. One real ANR therefore re-fired in
  every later cell, turning a single event into N hard failures and failing an
  otherwise-clean matrix. Both buffers are cleared per cell now. Found while
  running the matrix for the report worked example.

### Added
- `android-device-matrix` — `scripts/devmatrix.py`, the configuration-sweep step.
  Runs an app across a configurable matrix of screen sizes, densities,
  orientations, font scales, dark mode and display cutouts, and across
  capability-denial cells (permission revoke, appops deny, airplane/wifi/data,
  location off). Four built-in profiles (quick/thorough/a11y/capability) plus
  `--matrix my.json`; `profiles --dump` emits one to edit. Every cell is applied
  and then **verified by reading the configuration back off the device**, so the
  report states the dp bucket and WindowSizeClass the device actually entered
  rather than the arithmetic it intended. Gates only on mechanically unambiguous
  failures — `FATAL EXCEPTION`, `am_anr` (the events buffer, where ANRs actually
  are), dropbox native crashes, process death, never reaching the foreground —
  while accessibility-tree signals stay suspected. `capabilities` derives the
  app-specific surface from the manifest and splits it into runtime-testable,
  needs-a-different-AVD (with `config.ini` recipes), and already-absent. Emits an
  `android-ui-driver` snapshot per cell so `android-ux-audit` can audit any
  configuration without a second format, and `restore`/`doctor` put a
  crashed run's device back.

  Five defects were found by running it against a real app and fixed before it
  shipped: `wm size` returns before the display reconfigures, so an immediate
  read returned the *previous* cell's geometry and an eleven-cell sweep came back
  shifted by one cell (now polled, with a `config_not_applied` finding when a
  cell never takes); `swNNNdp` in `dumpsys window` first matches a stale
  `mFullConfiguration`, so every cell reported the native bucket (now read from
  `mGlobalConfiguration`, which also supplies the framework's own dp values);
  `mRotation` is ambiguous across displays, so orientation is judged by
  `widthDp > heightDp`; a rotation applied before launch is silently reverted by
  an orientation-locked activity (now applied post-launch and reported as
  `orientation_locked` when refused); and enabling a cutout overlay restarts
  SystemUI, which pulled the app off the foreground and looked like a crash.

### Fixed
- `android-ui-driver` and `android-walkthrough-video` read the display's
  **override** size when one is set, not the physical panel size. Under a
  `wm size` override (or rotation) they reported the native resolution, which
  made every bounds-vs-screen comparison — off-screen and clipping checks — wrong,
  and made a walkthrough recording lay its cards out against the wrong geometry.
  `android-ux-audit` already preferred the override density; the three now agree.

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
