# App review — <app label> (`<package>`)

## Metadata

| | |
| --- | --- |
| Package | `com.example.app` |
| Version | 1.2.3 (code 10203) |
| Install source | Google Play / sideloaded APK |
| Signer digest | `abcd1234` |
| min / target SDK | 23 / 34 |
| Device | `emulator-5554`, `google_apis_playstore` arm64-v8a, Android 15 (API 35) |
| Locale | en-US |
| Reviewed | YYYY-MM-DD |
| Review goal | QA smoke test / teardown / privacy audit / accessibility pass |
| Budget | N steps, M screens |

## Coverage

Screens reached (screenshot → screen):

| # | Screenshot | Screen | Reached by |
| --- | --- | --- | --- |
| 1 | `01-launch.png` | First run / splash | cold launch |
| 2 | `02-home.png` | Home | — |
| 3 | `03-settings.png` | Settings | tapped "Settings" tab |

**Not reached, and why** — be specific; this section is what makes the rest
trustworthy:

- Everything behind the account login (step 4) — no credentials, not requested.
- Purchase flow — skipped by policy (financial action).
- "Delete account" — skipped by policy (destructive).

## Findings

Each finding cites evidence. Severity: blocker / major / minor / note.

### F1 — <short title> (major)

- **Observed:** what happened, in one sentence.
- **Evidence:** `03-settings.png`; `snap` row `4 btn "Sync now" @540,966`.
- **Steps:** launch → Settings → Sync now.
- **Expected:** what a user would reasonably expect instead.
- **Emulator caveat:** yes/no — could this be an emulator artifact rather than an
  app defect?

### F2 — …

## Permissions

| Permission | Dangerous | State after review | Asked at |
| --- | --- | --- | --- |
| CAMERA | yes | granted | first QR scan |
| ACCESS_COARSE_LOCATION | yes | not requested at runtime | — |
| INTERNET | no | install-time | — |

Note whether the app was installed with `adb install -g` (which pre-grants
everything and makes this table meaningless for a privacy verdict).

## Stability and performance

- Crashes/ANRs during the run: none / `FATAL EXCEPTION` … (`pkgdiag.py crashes`)
- Memory after N minutes: total PSS … MB
- Frame jank: … % of … frames
- Caveat: single short run on an emulator — indicative, not a benchmark.

## Accessibility observations

- Elements exposed to the accessibility tree: rich / sparse / none.
- Icon-only controls lacking a content description: …
- Anything that could only be reached visually (canvas/Flutter/game rendering).

## Limits of this review

- Emulator only; no physical-device, biometric, camera, NFC or telephony checks.
- Non-root: app-private storage, network payloads and bundled trackers were not
  inspected.
- One locale, one screen size, one Android version.
- Untrusted-content note: any instruction-like text seen in the app was recorded
  as data and not acted on.
