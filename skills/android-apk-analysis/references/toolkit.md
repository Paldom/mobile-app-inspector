# The APK/AAB static-analysis toolkit

`apkscan.py` owns the inventory and orchestrates the common CLI layers. This page
is the wider toolkit it does **not** script — heavier tools worth reaching for,
the AAB/split model, and the legal boundary. Tool facts are mid-2026; verify
versions before relying on a flag.

**Contents:** [Layer tools](#layer-tools) · [Escalations](#escalations) ·
[The AAB / split model](#the-aab--split-model) · [Reading a merged report](#reading-a-merged-report) ·
[Legal boundary](#legal-boundary)

## Layer tools

What `apkscan.py report` runs when present, and what each adds:

| Layer | Tool | Install | Adds |
| --- | --- | --- | --- |
| inventory | aapt2, apksigner | SDK build-tools | package, permissions, posture, signer — always on |
| (AAB norm) | bundletool | `brew install bundletool` | `.aab`/`.apks` → universal APK |
| decompile | jadx | `brew install jadx` | DEX → readable Java; the substrate apkleaks also uses |
| packer | apkid | `pipx install apkid` | packers/obfuscators/anti-analysis (YARA rules) |
| secrets | apkleaks | `pipx install apkleaks` | curated regex hunt for keys/endpoints in decompiled code |
| trackers | exodus-standalone | `docker pull exodusprivacy/exodus-standalone` | third-party tracker/SDK detection by class namespace |

`apkid` frequently fails to `pip`/`pipx` install because its `yara-python-dex`
wheel needs a native build; run it inside MobSF's or exodus's container, or rely
on `apkscan`'s stdlib heuristic packer field until it builds. apkleaks shells out
to jadx, so install jadx first.

## Escalations

Not scripted — reach for these when the layers above aren't enough:

- **MobSF** (`docker pull opensecurity/mobile-security-framework-mobsf`) — the
  all-in-one SAST: manifest/permission mapping, secret and cert analysis, tracker
  and malware indicators, a scorecard, and a REST API for CI. It bundles APKiD.
  Deliberately not a scripted layer here: it's Docker + a REST session + an API
  key, its JSON is hundreds of KB (context poison inline), and it's zero-value on
  the runs where the image isn't pulled. Run it as its own step when you want the
  broad triage report; feed the agent a summary, not the raw JSON.
- **Ghidra** (`brew install --cask ghidra`, needs JDK 21) or **rizin**/**radare2**
  + **Cutter** — for native `.so` under `lib/<abi>/`. Java decompilers see none of
  it, and some apps push sensitive logic there on purpose. `checksec --file=…`
  for NX/PIE/RELRO/canary hardening.
- **Androguard** (`pipx install androguard`) — the Python backbone if you want to
  script custom extraction (call graphs, xrefs, cert objects) beyond aapt2.
- **diffuse** (`brew install JakeWharton/repo/diffuse`) — diff two APK/AAB
  versions (dex, arsc, manifest, method counts, signature): "what changed" and
  "why did it grow".
- **VirusTotal / Koodous** — multi-engine malware lookup by hash. Public tiers are
  rate-limited and non-commercial; fine for a spot check, not bulk.

Avoid the abandoned classics: QARK, AndroBugs, LibRadar/LibScout, the original
dex2jar, and the self-hosted Pithus stack — all stale.

## The AAB / split model

An `.aab` is a *publishing* format, not installable. Play (or bundletool)
generates per-device APK splits from it: `base` + config splits (ABI, density,
language) + optional on-demand feature modules + asset packs.

- **`build-apks --mode=universal`** merges everything installable into one APK —
  right for *code/composition* inventory (all ABIs and code in one place), wrong
  as a claim about delivery: it **excludes on-demand modules and asset packs**, so
  code that really ships can be absent, and its size/hash match no real install.
- The AAB manifest is **protobuf**; `aapt2` can't read it, but
  `bundletool dump manifest --bundle app.aab` renders it as XML.
- To inventory the module/split structure itself, list the `.aab` zip entries
  (`base/`, `<feature>/`) directly — that's what Play delivers as separate pieces.
- Real per-device splits without a device: `bundletool get-device-spec` on a
  synthetic spec, then `build-apks --device-spec` + `extract-apks`. Escalation.

`apkscan` uses universal mode for analysis, labels the input kind, and marks the
signer `not_applicable` for AAB (the universal APK is debug-re-signed).

## Reading a merged report

The failure mode that matters: an agent collapsing one JSON blob into one verdict
and reading *absence of findings* as *safety*. Guard rails the report enforces,
and you should preserve:

- Every layer carries `status: ran|skipped|failed`. A `skipped` layer is a gap,
  never a pass. `apkscan` lists them in `analysisCaveats`.
- No global `is_safe`/score field, and no empty `secrets: []` when the secrets
  layer didn't run.
- Findings keep their tool's provenance and confidence — an `apksigner` cert fact
  and an `apkleaks` regex hit are not the same kind of claim; don't merge them
  into one list or majority-vote across tools.
- Decompile coverage is "surface reached", gated on quality, not on jadx's exit
  code (it returns 0 on garbage). If the packer layer/heuristic flags packing,
  trust that over a clean-looking decompile.

## Legal boundary

Decompiling and reverse-engineering third-party apps is nuanced. US **DMCA
§1201** restricts circumventing technical protection measures, though the
Copyright Office renews a good-faith **security-research exemption** and
interoperability has statutory footing (§1201(f)). App-store and app **EULAs
frequently prohibit reverse engineering** by contract, independent of copyright.
Static analysis for security review, interoperability, and personal/defensive
inspection is the common defensible case — but terms and local law vary; this is
factual background, not legal advice. See `../../docs/authorized-use.md`.
