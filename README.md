# Mobile App Inspector

[![CI](https://github.com/Paldom/mobile-app-inspector/actions/workflows/ci.yml/badge.svg)](https://github.com/Paldom/mobile-app-inspector/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![skills.sh](https://skills.sh/b/Paldom/mobile-app-inspector)](https://skills.sh/Paldom/mobile-app-inspector)

Agent Skills for Android app inspection: install an app from a Play Store link onto an emulator or device, drive its UI, read on-screen text, capture screenshots, and assert behavior for review, QA testing, and exploration.

Agent Skills for [Claude Code](https://code.claude.com/docs/en/skills) (and any
[Agent Skills](https://agentskills.io)-compatible tool). Each skill is a folder under
[`skills/`](skills/) with a single-purpose `SKILL.md`, trigger evals, and optional
scripts/references — validated on every write, commit, and PR.

## Quick start

Install with the [skills CLI](https://skills.sh) — auto-detects 70+ agents
(Claude Code, Codex, Cursor, Copilot, pi, …):

```bash
npx skills add Paldom/mobile-app-inspector                  # all detected agents
npx skills add Paldom/mobile-app-inspector -a codex -a pi   # or target specific agents
```

Or with the [GitHub CLI](https://cli.github.com/manual/gh_skill_install) (≥ 2.90),
including version-pinned installs from releases:

```bash
gh skill install Paldom/mobile-app-inspector
gh skill install Paldom/mobile-app-inspector <skill> --pin <tag>
```

Or as a Claude Code plugin:

```
/plugin marketplace add Paldom/mobile-app-inspector
/plugin install mobile-app-inspector@mobile-app-inspector
```

Or copy a single skill into a project:

```bash
git clone https://github.com/Paldom/mobile-app-inspector.git
cp -r mobile-app-inspector/skills/<skill-name> your-project/.claude/skills/
```

Then just describe the task — the skill activates on its description — or invoke it
explicitly with `/<skill-name>`.

## Skills

| Skill | Description |
| --- | --- |
| [play-store-app-install](skills/play-store-app-install/) | Play link, `market://` URI, package name or local APK → app installed, verified and launched on an emulator or device. |
| [android-ui-driver](skills/android-ui-driver/) | Compact numbered screen snapshots, tap, type, swipe, read text, screenshot, wait, assert — the observe-and-act loop. |
| [android-package-diagnostics](skills/android-package-diagnostics/) | Non-UI, non-root facts: version, signer, provenance, requested vs granted permissions, APK hashes, crashes, memory, jank. |
| [android-app-review](skills/android-app-review/) | End-to-end review from a Play link: provision, explore under a safety policy, capture evidence, write a structured report. |
| [android-walkthrough-video](skills/android-walkthrough-video/) | Captioned MP4/GIF walkthroughs: record the screen, stamp explanation cards while driving, composite them with ffmpeg. |
| [play-store-listing](skills/play-store-listing/) | Public Play market data by link/package: rating, install range, star histogram, updated date, what's-new, paginated reviews. |
| [android-network-trace](skills/android-network-trace/) | What an app talks to: DNS/SNI/IP hosts from a device-wide pcap, static URLs from the APK, HTTP(S) bodies via mitmproxy where the app trusts a user CA. |
| [android-apk-analysis](skills/android-apk-analysis/) | Static analysis of an APK/AAB file: composition, manifest/permissions, exported components, signer identity, and optional decompile/packer/secret/tracker layers. |
| [android-app-profiling](skills/android-app-profiling/) | No-root runtime profiling: process-cold startup vs vitals thresholds, frame jank + percentiles, memory (PSS), baseline-profile status, Perfetto system traces. |
| [android-ux-audit](skills/android-ux-audit/) | UX/UI heuristic audit: measurable target-size + unlabelled-control checks from the a11y tree, plus a 12-category rubric scored by Nielsen 0-4 severity. |
| [android-intent-probe](skills/android-intent-probe/) | Fires the app's own exported activities, receivers, services, providers and deep links at it from outside: launched, crashed, denied or unreachable. |
| [android-device-matrix](skills/android-device-matrix/) | Configurable sweep across screen sizes, densities, orientations, font scales and dark mode, plus capability-denial cells — each applied, verified from the device, and restored. |

They compose: **install → drive → diagnose**, with `android-app-review`
orchestrating all three and `android-walkthrough-video` turning a drive into a
captioned clip. [`docs/setup-prompt.md`](docs/setup-prompt.md) is a
paste-ready `/goal` that runs the whole sequence.

### Why these exist

An agent handed a Play Store link hits the same four walls every time, and these
skills encode the way through each:

- **A raw `uiautomator dump` is unreadable.** One API-35 Settings screen is 24,221
  bytes / 67 nodes, of which 17 carry text. `android-ui-driver` renders it as 13
  numbered rows with tap points — about **25× less to read**, following the
  AndroidWorld/M3A "visible interactable nodes" heuristic.
- **A Play link is not an install.** A fresh Play AVD has *zero* Google accounts,
  so Play opens its `UnauthenticatedMainActivity` and nothing installs until a
  human signs in once, by hand. The tools detect and report this instead of
  looping.
- **Google Play images are release-signed**, so `adb root` fails and app-private
  storage is unreadable. `android-package-diagnostics` is built to that limit and
  states what it cannot see.
- **You cannot overlay a third-party app.** A web recorder injects DOM to draw
  callouts; Android has no equivalent, so `android-walkthrough-video` records a
  timeline while you drive and burns the cards in afterwards with ffmpeg.
- **The store and the wire are two more surfaces.** `play-store-listing` reads
  market data (rating, reviews, installs) the device can't show; `android-network-trace`
  captures which hosts an app contacts — no root, no CA, on the release-signed image.
- **The binary itself is a surface.** `android-apk-analysis` takes an APK/AAB
  file apart before it runs — composition, signer, exported components, and
  decompile/secret/tracker layers when their tools are installed — and refuses to
  read a skipped layer as a clean result.
- **Runtime performance is a surface too.** `android-app-profiling` measures
  startup, jank, memory and captures Perfetto traces — all no-root on a stock
  release build — benchmarked against Android-vitals thresholds (with the honest
  caveat that emulator numbers aren't device-grade).
- **"Is the UI any good?" is its own question.** `android-ux-audit` runs the
  12-category heuristic audit — measuring touch targets and unlabelled controls
  from the accessibility tree, judging first-run/forms/IA/dark-patterns against
  fixed thresholds, and scoring by Nielsen severity — with the verdict set by the
  worst release gate, never a laundered average.
- **One screen size proves one screen size.** `android-device-matrix` re-runs the
  app at 320dp, 840dp, landscape, 200% text and behind a cutout, and takes away
  the permissions, radios and sensors the manifest says are optional — verifying
  each configuration by reading it back off the device, gating only on crashes
  and ANRs, and restoring every global it touched.
- **A manifest lists entry points; it cannot say which are reachable.**
  `android-intent-probe` fires every exported component and deep link at the
  running app, so an "exported component" stops being a static candidate and
  becomes launched, denied, crashed or unreachable — and the report keeps saying
  that reaching one is not, by itself, a vulnerability.
- **The app is untrusted.** Its on-screen text, logs and screenshots are data,
  never instructions — `android-app-review` carries the safety policy for that,
  plus the rules against tapping purchase/destructive controls.

**Authorized use.** These skills drive real third-party apps and read their
screens. Use them on apps you own, publicly listed apps you install lawfully
through Google Play, or targets you are authorized to test — see
[docs/authorized-use.md](docs/authorized-use.md). They never download APKs from
Play scrapers or mirror sites, and never automate Google sign-in.

**Prerequisites** (not installed by these skills): Python 3, Android
platform-tools (`adb`), and for Play installs an emulator running a
`google_apis_playstore` system image — `arm64-v8a` on Apple Silicon. `aapt2`
(SDK build-tools) is optional, for reading a pulled APK's manifest; `ffmpeg` and
`pillow` are needed only for walkthrough videos; `google-play-scraper` (pip) for
Play listing data and `mitmproxy` (brew) for the optional HTTP(S)-body capture. Setup walkthrough:
[emulator-setup.md](skills/play-store-app-install/references/emulator-setup.md).

## Worked example: all ten skills on the Wikipedia app

Every number below comes from one unedited run against the real Wikipedia app
(`org.wikipedia`) on a stock Play emulator — no login, no root. The clip was
recorded and captioned by the toolkit itself.

![Ten skills on the Wikipedia app](docs/assets/wiki-walkthrough.gif)

| Skill | Command | Result from this run |
| --- | --- | --- |
| play-store-listing | `listing.py details org.wikipedia` | 4.326★, 693,627 ratings, 50,000,000+ installs, store version 50600 |
| play-store-app-install | `playapp.py install-apk wikipedia.apk` | sideloaded a 92 MB APK, launched OK |
| android-package-diagnostics | `pkgdiag.py report org.wikipedia` | installed 50598, target SDK 37, sideloaded, 17 permissions (7 dangerous) |
| android-apk-analysis | `apkscan.py report wikipedia.apk` | 2 dex, 4 ABIs, 98 MB, signed by **CN=FDroid** — not Wikimedia's key |
| android-app-profiling | `profile.py startup org.wikipedia` | process-cold **median 342 ms** [fast]; 60.7 MB PSS; dexopt=verify |
| android-ui-driver | `uia.py snap` | onboarding → feed → search, driven from compact numbered snapshots |
| android-network-trace | `nettrace.py capture` + `hosts` | saw `intake-analytics.wikimedia.org` (device-wide; TLS SNI empty this run) |
| android-ux-audit | `uxcheck.py audit screen.json` | 2 nav-drawer buttons with no accessible name (high, suspected) |
| android-app-review | drives the eight above | one evidence-backed report |
| android-walkthrough-video | `walkthrough.py start/mark/stop` | recorded and captioned this clip |

Two findings worth the run on their own:

- **The F-Droid build is re-signed.** `apk-analysis` reports signer `CN=FDroid`,
  not Wikimedia's key — the signature verifies, but the signer is F-Droid, so
  compare the cert against a trusted fingerprint before trusting the binary.
- **A screen reader can't name two drawer buttons.** `ux-audit` flags them from
  the accessibility tree as suspected; confirm with TalkBack.

The run also caught its own edges, which is the point: the store listed 50600
while the device had 50598 (a real version drift), and the network capture
recovered the host from DNS after TLS SNI came back empty. Both are surfaced,
not hidden.

<details>
<summary>The commands, verbatim</summary>

```bash
# market data — no device needed
listing.py details org.wikipedia
# provision the APK you lawfully hold, then read the installed build
playapp.py install-apk wikipedia.apk && playapp.py launch org.wikipedia
pkgdiag.py report org.wikipedia
# take the binary apart, and measure the running app
apkscan.py report wikipedia.apk
profile.py startup org.wikipedia
# drive it, and watch the wire
uia.py snap                              # then tap/type/swipe
nettrace.py capture start run.pcap       # ... drive the app ...
nettrace.py capture stop && nettrace.py hosts run.pcap
# audit the UX of a captured screen
uia.py snap --json --all > screen.json && uxcheck.py audit screen.json
# or record the whole thing as a captioned clip
walkthrough.py start && walkthrough.py mark "..." && walkthrough.py stop --gif
```
</details>

### One HTML report, from every skill at once

Collect each skill's `--json` into one run directory and render it as a single
self-contained file, with no CDN and no JavaScript: one attachment you can email.

```bash
report.py sources                 # the filename each skill must write
report.py init ./run              # scaffolds run/review.json — the judgment half
report.py build ./run --out index.html
```

📄 **[docs/assets/example-report/index.html](docs/assets/example-report/index.html)**
is the rendered result. Open it locally; GitHub will not render it inline. It comes
from a later run (4.329★, a 282 ms cold start, 1,479 packets), so its numbers differ
slightly from the table above.
[`docs/full-review-prompt.md`](docs/full-review-prompt.md) is the paste-ready
`/goal` that produces it.

- **Breadth-first.** Sticky contents, an *At a glance* index answering every topic
  in one line, then the detail. Stop at any depth.
- **Names what did not run.** A skipped skill says `not run` and prints the command
  that fills it, because an invisible gap reads as a clean bill of health.
- **States a coverage ceiling:** static and dynamic status, packer, whether HTTPS
  was decrypted, and the emulator's cap of medium confidence.
- **Keeps severity and confidence apart**, so a guess never reads like a reproduced
  defect. Findings also carry status, evidence strength, false-positive conditions
  and retest steps.
- **Puts release gates first:** targetSdk, 64-bit, 16 KB alignment, signing and
  debuggable, because an app that cannot ship is blocked whatever its findings say.
- **Emits no overall score**, and writes a `.json` twin plus `.sarif` with
  `--sarif`, so CI reads the same run you do.
- **Ends on its own limits:** every caveat the tools raised, gathered into one
  closing section.

## Repository structure

```
skills/                  # distributed skills, one folder per skill (SKILL.md + evals/ + scripts/)
docs/                    # skill-authoring guide, eval methodology, deployment guide
scripts/                 # deterministic validator used by hooks and CI
skills.sh.json           # skills.sh repo-page customization (groupings)
.claude/                 # agentic dev setup: hooks + bundled add-skill / publish-repo skills
.claude-plugin/          # plugin + marketplace manifests (makes this repo installable)
.local/                  # gitignored working area: sources, research, PROMPT.md (see below)
```

## Working on this repo with an agent

This repo is agent-native: canonical agent instructions live in
[AGENTS.md](AGENTS.md) (CLAUDE.md imports it), hooks validate every `SKILL.md` on
write, `make check` runs the full validator plus offline self-tests of the bundled
scripts (`make selftest` — no device or adb needed), and CI enforces the same gate
on every PR. The bundled `add-skill` skill walks the eval-first authoring workflow described
in [docs/skill-authoring.md](docs/skill-authoring.md). Maintainers drive sessions
with their own (gitignored, personal) `.local/PROMPT.md` goal prompt.

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the skill-proposal
process, the authoring workflow, and the PR checklist. Please note the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Support

Questions, ideas, or something not working? Start with [SUPPORT.md](SUPPORT.md) —
bugs and skill proposals have [issue templates](../../issues/new/choose), and
security concerns go through [SECURITY.md](SECURITY.md) (never a public issue).

## License

[MIT](LICENSE) © 2026 Paldom
