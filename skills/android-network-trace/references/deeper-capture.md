# Getting more than destinations

The default skill gives you **hosts** (DNS/SNI/IP) with no root, no CA, no app
change — the 80% answer on a non-rootable Play image. This page is the honest map
of what it takes to get more (request/response bodies, per-app attribution), why
each step costs what it does, and where it simply stops. These paths are
**documented, not scripted**: they need extra tools and heavy per-app tuning, and
several are ToS-sensitive — run them only under `../../docs/authorized-use.md`.

**Contents:** [The wall](#the-wall) · [Per-app attribution](#per-app-attribution) ·
[Bodies without root](#bodies-without-root-apk-repackage) ·
[The -http-proxy flag](#the-http-proxy-launch-flag) ·
[Rooted research image](#rooted-research-image) · [What always stops you](#what-always-stops-you)

## The wall

Since Android 7, apps trust only the **system** CA store, not user-added CAs,
unless their `network_security_config` opts in — which you can't edit on a
third-party app without changing the app. So to read HTTPS bodies you must either
(a) get your interception CA into the *system* store (needs root), or (b) make
the app itself trust your CA (needs repackaging), and then also (c) defeat any
certificate pinning. The Play image gives you none of (a) — it's release-signed,
`adb root` fails (reproduced). That's the whole reason the default skill stops at
destinations.

## Per-app attribution

A device-wide pcap can't say which package made a connection. Two ways forward:

- **Baseline subtraction (in-skill, no extra tools).** Capture idle (target not
  started), capture the run, `hosts run.pcap --baseline idle.pcap`, and
  force-stop other apps during the run. Cheap, approximate — it removes the
  steady background (Play Services, connectivity checks), not concurrent noise.
- **PCAPdroid (no root, real per-UID).** An on-device app using `VpnService` to
  capture locally, attributed per application, with PCAP/PCAPNG export — and it
  attaches to a running emulator, unlike launch-only `-tcpdump`. Install it, grant
  the VPN consent (one-time UI approval; `appops set <pkg> ACTIVATE_VPN allow`
  may skip it — verify on your API level), select the target app, capture. Its
  mitm addon can also decrypt TLS for apps that trust a user CA (same Android 7+
  limit). Trade-off: routes traffic through a local VPN so interface/port info
  shifts, and it's another app on the device.

## Bodies without root: APK repackage

The highest-value no-root way to read HTTPS bodies — it removes the "you need a
rooted image" step for apps that **don't pin**. You make the app trust a user CA
by editing its own network-security config, then re-sign and reinstall.

```bash
# 1. get the APK(s): android-package-diagnostics apk <pkg> --pull ./apks
# 2. patch + re-sign in one shot (handles the NSC edit, pinning-config strip, resign)
apk-mitm ./apks/base.apk                    # npm i -g apk-mitm ; needs Java
# for split APKs, apk-mitm takes the .apks/.xapk bundle; plain multi-file often fails
# 3. install the patched build (uninstall the original first — different signer)
adb uninstall <pkg>
adb install ./apks/base-patched.apk
# 4. now the app trusts a user CA -> nettrace.py proxy set + mitmdump decodes bodies
```

Costs and limits, all real:
- Needs `apk-mitm` (Node) + Java + the SDK's `apksigner`/`zipalign`.
- **Breaks on**: split-APK bundles (signature-inconsistent installs), server-side
  integrity/Play Integrity checks, and native/custom-TLS stacks `apk-mitm` can't
  rewrite.
- The result is a **resigned, modified** app — not the store build, and a
  distribution/ToS hazard. Keep it on a disposable emulator; never distribute it.
- For pinning that survives the config strip, `objection patchapk` injects a
  Frida gadget instead — same install caveats.

## The -http-proxy launch flag

`emulator -http-proxy 10.0.2.2:8080` tunnels the device's TCP *below* Android, so
proxy-ignoring apps can't opt out the way they ignore the global `http_proxy`
setting, and even undecryptable flows leave CONNECT lines (host + port). It is
**launch-only** — you can't set it on a running emulator — which conflicts with
this repo's running-emulator model, so it's an alternative, not the default.
HTTPS bodies still need CA trust; this only improves *which* connections you see.

## Rooted research image

When the app pins or ignores user CAs and you're authorized to go deeper, use a
**separate** image — never the Play baseline:

- A `google_apis` or AOSP AVD supports `adb root` + `-writable-system`, or root a
  Play image with rootAVD + Magisk (then a Magisk module like Cert-Fixer promotes
  the user CA to system at boot; Android 14/15 moved the store to
  `/apex/com.android.conscrypt/cacerts`, so old push-to-cacerts recipes are dead).
- Push an arch-matched `frida-server` (arm64 on Apple Silicon), run it as root,
  and inject an unpinning script (objection `android sslpinning disable`, or
  httptoolkit's `frida-interception-and-unpinning`).
- Keep this image isolated: installing a system CA widens the whole device's trust
  boundary, so it must hold no personal accounts and be wiped after.
- Capture the unmodified Play-image baseline **first**, so rooted/instrumented
  results are never mistaken for production behavior.

## What always stops you

Some apps are un-interceptable on an emulator by design, and the right move is to
document that, not burn hours:
- **Play Integrity / hardware attestation** (banking, DRM, anti-cheat): emulators
  can't pass strong integrity, and Google's 2025 hardware-backed attestation makes
  spoofing unreliable. These apps often refuse to run at all.
- **FLAG_SECURE + pinning + native TLS** stacked together.
- **QUIC/HTTP3-only** endpoints when you can't force TCP fallback.
- **ECH**, which hides even the SNI.

Report the limitation as a finding. "This app pins and enforces Play Integrity, so
its traffic can't be decrypted on an emulator" is a legitimate, useful result.
