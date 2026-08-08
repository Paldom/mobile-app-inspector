# Getting a Play-enabled Android emulator on macOS

Everything here is a **prerequisite**, not something the skill installs for you.
`playapp.py` and `uia.py` need only Python 3 and `adb`, but `adb` itself, the
emulator, and a system image come from the Android SDK, and `aapt2` comes from
build-tools. Budget several GB and one manual sign-in.

**Contents:** [Which image](#which-image) · [Install the SDK](#install-the-sdk) ·
[Create the AVD](#create-the-avd) · [Boot it](#boot-it) ·
[The one-time Play sign-in](#the-one-time-play-sign-in) ·
[Make it deterministic](#make-it-deterministic) · [Troubleshooting](#troubleshooting)

## Which image

| Image tag | Play Store app | Google services | `adb root` |
| --- | --- | --- | --- |
| `default` / AOSP | no | no | yes |
| `google_apis` | **no** | yes | yes |
| `google_apis_playstore` | yes | yes | **no** (release-signed) |

Installing from a Play link requires `google_apis_playstore`. Accept that you
lose root: on that image `adb root` returns *"adbd cannot run as root in
production builds"* and `adb shell id` stays `uid=2000(shell)`. If you need root
instead, use `google_apis` and sideload APKs.

Architecture must match the host: **`arm64-v8a` on Apple Silicon**, `x86_64` on
Intel. A mismatch either runs through slow translation or fails outright with
`INSTALL_FAILED_NO_MATCHING_ABIS`.

## Install the SDK

Easiest path is Android Studio (its setup wizard fetches platform-tools, the
emulator, and an image). Command-line only:

```bash
brew install --cask android-commandlinetools
export ANDROID_HOME="$HOME/Library/Android/sdk"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"

sdkmanager --list | grep google_apis_playstore     # see what is actually offered
sdkmanager "platform-tools" "emulator" \
           "system-images;android-35;google_apis_playstore;arm64-v8a"
```

Add those two `export` lines to your shell profile — "adb not found" is the most
common failure by far, and Android Studio does not put `adb` on `PATH` for you.

`aapt2` (used by `android-package-diagnostics apk --pull` to read a manifest)
lives in `build-tools`, not `platform-tools`:

```bash
sdkmanager "build-tools;35.0.0"
```

## Create the AVD

```bash
avdmanager list device                              # pick a real device id
echo "no" | avdmanager create avd \
  --name play-api35 \
  --package "system-images;android-35;google_apis_playstore;arm64-v8a" \
  --device pixel_7
```

In Android Studio instead: **Device Manager → Create device**, pick a profile
showing the **Google Play** logo, then an image labelled **Google Play** (not
merely "Google APIs").

## Boot it

```bash
emulator -avd play-api35 -gpu auto -no-boot-anim &

adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do
  sleep 2
done
adb devices -l
```

Wait for `sys.boot_completed`, not just for `adb wait-for-device` — the transport
comes up long before the UI does, and commands issued in between fail confusingly.

## The one-time Play sign-in

A freshly created AVD has **zero** Google accounts (`dumpsys account` shows
none), so Play cannot install anything. Open the Play Store app in the emulator
window and sign in **by hand**, once.

Do not automate this, and do not ask an agent to: it involves 2FA and CAPTCHA by
design, and automating Google sign-in breaches Play's terms. Use a throwaway
account with no payment method and no personal data — the AVD is a test
environment, and screenshots and logs from it may be shared.

Afterwards, `playapp.py preflight` should report the account, and Play Protect
certification usually settles within a few minutes.

## Make it deterministic

```bash
adb shell settings put global window_animation_scale 0
adb shell settings put global transition_animation_scale 0
adb shell settings put global animator_duration_scale 0
adb logcat -c
```

Animations are the main cause of `uiautomator dump` failing with "could not get
idle state". Once the AVD is signed in and configured, take an emulator snapshot
so you can return to this exact state instead of repeating the sign-in.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `adb: command not found` | SDK not on `PATH` | export `ANDROID_HOME` and `platform-tools` as above |
| `device offline` | stale adb server / half-booted AVD | `adb kill-server && adb start-server`; cold-boot the AVD |
| No Play Store on the device | `google_apis` or AOSP image | recreate the AVD with `google_apis_playstore` |
| Play opens "unauthenticated" screen | no Google account | sign in by hand, once |
| App missing from Play search | country, account, or device filtering | check the web listing; or use an APK you lawfully have |
| App installs then exits instantly | Play Integrity / emulator detection | use a physical device; banking and DRM apps refuse emulators |
| `INSTALL_FAILED_NO_MATCHING_ABIS` | APK has no ABI the image supports | use an `arm64-v8a` image on Apple Silicon |
| `INSTALL_FAILED_MISSING_SPLIT` | only the base APK was installed | pass every split to `install-apk` |
| Emulator blank or very slow | GPU/virtualisation mismatch | `-gpu auto`, cold boot, or `-gpu software` |
| `localhost` unreachable from the app | inside the AVD, `localhost` is the AVD | use the host alias `10.0.2.2` |
