---
name: android-package-diagnostics
license: MIT
description: Reports non-UI facts about an installed Android package - version, signer, install source, requested versus granted permissions, APK paths and hashes, crashes, ANRs, memory and jank. Use when asked what permissions an app requests, which version is installed, whether it was sideloaded, why it crashed, or its memory use. Not for tapping through the UI or end-to-end app reviews.
argument-hint: "<package-name> [what to check]"
---

# android-package-diagnostics

Everything you can learn about an installed app **without touching its UI and
without root**. `scripts/pkgdiag.py` (standard library only) distils
`dumpsys package` — over 700 lines for a single app — plus logcat, meminfo and
gfxinfo into a short structured report.

Non-root is the operating constraint, not an oversight: Google Play emulator
images are release-signed, so `adb root` is unavailable. Everything here is
chosen to work under that limit.

## When NOT to use

- Tapping, reading screens, screenshots → `android-ui-driver`.
- Getting the app onto the device → `play-store-app-install`.
- A full narrative review of the app → `android-app-review` (it calls this skill).
- Deep performance work (startup medians, jank percentiles, Perfetto traces) →
  `android-app-profiling`; `perf` here is only the quick one-shot number.
- Reading app-private files, databases, SharedPreferences → **not possible here**;
  see [What this cannot do](#what-this-cannot-do).
- Decompiling, static bytecode analysis, tracker detection → dedicated tools
  (jadx, apktool, exodus); this reports what the *platform* knows.

## Workflow

Run as `python3 "${CLAUDE_SKILL_DIR}/scripts/pkgdiag.py" <cmd> <package>`.

1. **One-shot summary** — start here for almost any question:

   ```bash
   pkgdiag.py report org.fdroid.fdroid          # --json for machine use
   ```
   ```
   org.fdroid.fdroid  1.23.2 (code 1023052)
     sdk          min 23 / target 30
     abi          arm64-v8a
     installer    (none — sideloaded or preinstalled)
     signer       8b8a3ff5
     uid/dataDir  10207  /data/user/0/org.fdroid.fdroid
     apks         1
     permissions  29 requested, 10 dangerous, 10 runtime-granted, 0 denied
     dangerous:
       - CAMERA  [granted]
       - ACCESS_COARSE_LOCATION  [granted]
   ```

2. **Permission audit** — every requested permission with its real state:

   ```bash
   pkgdiag.py permissions org.fdroid.fdroid
   ```

   Distinguishes *install-time* from *runtime* permissions and marks the
   dangerous ones. "Dangerous" is read from the device itself
   (`pm list permissions -g -d`), not a hardcoded table that rots each API level.

3. **Crashes and ANRs**:

   ```bash
   adb logcat -c            # clear first, so you know what is fresh
   # ... reproduce ...
   pkgdiag.py crashes org.fdroid.fdroid
   ```

4. **Runtime cost** (app must be running):

   ```bash
   pkgdiag.py perf org.fdroid.fdroid
   # total PSS 79 MB / java 17 MB / native 23 MB; 88 frames, 18 janky (20.45%)
   ```

5. **The APK itself** — paths, sizes, SHA-256, and manifest facts:

   ```bash
   pkgdiag.py apk org.fdroid.fdroid --pull ./apks
   ```

   Pulls each APK, hashes it, and (when SDK build-tools are installed) reads
   `aapt2 dump badging` for label, SDK versions and native ABIs.

## Output spec

- A summary, never a raw `dumpsys` paste into the conversation.
- Permission claims always say *requested* vs *granted* — they are different facts.
- Provenance stated explicitly: Play-installed (`com.android.vending`) vs
  sideloaded/preinstalled (`none`).
- Any limit hit is named rather than worked around silently.

## Gotchas

- **`adb install -g` pre-grants every runtime permission.** A privacy review of
  an app installed that way will look falsely permissive — everything shows
  `granted`. For an honest audit, install *without* `-g` and let the app ask.
- **Log buffers are ring buffers.** `crashes` reports whatever is still in them,
  which may predate your run or have already scrolled away. Always `adb logcat -c`
  before reproducing.
- **`pm list permissions -d` alone is nearly empty** (3 entries on API 35). The
  grouped form `-g -d` is the real list (~129). The script uses the grouped form;
  do not "simplify" it.
- **The app uid is `appId=` in dumpsys**, not `userId=` — `userId` is the Android
  *user/profile* number.
- **`perf` needs a live process.** `dumpsys meminfo` reports "No process found"
  for a stopped app; start it first.
- **Frame jank needs interaction.** `gfxinfo` is empty until the app has actually
  rendered; drive it a little, then measure.
- **Split APKs are normal.** Play-delivered apps often install several; `report`
  flags this. A single pulled `base.apk` is not the whole app.
- **A signer digest is short (`8b8a3ff5`)** — useful for *comparing* two installs
  of the same app, not as a full certificate check. Use `apksigner verify
  --print-certs` on a pulled APK for that.

## What this cannot do

Without root, on a Play-signed image, these are genuinely out of reach — say so
rather than improvising:

| Want | Why not | Real alternative |
| --- | --- | --- |
| Read `/data/data/<pkg>` | not rooted; `run-as` only works on **debuggable** builds, and store apps are not debuggable | AOSP/rooted image, or a debuggable build from the developer |
| Decrypt TLS traffic | Android 7+ ignores user-installed CAs for app traffic unless the app opts in | app with a debug `network_security_config`, or a rooted system CA |
| Enumerate trackers / SDKs | needs static analysis of the bytecode | jadx / exodus-standalone on a pulled APK |
| Full certificate chain | `dumpsys` shows only a short digest | `apksigner verify --print-certs base.apk` |
| Battery attribution per app | `batterystats` needs a reset + unplug cycle to be meaningful | `dumpsys batterystats --reset`, then a controlled run |

## Files

- `scripts/pkgdiag.py` — report, permissions, crashes, perf, apk. `--help` per
  subcommand.
