---
name: android-ux-audit
license: MIT
description: Runs a UX/UI heuristic audit of a running Android app - measurable touch-target and unlabelled-control checks from the accessibility tree plus a 12-category rubric scored by Nielsen 0-4 severity. Use when asked to audit UX, usability, or accessibility, evaluate touch targets or heuristics, or score usability problems. Not for a general end-to-end app review, performance, or web pages.
argument-hint: "<what to audit — a screen or a journey>"
---

# android-ux-audit

The UX/UI **heuristic audit step**: judge a running app against a fixed rubric and
produce an evidence-backed, severity-scored finding register. It is more specific
than `android-app-review` (which explores and reports generally) — this applies
the 12-category framework with concrete numeric thresholds and Nielsen severity,
and it is the natural "UX" step inside a larger review.

**Measure what you can, judge the rest.** `scripts/uxcheck.py` computes two
proxy checks from an `android-ui-driver` snapshot — actionable controls with no
accessible name, and touch-target sizes in dp — and emits them as **suspected**
findings with a **provisional priority**; you confirm (TalkBack, hit-area) and the
rubric assigns the final Nielsen severity. The
other categories (first-run, IA, forms, content, states, dark patterns, contrast,
screen-reader flow) are judgment against the rubric in `references/heuristics.md`.

## When NOT to use

- A general "review/QA/explore this app and report" → `android-app-review` (it can
  call this skill for the UX step).
- Startup/jank/memory numbers → `android-app-profiling`.
- Permissions/version/crashes → `android-package-diagnostics`.
- Tapping/reading a screen → `android-ui-driver`. Web-page a11y → a web tool.

**Precedence:** an explicit UX / usability / accessibility request routes here; a
broad "review this app end to end" routes to `android-app-review`, which may
invoke this as its UX step.

## The severity spine

The **rubric** assigns each finding a **Nielsen 0-4 severity** (frequency × impact
× persistence) — `uxcheck` only emits a *provisional* priority (high/medium) as an
input, never a severity. Priority = severity × reach × confidence ÷ effort. Do
**not** collapse findings into one "UX score": the verdict is the **worst release
gate** (core-utility, accessibility, reliability, measurability), and **suspected**
findings trigger REVIEW, not an automatic gate failure. Details in
`references/heuristics.md`.

## Workflow

1. **Scope** 3–5 critical journeys (first-run → first value; the core task; a
   form; sign-out/delete). Note the platform — judge Android against Material 3,
   not iOS parity (`references/heuristics.md` has the don't-force-parity table).

2. **Per screen, capture then measure:**

   ```bash
   uia.py snap --json --all > screen.json     # android-ui-driver
   uia.py shot screen.png                      # evidence for the judgment pass
   uxcheck.py audit screen.json                # density auto-read from the device
   ```

   `uxcheck audit` flags actionable controls with **no accessible name** in the
   snapshot (high) and targets below **48dp** (medium) or well under it (high) —
   as **suspected** signals with the element, bounds, and an acceptance-criterion
   fix. Exit 1 = findings. Confirm each: a name via TalkBack, a target via its
   real hit area (TouchDelegate can differ). Contrast and crowding are manual
   (too noisy / false-precise to automate). Values are dp — there is no raw-px
   "WCAG floor".

3. **Judge the rubric** against the screenshot + snapshot text, per
   `references/heuristics.md`: first-run/permissions, IA/navigation, visual
   hierarchy, forms, microcopy, empty/loading/error states, dark patterns, and —
   by eye, not faked — contrast and the screen-reader flow. Verify the hard
   numbers the tree can't: 4.5:1 text contrast, response-time feel (0.1/1/10s),
   layout at max font size.

4. **Assemble the register** with `assets/finding-register.md`: one row per
   finding (id, screen, category, heuristic, severity, evidence, recommendation,
   effort), then a severity × effort backlog. Every finding cites a screenshot or
   a `uxcheck`/`snap` line.

## Output spec

- A finding register: severity-scored, evidence-backed, with acceptance-criterion
  recommendations ("targets ≥ 48dp", not "buttons feel small").
- Measurable findings (from `uxcheck`) kept **separate** from judgment findings,
  so a clean `uxcheck` is never mistaken for a clean UX.
- A prioritized backlog and an explicit coverage/limits section (what journeys and
  settings were and weren't tested). No single overall score.

## Gotchas

- **`uxcheck` is the measurable subset, not the audit.** A screen with no
  target/label findings can still fail first-run, IA, contrast, or dark-pattern
  checks. The tool says so; don't report "UX is fine" from a clean run.
- **Contrast is triage, not measurement.** `uxcheck` does not compute it; by-eye
  can only *identify suspects* against 4.5:1 / 3:1 — a numeric pass/fail needs a
  real contrast tool or design-token evidence. Never emit a fabricated ratio.
- **The a11y tree ≠ the screen-reader experience.** `uxcheck` finds *unlabelled*
  controls; it can't tell you focus order or announcement quality. The real test
  is completing a core task with TalkBack — a judgment step.
- **Emulator caveat.** Response-time/jank *feel* isn't device-grade on an
  emulator (see `android-app-profiling`); measure timing there, judge it here.
- **Judge each platform on its own language.** Don't flag an Android app for
  lacking an iOS back chevron, or vice versa — the rubric's parity table says
  where divergence is correct.
- **The script emits priority, not severity.** A `<48dp` target is a provisional
  *priority*, not a Nielsen rating — geometry alone can't set severity (it needs
  frequency, reach, task-criticality). Let the rubric assign the final severity.
- **Severity is not finding-count.** The executive line is severity-weighted:
  name the few catastrophes and majors, not the total.
- **Canvas/Flutter screens** expose little to the tree, so `uxcheck` sees few
  elements — that itself is an accessibility finding (poor semantics), and the
  judgment pass must lean on the screenshot.

## Files

- `scripts/uxcheck.py` — `audit` (measurable label + target-size findings),
  `--self-test` (offline: dp math, severity classification — no device).
- `references/heuristics.md` — the 12-category rubric, thresholds, cross-platform
  parity table, dark-pattern list, and release gates.
- `assets/finding-register.md` — the finding-record + backlog template.
