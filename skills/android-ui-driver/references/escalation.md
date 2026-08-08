# When raw adb is the wrong tool

`scripts/uia.py` is deliberately the floor: it needs nothing but Android
platform-tools and Python 3, and it works on a non-rooted, release-signed Google
Play emulator image. That buys zero setup at the cost of real ceilings. This
page names the ceilings and the escalation for each.

**Contents:** [Honest positioning](#honest-positioning) ·
[Ceilings](#ceilings-and-what-to-move-to) · [uiautomator2](#uiautomator2-python) ·
[Appium](#appium--uiautomator2-driver) · [Maestro](#maestro) ·
[WebViews](#webviews) · [Choosing](#choosing)

## Honest positioning

The compact-snapshot idea is what makes this agent-friendly, and it is
**backend-neutral** — the same compaction could wrap uiautomator2 or Appium. The
measured 25x reduction (24,221 bytes of XML → ~900 bytes of rows on an API 35
Settings screen) is evidence that *compaction* pays, not evidence that raw adb
beats the alternatives.

What raw adb genuinely wins on is **setup cost**: no pip install, no Node
runtime, no server process, no agent APK pushed to the device, nothing to keep
version-matched. What it genuinely loses on is **per-action latency** and
**input fidelity**.

No task-level benchmark against uiautomator2 has been run for this repo. Do not
claim raw adb is faster; claim it is dependency-free.

## Ceilings and what to move to

| Ceiling | Measured / observed | Escalate to |
| --- | --- | --- |
| ~2 s per hierarchy dump | 1.95–2.20 s, API 35 arm64 emulator | uiautomator2 (persistent on-device HTTP server) |
| Non-ASCII text input impossible | `input text 'café'` throws `NullPointerException` in `InputShellCommand.sendText`, types nothing; no `cmd clipboard` on the image | uiautomator2 `send_keys`, or an IME such as ADBKeyBoard |
| No background dialog handling | permission prompts must be handled inline | uiautomator2 watchers, Maestro's built-in handling |
| No WebView DOM | WebView is one opaque subtree in the native hierarchy | Appium context switch, or CDP (below) |
| Flaky-by-default waits | you write the polling | Maestro's zero-wait retries |
| Maintained regression suite | this is a driver, not a test framework | Maestro flows or Appium + a runner |
| Multi-device / grid | single device by design | Appium |

## uiautomator2 (Python)

Closest ergonomics to this driver, so it is the smallest jump.

```bash
pip install uiautomator2
```

```python
import uiautomator2 as u2
d = u2.connect()                      # local emulator / USB device
d.app_start("com.example.app", wait=True)
d(text="Sign in").click()             # implicit wait built in
d(resourceId="com.example.app:id/email").set_text("me@example.com")  # unicode OK
d.watcher.when("Allow").click(); d.watcher.start()   # background dialog handler
xml = d.dump_hierarchy(compressed=True)
d.screenshot("s.png")
```

Cost: a pip dependency plus two APKs pushed to the device (its server), which
Android can kill in the background. Gains: much faster repeated queries, real
unicode input, watchers.

## Appium + UiAutomator2 driver

The industry-standard, W3C WebDriver option. Choose it for multi-language
clients, device grids, WebView context switching, or long-term vendor stability.

```bash
npm install -g appium
appium driver install uiautomator2
appium driver doctor uiautomator2
```

Heavier: a Node server, a session lifecycle, and `page_source` is the same large
XML you were avoiding. Prefer `accessibility id` and `id` locators — Google
intends to remove `UiSelector`/`UiObject`, which will invalidate Appium's
`-android uiautomator` locator strategy.

## Maestro

Declarative YAML with built-in retry/tolerance — the best out-of-the-box
flakiness story, and readable as a durable artifact.

```yaml
appId: com.example.app
---
- launchApp
- assertVisible: "Welcome"
- tapOn: "Continue"
- takeScreenshot: dashboard
```

Less expressive for data-dependent branching than Python. Its AI assertion
commands route screenshots to a cloud backend — a privacy consideration when
reviewing someone else's app.

## WebViews

If the app embeds a **debuggable** WebView you can attach real browser tooling:

```bash
adb shell cat /proc/net/unix | grep devtools_remote
adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>
# then connect over CDP to http://localhost:9222
```

Production third-party builds normally leave WebView debugging off, so no socket
appears and this path is closed. Check before planning around it.

## Choosing

Stay on `uia.py` while the work is: exploratory, one-off, ASCII-only, on one
device, and latency-tolerant. That covers most agent-driven review and
inspection.

Move to uiautomator2 the moment you need unicode input or a tight loop; to
Maestro when a human will maintain the flows; to Appium when you need a grid,
WebView contexts, or a non-Python client.
