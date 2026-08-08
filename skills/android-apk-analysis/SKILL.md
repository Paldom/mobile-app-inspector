---
name: android-apk-analysis
license: MIT
description: Statically analyzes an APK or AAB file on disk (no device) - composition, permissions, exported components, signer identity, and optional decompile, packer, secret, and tracker layers. Use when asked to analyze, inspect, decompile, or security-scan an .apk/.aab file, check its signer, or find hardcoded secrets or trackers. Not for an installed app, live traffic, or the Play listing.
argument-hint: "<path to .apk / .aab / .apks / .xapk>"
---

# android-apk-analysis

Takes apart an Android app **binary on disk**, before it ever runs. It sits
alongside two siblings that answer different questions from different inputs:

| Skill | Input | Question |
| --- | --- | --- |
| **android-apk-analysis** (this) | an `.apk`/`.aab` **file** | what's in the binary, who signed it, is it safe |
| `android-package-diagnostics` | an **installed package** on a device | runtime state of what's installed |
| `android-network-trace` | live traffic / a pcap | what it talks to on the wire |

`scripts/apkscan.py` (standard library) owns an **inventory core** and
**orchestrates** heavier tools only when they're installed — never faking a layer
that didn't run.

## When NOT to use

- An app already installed on a device/emulator → `android-package-diagnostics`.
- What the app contacts at runtime → `android-network-trace` (it also has a
  stdlib URL grep of an APK for the tracing workflow; this skill's `secrets`
  layer is the fuller apkleaks-based hunt).
- Play Store rating/reviews → `play-store-listing`.
- Driving the UI → `android-ui-driver`. iOS `.ipa` → out of scope.

## Setup

The **inventory core** needs only Python 3 + Android SDK build-tools (`aapt2`,
`apksigner`). `bundletool` (brew) is needed for `.aab` input. The optional deep
layers are discovered at runtime; check what you have:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/apkscan.py" tools
```

```
[ok ] aapt2       manifest/permissions
[ok ] apksigner   signer identity
[ok ] jadx        decompile              (brew install jadx)
[MISS] apkid       packer/obfuscator  ->  pipx install apkid
```

## Workflow

```bash
apkscan.py report app.apk                         # inventory + all available layers
apkscan.py report app.apk --layers decompile      # only the layers you want
apkscan.py report app.aab --json                  # AAB: normalized first (see below)
apkscan.py report app.apk --keep                  # keep jadx output etc. for grepping
```

The report has two parts:

1. **Inventory** (always, stdlib + SDK): file SHA-256; composition (dex count,
   native ABIs, sizes); manifest (package, version, min/target SDK, permissions
   with a dangerous-permission flag); **posture** (exported component counts,
   `debuggable`, `allowBackup`, `usesCleartextTraffic`, network-security-config
   presence); signer cert SHA-256 + which signature schemes verify; and a
   **heuristic** packer check (known packer `.so` names).
2. **Layers** (only if the tool is installed): `decompile` (jadx), `packer`
   (apkid), `secrets` (apkleaks), `trackers` (exodus). Each carries a
   `status: ran|skipped|failed`; a skipped layer names the install command.

Read the **caveats** block: it lists exactly why the report is *not* a clean bill
of health — skipped layers, suspected packing, obfuscation limits.

## Output spec

- Text summary or `--json` (keyed by the input file's SHA-256).
- Per layer: `status`, and for skipped ones the reason + install command.
- `analysisCaveats`: the explicit "this is not a clean result because…" list.
- No global `is_safe`/verdict field, and never an empty `secrets: []` when the
  secrets layer didn't run.

## Gotchas

- **Skipped ≠ clean.** If `apkleaks`/`exodus`/`apkid` aren't installed, their
  layers are `skipped`, not "nothing found". The caveats block says so; do not
  report "no secrets/trackers" from a run where those layers never executed.
- **AAB signer is not in the file.** An `.aab` is normalized to a universal APK
  with `bundletool`, which **re-signs it with a debug key** — so signer analysis
  is `not_applicable` for AAB input, not a debug cert. The delivered APK is signed
  downstream (Play App Signing or another store/workflow) and any upload signature
  isn't the delivered one, so the shipped signer **can't be determined from the
  AAB** — get it from an installed copy. (This is a trap: naive tools print
  bundletool's debug cert as "the signer".)
- **`.apks`/`.xapk`/split input** is analyzed via its **base/universal** member,
  not each split separately — the report labels the input kind. For per-split
  detail, unzip and analyze members individually.
- **Universal APK ≠ what Play ships.** It merges all ABIs/densities into one file
  (right for code inventory) but excludes on-demand feature modules and asset
  packs, and its size/hash match nothing a device installs. The report labels the
  input kind and never quotes the derived APK's hash as "the app's hash".
- **jadx exits 0 on garbage.** It completes even on obfuscated code, emitting
  per-method errors; the java-file count is surface reached, not proof of full
  coverage. Run the `packer` layer (or read the heuristic packer field) first —
  if the app is packed, decompiler output is stubs.
- **Native `.so` logic is invisible** to Java decompilation; some apps push
  sensitive code there deliberately. That's a Ghidra/rizin job — see references.
- **Every scanner false-positives.** apkleaks flags test keys and public
  constants; exodus flags library *presence*, not runtime use. Verify before
  reporting.
- **A valid signature is not a trusted signer.** Compare the cert SHA-256 to a
  known-good build; a debug-key signer on a "release" app is a tell.
- **apkid's wheel often fails to build** (`yara-python-dex`); until it installs,
  the stdlib heuristic packer field is the fallback, clearly labelled heuristic.

## Files

- `scripts/apkscan.py` — `report`, `tools`, `--self-test` (offline: composition,
  APK-set extraction, posture/packer parsing, layer degradation — no device, no
  external analysis tool required).
- `references/toolkit.md` — the full static-analysis toolkit (MobSF, Ghidra,
  diffuse, Androguard), the AAB/split model, and the legal boundary.
