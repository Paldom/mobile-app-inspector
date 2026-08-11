# Designing the matrix

Why these cells, what the emulator can and cannot stage, and why each detector
is trusted or distrusted. Everything here was checked against a stock
`google_apis_playstore` API 35 arm64 AVD.

**Contents:** [Fidelity](#fidelity-what-wm-size-is-not) ·
[Breakpoints](#breakpoints) · [Capability absence](#capability-absence) ·
[AVD recipes](#avd-recipes) · [Detectors](#detectors-and-their-false-positives) ·
[State safety](#state-safety)

## Fidelity: what `wm size` is not

`wm size WxH` and `wm density N` rewrite the *logical* display the framework
advertises. `Configuration`, `WindowMetrics.getBounds()` and resource selection
(`sw600dp`, `w840dp`) all follow. That is genuinely most of layout.

It does not reproduce:

| Not reproduced | Why it matters |
| --- | --- |
| Display cutout / punch-hole | `DisplayCutout` stays flat unless you enable an emulation overlay |
| Rounded-corner insets | the panel shape does not change |
| Fold posture / hinge | needs a foldable AVD and `cmd device_state` |
| A real OEM's inset geometry | only the same bars, rescaled |
| Multi-display, external display | untouched |

**The concrete way a pass here misleads:** an edge-to-edge app that lays out from
`WindowMetrics` and never consumes `WindowInsets.Type.displayCutout()` passes
every `wm size` cell, then ships content under the camera hole on real hardware.
Enable a cutout cell (`"cutout": "tall"`) to catch the obvious version of this,
and treat a real device as the final word.

This AVD reports one device state (`cmd device_state print-states` →
`DEFAULT`), so fold cells are unavailable here; on a foldable AVD they are
`cmd device_state state <id>`.

## Breakpoints

`dp = px × 160 ÷ dpi`. Pick the dpi, then the pixels.

Width dp selects the Material 3 / Jetpack **WindowSizeClass**:

| Class | Width dp | Typical |
| --- | --- | --- |
| compact | < 600 | phone portrait |
| medium | 600–839 | large phone landscape, small tablet, unfolded inner |
| expanded | 840–1199 | tablet |
| large | 1200–1599 | large tablet, desktop window |
| extra-large | ≥ 1600 | desktop |

`smallestScreenWidthDp` (`sw320`/`sw600`/`sw720`) is the *resource* bucket and is
orientation-independent: it is what picks `layout-sw600dp`. Both matter, so the
report prints the measured `widthDp`, `sizeClass` and `smallestWidthDp` per cell.

The default cells and what each is for:

| Cell | px @ dpi | dp | Catches |
| --- | --- | --- | --- |
| `compact-sw320` | 720x1280 @360 | 320x569 | the narrowest width still shipped |
| `compact-sw360` | 1080x2400 @480 | 360x800 | the most common phone |
| `medium-sw600` | 1200x1920 @320 | 600x960 | the compact→medium jump |
| `expanded-sw840` | 1680x2400 @320 | 840x1200 | tablet layouts, two-pane |
| `tablet-landscape` | 1920x1200 @240 | 1280x800 | wide, short — where vertical space runs out |
| `landscape` | rotation only | 914x411 here | activity recreation + a wide window |
| `largest-font` | font_scale 2.0 | — | text overflow |
| `worst-case` | 720x1280 @360 + 2.0 | 320x569 | narrow *and* huge text: the clipping magnet |

Verify, do not assume: the tool reads `dumpsys window` back (`cur=WxH`, the
`swNNNdp` token) and reports the bucket the device actually entered.

## Capability absence

### Testable at runtime, no root

| Loss | Command | What the app observes |
| --- | --- | --- |
| Permission denied | `pm revoke <pkg> <perm>` | `checkSelfPermission` → DENIED; the "please grant" branch |
| Permission neutered | `appops set <pkg> <OP> ignore` | often still GRANTED, but data comes back null/empty |
| Location off | `cmd location set-location-enabled false` | provider disabled |
| Offline | `cmd connectivity airplane-mode enable` | no transport |
| Wi-Fi / mobile off | `svc wifi disable`, `svc data disable` | one transport gone |
| Dark mode | `cmd uimode night yes` | night resources |
| Large text | `settings put system font_scale 2.0` | scaled text |
| Cutout | `cmd overlay enable …cutout.emulation.<kind>` | a real `DisplayCutout` |

**`pm revoke` and `appops ignore` are not substitutes.** Revoke tests the
permission-request path. `appops ignore` tests the nastier one: the app believes
it is allowed and gets nothing back, which is where a missing null check becomes
a crash. The `capability` profile runs both.

### Needs a purpose-built AVD

`pm list features` comes from the system image plus `config.ini` and is fixed at
boot. Nothing at runtime removes an entry, so these are emitted as recipes.

### Not testable here

Real OEM cutout hardware, genuine TEE/StrongBox provisioning, multi-SIM and
carrier behaviour, true low-memory kills under field conditions, and a
Play-Services-free device (the Play image ships GMS — use a `google_apis` or
AOSP image instead).

## AVD recipes

Edit `~/.android/avd/<name>.avd/config.ini` and cold boot, or pass
`-feature`/`-camera-back` flags to `emulator`. Keys that actually change
`pm list features`:

```ini
hw.camera.back=none          # android.hardware.camera[.any]
hw.camera.front=none         # android.hardware.camera.front
hw.gps=no                    # android.hardware.location.gps
hw.accelerometer=no          # android.hardware.sensor.accelerometer
hw.gyroscope=no              # android.hardware.sensor.gyroscope
hw.sensors.proximity=no      # android.hardware.sensor.proximity
hw.sensors.magnetic_field=no # android.hardware.sensor.compass
hw.sensors.light=no          # android.hardware.sensor.light
hw.audioInput=no             # android.hardware.microphone
hw.bluetooth=no              # android.hardware.bluetooth
hw.nfc=no                    # android.hardware.nfc
hw.fingerprint=no            # android.hardware.fingerprint
hw.keyboard=no / hw.dPad=no / hw.mainKeys=no
hw.lcd.width / hw.lcd.height / hw.lcd.density   # a genuinely different panel
hw.ramSize=2048              # memory pressure
```

Creating one:

```bash
avdmanager create avd -n no_camera_api35 -k "system-images;android-35;google_apis;arm64-v8a"
printf 'hw.camera.back=none\nhw.camera.front=none\n' >> ~/.android/avd/no_camera_api35.avd/config.ini
emulator -avd no_camera_api35 -no-snapshot-load
adb shell pm list features | grep camera   # confirm it is really gone
```

Always confirm with `pm list features` — a key that does not take effect is a
silent false negative.

## Detectors and their false positives

Ranked by how much they can be trusted. Only the top group gates.

| Signal | Source | False positives | Gate |
| --- | --- | --- | --- |
| `FATAL EXCEPTION` for the package | `logcat -b crash` | very low | yes |
| `am_anr` for the package | `logcat -b events` | very low | yes |
| `data_app_native_crash` | `dumpsys dropbox` | very low | yes |
| pid gone and not back | `pidof` | low | yes |
| never reaches top resumed activity | polled `dumpsys activity` | low once polled | yes |
| system prompt in front | top activity is a system package | expected in capability cells | no |
| refused rotation | requested vs measured rotation | none, but it is informative | no |
| blank screen | ≤1 node, or nothing carrying text/desc | medium | no |
| off-screen control | bounds outside the display | medium–high (animations, edge anchors) | no |
| zero-area control | empty bounds | medium | no |
| text truncation | — | very high | **not implemented** |

Reading the configuration back is itself error-prone, and three of these traps
only appeared when the sweep grew to eleven cells:

- `wm size` returns before the display reconfigures, so an immediate read returns
  the previous cell's geometry. Poll until the request is observed; report
  `config_not_applied` if it never is.
- `swNNNdp` appears several times in `dumpsys window`. The first match is often a
  stale `mFullConfiguration`; `mGlobalConfiguration` is the current one.
- `mRotation` is likewise ambiguous. Landscape is `widthDp > heightDp`.

Two more were false positives found by running the tool against a real app
and then fixing it:

- A single `mCurrentFocus` read during a resize returned another process, which
  looked like the app dying. It was mid-transition. Now polled.
- Disabling location made GMS put `LocationOffWarningActivity` in front. That is
  the platform prompting, not a failure, so system packages are classified
  separately.

ANRs deserve their own note: they are **not** in the crash buffer. Checking only
`-b crash` misses every hang.

## State safety

Every mutation is global to the device, so the run records the literal prior
value and writes it back — at the end of each cell, at the end of the run, and
on Ctrl-C. State lives in a file keyed to the adb serial so two emulators do not
share it, and `restore` replays it after a crash.

| Key | Restore | Trap |
| --- | --- | --- |
| size / density | `wm size reset`, `wm density reset` | `reset` returns the panel's value, not the previous cell's, so a pre-existing override must be re-applied instead |
| `font_scale` | put the saved value | the original is not always `1.0` |
| rotation | restore `user_rotation` **and** `accelerometer_rotation` | restoring only one leaves rotation stuck |
| night | `cmd uimode night <saved>` | `settings secure ui_night_mode` can disagree; use `cmd uimode` for both read and write |
| cutout overlay | disable what is on, enable what was on | more than one can exist |
| appops | write the exact saved mode | `default` is not a restore: an op that was `foreground` stays broken |
| permissions | `pm grant` only what was granted | re-granting something that was denied changes the app's state |
| airplane / radios | put the saved value back | leaving a radio off silently breaks the next suite |

`doctor` reports anything non-default even with no saved state, which is the
quickest way to answer "did something leave my emulator weird?".
