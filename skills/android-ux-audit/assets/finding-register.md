# UX audit — <app label> (`<package>`)

## Metadata

| | |
| --- | --- |
| Package / version | `com.example.app` 1.2.3 (code 10203) |
| Platform | Android <API/OS>, Material 3 (judge against this, not iOS) |
| Device | `emulator-5554`, density <dpi>, locale <xx-XX> |
| Journeys audited | first-run → first value; <core task>; <a form>; sign-out/delete |
| Reviewed | YYYY-MM-DD |

## Verdict — worst gate wins (no single score)

| Gate | Pass? | Blocking findings |
| --- | --- | --- |
| Core utility | ✅ / ❌ | F-… |
| Accessibility | ✅ / ❌ | F-… |
| Reliability | ✅ / ❌ | F-… |
| Measurability | ✅ / ❌ | F-… |

Overall = the worst gate. Cosmetic findings below **cannot** change this.

## Coverage

Screens audited (screenshot → screen); and explicitly **not** audited (why):
`<list>`. Settings tested: default + <max font / dark mode / TalkBack?>.

## Findings

One row per finding, most severe first. `evidenceType`: measured (uxcheck) /
observed (screenshot) / suspected (needs human check — cannot fail a gate).

| ID | Screen | Category / heuristic | Sev | Evidence type | Evidence | Recommendation | Effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F-AX-01 | Categories | accessibility / H2 screen-reader | 3 | measured | `uxcheck`: btn #card @[…], no label | Add content-description | S |
| F-INT-02 | Checkout | interaction / H4 target-size | 3 | measured | 20px control below 24px floor | Hit region ≥48dp | S |
| F-FRM-03 | Sign-up | forms / H5,H9 | 4 | observed | 01-signup.png: server error clears the form | Preserve input; specific recoverable error; idempotent retry | M |

Detail any finding that needs it with the full record: user goal, reproduction,
observed vs expected, user impact, principle, reach, confidence, alternatives,
validation method, closure metric.

## Prioritized backlog (severity × effort)

| Priority | Action | Trigger | Effort | Success measure |
| --- | --- | --- | --- | --- |
| P0 | <remove S4 blockers from the primary task> | any S4 | M | primary-task completion at target; no blocker |
| P0 | <repair inaccessible critical controls> | TalkBack can't operate an essential control | M | core journey completes with TalkBack |
| P1 | <targets/labels/contrast fixes> | uxcheck / contrast findings | S | checks pass |
| P2 | <shorten first-run to first value> | forced slides/permissions delay value | M | activation improves |

## Limits

Emulator only (timing/feel not device-grade — measure with android-app-profiling);
one locale/size; contrast judged by eye, not measured; screen-reader flow checked
on the audited journeys only.
