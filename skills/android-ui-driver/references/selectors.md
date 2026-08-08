# What the accessibility tree exposes, per app framework

`uiautomator dump` reads Android's accessibility/UI hierarchy. You can only
locate what the app publishes to it. That is developer-dependent, which is why
locator choice matters more than locator syntax.

**Contents:** [Locator preference](#locator-preference-order) ·
[Per framework](#per-framework) · [Reading the snapshot](#reading-the-snapshot) ·
[When nothing is exposed](#when-nothing-is-exposed)

## Locator preference order

1. **`--text`** — visible text or content-description. The most portable choice
   on apps you do not control, and the one a human would name. Breaks on locale
   change.
2. **`--desc`** — content-description. Best for icon-only buttons.
3. **`--id`** — resource-id. Stable *when present*, but R8/ProGuard strips or
   obfuscates it in release builds, and Compose does not emit it by default.
4. **`--nth`** — position among equal matches, for lists and tab bars. Required
   when several elements match: `tap`/`type` refuse ambiguous targets rather than
   guessing, and print the candidates so you can choose.
5. **Index** (`tap 3`) — fast, but valid only until the screen changes.
6. **`--at X,Y`** — last resort. Breaks across screen size, density, font
   scale, orientation and locale.

## Per framework

| Framework | `text` | `content-desc` | `resource-id` | Notes |
| --- | --- | --- | --- | --- |
| Android Views (XML layouts) | yes | yes | yes, unless obfuscated | the easy case |
| Jetpack Compose | yes | yes | **only** if the developer set `testTagsAsResourceId = true` | you cannot enable this on a third-party app; use text |
| React Native | yes | `accessibilityLabel` | `testID` maps to resource-id (RN ≥ 0.64, package-prefixed) | coverage is patchy, `TextInput` often omits it |
| Flutter | via the semantics bridge | via semantics | no | renders to a canvas; you see whatever semantics the app shipped, which can be complete or nearly empty. Widget keys are never visible |
| Unity / games / custom canvas | usually nothing | usually nothing | no | screenshot + read visually |
| WebView | one opaque subtree | limited | no | native side sees a container, not the DOM |

The practical rule: **do not guess the framework, read the snapshot.** If `snap`
returns a handful of rows on a visually busy screen, the app is not publishing
semantics — switch to screenshots regardless of what framework you think it is.

## Reading the snapshot

```
act=org.fdroid.fdroid/.views.main.MainActivity size=1080x2400 n=10
0 chk "Enable repository" @477,140 checked=false
1 btn "Over Wi-Fi / Always use this connection when available" @540,966
2 txt "Latest" @108,2282 #navigation_bar_item_large_label_view selected
3 scroll "" @540,1095 #recycler_view scrollable
```

- `0 chk … checked=false` — a toggle and its current state; assert on the state,
  not on the presence of the row.
- `1 btn "A / B"` — a clickable parent that absorbed its title and summary text.
  Tapping row 1 hits the parent, which is what a user taps.
- `2 txt … selected` — the active tab. `selected` is how you tell which tab is
  current; the text alone will not.
- `3 scroll … scrollable` — swipe here to reveal more. If a list looks short,
  check for a `scrollable` row before concluding the content is missing.

## When nothing is exposed

1. `shot` a PNG and read it directly — you have vision; the hierarchy is an
   optimisation, not a requirement.
2. Locate the control visually, then `tap --at X,Y` from the screenshot
   coordinates. Screenshot pixels map 1:1 to device coordinates (both are the
   `size=WxH` reported by `snap`), so no scaling is needed — but re-check after
   any rotation.
3. If you need repeatable assertions on such a screen, say so plainly: it is not
   reliably automatable through the accessibility tree, and a visual assertion is
   the honest fallback.

`FLAG_SECURE` is the one case where the *screenshot* fails instead: the PNG comes
back valid but entirely black. The hierarchy often still works there, so try
`text` before giving up.
