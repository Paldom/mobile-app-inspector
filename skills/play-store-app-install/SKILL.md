---
name: play-store-app-install
license: MIT
description: Installs an Android app onto an emulator or device from a Google Play link, market:// URI, package name, or a local APK, then verifies and launches it. Use when the user shares a Play Store URL or asks to install, sideload, provision or set up an app on an emulator for testing, to put a base.apk and its split config APKs on the device, or when they hit an INSTALL_FAILED error. Not for driving the UI or reading screens.
argument-hint: "<play-store-url | package-name | path/to.apk>"
---

# play-store-app-install

Gets a named Android app onto a device and running, from whatever the user has:
a Play Store link, a bare package name, or APK files they already hold.
`scripts/playapp.py` (standard library only) does the deterministic parts —
parsing, preflight, state checks, sideloading, verification, launching.

The one step it cannot do alone is tapping Play's **Install** button, which is UI
work; use `android-ui-driver` for that, bracketed by `open-listing` and
`wait-installed`.

## When NOT to use

- Tapping around inside an app that is already installed → `android-ui-driver`.
- Permissions, version, crash logs of an installed app → `android-package-diagnostics`.
- "Review this app end to end" → `android-app-review` (it calls this skill first).
- Publishing *your* app to Play, `bundletool`, internal test tracks → developer
  workflows, out of scope here.

## The one thing to know first

**A Play link alone does not guarantee an install.** Play can refuse for country,
account entitlement, age rating, payment, ABI, API level, staged rollout, or an
emulator exclusion, and integrity-gated apps (banking, DRM, anti-cheat) commonly
refuse emulators outright. Treat installation as a state machine with a real
"cannot, and here is why" outcome — not a step that always succeeds.

**Never download an APK for the user from a Play scraper or a mirror site.**
Unofficial Play clients breach Play's terms and risk the account; mirrors have no
provenance. Either install through Play, or use an APK the user already lawfully
has.

## Workflow

Run everything as `python3 "${CLAUDE_SKILL_DIR}/scripts/playapp.py" <cmd>`.

1. **Resolve the link.** The package name is already in the URL's `id=`
   parameter — no web search needed.

   ```bash
   playapp.py resolve "https://play.google.com/store/apps/details?id=com.spotify.music&hl=en"
   # -> com.spotify.music
   ```

   Handles `play.google.com/...?id=`, `market://details?id=`,
   `.../details/App_Name?id=`, and a bare package name. Shortened
   `play.app.goo.gl` links cannot be resolved offline — ask for the full URL.

2. **Preflight.** Never skip this; it is where the two blocking conditions surface.

   ```bash
   playapp.py preflight            # add --fix to switch off UI animations
   ```

   Prints one machine-readable line per component so you can act on a single
   grep, then a verdict:

   ```
   STATUS device: OK emulator-5554
   STATUS play_store: OK com.android.vending
   STATUS play_account: MISSING no Google account — a human must sign in ...
   STATUS animations: WARN on (window=1.0, ...) — re-run with --fix
   RESULT: fail
   ```

   `OK` / `WARN` / `MISSING` / `FAIL`, and `RESULT: ok|fail`. `--fix` switches off
   the three animation scales — the only blocker here that is safely scriptable;
   the Play image choice and the Google sign-in are not.

3. **Check current state** before installing anything:

   ```bash
   playapp.py status com.spotify.music     # exit 1 = not installed
   ```

   Uses `pm path`, not `pm list packages` — the latter substring-matches and will
   happily report `com.foo.bar` when you asked about `com.foo`.

4. **Install**, by whichever route applies:

   **a. From Play** (needs a Play image *and* a signed-in account):
   ```bash
   playapp.py open-listing com.spotify.music     # opens market://details on device
   # then, with android-ui-driver:
   #   uia.py snap                                # find the button
   #   uia.py tap --text "Install"                # may read Install/Update/Open/Buy
   playapp.py wait-installed com.spotify.music --timeout 300
   ```

   **b. From an APK the user has:**
   ```bash
   playapp.py install-apk ./app.apk
   playapp.py install-apk ./base.apk ./split_config.arm64_v8a.apk ./split_config.en.apk
   ```
   One file uses `adb install`, several use `install-multiple`. Both pass `-r`
   (replace) and `-g` (grant runtime permissions).

5. **Launch and confirm it survived launch:**

   ```bash
   playapp.py launch com.spotify.music
   ```

   Resolves the real launcher activity, starts it, then checks the package is
   actually in the foreground. If it is not, the app crashed or bounced — exit 1,
   and `android-package-diagnostics crashes` will say why.

## Output spec

- The package name, stated once and reused (never re-derived by guessing).
- A preflight verdict naming any blocker in the user's terms.
- After install: version name + code and install source, from `status`.
- On failure: which state was reached and the specific reason, not a retry loop.

## Gotchas

- **A fresh Play AVD has no Google account.** Play then opens
  `...unauthenticated.activity.UnauthenticatedMainActivity` and nothing can be
  installed. A human must sign in **once, by hand**. Do not automate Google
  sign-in — 2FA and CAPTCHA aside, it breaches Play's terms. `preflight` and
  `open-listing` both detect and report this state.
- **`-g` skews a privacy review.** `install-apk` pre-grants runtime permissions so
  the app runs without prompts. If the point is auditing what the app *asks* for,
  install without `-g` and let the prompts happen — otherwise
  `android-package-diagnostics` will show everything already granted.
- **Google Play images are release-signed**, so `adb root` fails
  ("adbd cannot run as root in production builds") and app-private storage stays
  unreadable. That is the price of having the Play Store; an AOSP image is
  rootable but has no Play.
- **`google_apis` ≠ `google_apis_playstore`.** Only the latter ships the Store app.
  `preflight` checks this explicitly.
- **Apple Silicon needs `arm64-v8a` system images.** An x86-only APK then fails with
  `INSTALL_FAILED_NO_MATCHING_ABIS`. `install-apk` explains each `INSTALL_FAILED_*`
  code it sees.
- **Split APKs**: installing only `base.apk` gives `INSTALL_FAILED_MISSING_SPLIT`.
  Pass every split in one `install-apk` call.
- **`status` on a preinstalled app** shows installer `(none)` — a genuine
  provenance signal, not a bug. Play-installed apps report `com.android.vending`.

## Files

- `scripts/playapp.py` — resolve, preflight, status, open-listing, install-apk,
  wait-installed, launch, uninstall. `--help` per subcommand.
- `references/emulator-setup.md` — creating and booting a Play-enabled AVD from
  scratch, image choice, and the one-time Play sign-in.
- `../../docs/authorized-use.md` — what these skills may and may not be used for.
