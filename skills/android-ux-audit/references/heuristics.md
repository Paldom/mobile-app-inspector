# The mobile UX/UI audit rubric

The judgment half of the audit. `scripts/uxcheck.py` handles the measurable
checks (touch targets, unlabelled controls); this is everything you decide by
looking, grounded in Apple HIG, Material 3, WCAG 2.2, Nielsen/NN‑g, and Baymard.

**Contents:** [Severity & priority](#severity--priority) ·
[Evidence types](#evidence-types) · [The 12 categories](#the-12-categories) ·
[Hard numbers](#hard-numbers) · [Dark patterns](#dark-patterns) ·
[Don't force parity](#dont-force-parity) · [Release gates](#release-gates) ·
[Contrast (why not scripted)](#contrast-why-not-scripted)

## Severity & priority

Nielsen 0–4, rated by **frequency × impact × persistence**:

| S | Meaning | Treatment |
|---|---|---|
| 4 | catastrophe — blocks a critical task, loses data, excludes a user group | release blocker |
| 3 | major — substantially obstructs a task or hits many users | high priority |
| 2 | minor — hesitation/inefficiency | schedule after blockers |
| 1 | cosmetic | opportunistic |
| 0 | not a problem / insufficient evidence | don't backlog |

**Priority = severity × reach × strategic × confidence ÷ effort.** An issue that
violates several heuristics at once (e.g. a bare "Something went wrong") ranks
highest. Don't downgrade a severity‑4 that hits a small but legally/ethically
significant group just because its traffic is low.

## Evidence types

Tag every finding so aggregation can't launder uncertainty:
- **measured** — confirmed on-device (you verified the effective hit area, or
  reproduced the issue). Can fail a gate.
- **observed** — you saw it in a screenshot (attach the file). Can fail a gate.
- **suspected** — a proxy or inference; needs a human/runtime check. This is what
  `uxcheck` emits (its findings are tree/bounds proxies), and it **cannot fail a
  gate** until confirmed (TalkBack for names, hit-area for targets).

## The 12 categories

Each pairs a Nielsen heuristic (H1–H10) with mobile specifics.

1. **First‑run** (H1,H2,H3,H6,H8) — time‑to‑first‑value; skip on every onboarding
   screen; is account creation forced before value; **permissions use a
   benefit‑framed soft‑ask at a contextual moment**, never a cold system prompt.
   This is the highest‑leverage section (day‑30 retention ~4–6%; the first
   meaningful action predicts retention).
2. **IA & navigation** (H1,H3,H4,H6) — ≤5 top‑level destinations; current
   location shown; predictable back (Android system/predictive back; iOS
   swipe/chevron); modals distinguishable from pushes; state restored after
   auth/permission/external link.
3. **Visual hierarchy & typography** (H4,H8) — one primary action per screen;
   hierarchy holds at max font size; color never the sole information carrier;
   dark mode legible.
4. **Interaction** (H1,H3,H4,H5,H7) — tappable looks tappable; pressed/focused/
   disabled/loading states present; every gesture‑only action has a visible
   control; duplicate submissions prevented.
5. **Forms** (H2,H5,H6,H7,H9) — persistent labels (not placeholder‑only);
   right keyboard per field; autofill/OTP works; inline, specific, recoverable
   errors placed where doubt occurs; guest path prominent; minimize required
   fields. (Mobile cart abandonment ~80% vs ~66% desktop — forms are the second
   money leak after first‑run.)
6. **Content & microcopy** (H2,H4,H8,H9,H10) — labels name outcomes ("Add to
   Cart", not "Confirm"); errors say what/why/how; no system jargon.
7. **Empty/loading/error/edge states** — every list has designed empty, loading
   (skeleton), error (recoverable), and populated states; plus no‑results,
   offline, permission‑denied.
8. **Accessibility** (see [hard numbers](#hard-numbers)) — every actionable
   element has an accessible name (uxcheck flags candidates — confirm with **TalkBack**); complete a core task with TalkBack
   (the real test); layout survives max text; contrast passes in light & dark;
   reduce‑motion honored.
9. **Perceived performance** (H1,H7) — feedback within ~100 ms; skeletons for
   content, spinners only for short waits, progress+cancel beyond ~10 s. *Judge
   the feel here; measure actual timing with `android-app-profiling`.*
10. **Trust & privacy / dark patterns** — see [dark patterns](#dark-patterns).
11. **Retention mechanics** — notifications earned via soft‑ask, deep‑linked,
    frequency‑controlled; easy path to disable/cancel/delete.
12. **Platform conformance** — judge each platform on its own language; see
    [don't force parity](#dont-force-parity).

## Hard numbers

| Thing | Threshold |
|---|---|
| Touch target | 48dp Android / 44pt iOS (uxcheck flags in **dp**). WCAG 2.5.8's 24 **CSS px** floor is web/density-relative — don't apply raw device px on native |
| Text contrast | 4.5:1 normal, 3:1 large (≥18pt/14pt bold) — **by-eye is triage only**; a numeric pass needs a real tool |
| Non‑text/UI contrast | 3:1 (icons, control boundaries) |
| Response time | ≤0.1s instant (no feedback) · ≤1s uninterrupted · ≤10s show progress+cancel |
| Text scaling | usable at max Dynamic Type / ≥200% Android font size |
| Startup (feel) | measure with `android-app-profiling`; vitals: cold excessive ≥5s |

## Dark patterns

Flag FTC's 8 categories: social‑proof pressure, scarcity, urgency, obstruction,
sneaking/hidden info, interface interference, coerced action, asymmetric choice.
Concretely: confirm‑shaming ("No thanks, I'd rather pay full price"),
roach‑motel cancellation, pre‑checked opt‑ins, disguised ads, hidden/drip fees,
trick double‑negatives, and Accept‑prominent/Decline‑buried choices. These are
legal risk (FTC v. Amazon, $2.5B, 2025), not just UX nits — severity 3–4.

## Don't force parity

Share brand, color, type scale, and content; **branch** navigation, controls,
and motion. Do not flag an app for lacking the other platform's idiom:

| Dimension | iOS | Android | Force parity? |
|---|---|---|---|
| Primary nav | bottom tab bar | nav bar / rail | No |
| Back | on‑screen chevron + edge swipe | system + predictive back | No |
| Primary action | nav/tab CTA | FAB | No |
| Date/time | wheel picker | calendar picker | No |
| Depth | Liquid Glass translucency | elevation/shadow | No |
| Dialogs | centered alert / bottom action sheet | centered Material dialog | No |

Users don't notice wrong blur/elevation; they *do* notice a wrong‑feeling back
button or a non‑native picker.

## Release gates

The verdict is the **worst gate**, not a sum of findings — cosmetic volume can't
offset a blocker:

| Gate | Pass condition |
|---|---|
| Core utility | no unresolved S4 in the primary journey |
| Accessibility | core journeys complete with TalkBack; targets/contrast/labels pass |
| Reliability | no data loss, duplicate consequential action, or false success |
| Measurability | success/error/recovery of critical journeys is observable |

## Contrast (why not scripted)

`uxcheck` does **not** compute contrast. Reliable text‑vs‑background luminance on
a black‑box third‑party app is false precision, it would add a Pillow dependency,
and it re‑implements what Google's Accessibility Scanner already does (and even
that labels its screenshot estimate approximate). Judge contrast by eye against
4.5:1 / 3:1, or use a real contrast tool. If a future v2 attempts it, the
least‑wrong shape is: `adb exec-out screencap` RGBA parsed with `struct` (no
Pillow), crop text‑node bounds *from the tree*, bimodal luminance split
discarding anti‑aliased mid‑pixels, and **one‑sided** reporting — flag suspected
fails below ~3:1, never certify a pass.
