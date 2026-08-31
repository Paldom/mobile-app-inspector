---
name: android-network-trace
license: MIT
description: Captures an Android app's network traffic on an emulator and reports the hosts it contacts - DNS, TLS SNI and IP peers from a device-wide pcap, static URLs from the APK, plus HTTP(S) bodies via mitmproxy where the app trusts a user CA. Use to find what servers an app talks to, whether it phones home to trackers, or to record its connections, sniff its traffic, or summarise its API calls. Not for the Play listing, permissions, or UI driving.
argument-hint: "<capture | hosts <pcap> | apk-endpoints <apk>>"
---

# android-network-trace

Answers "what does this app talk to?" `scripts/nettrace.py` (standard library)
captures the emulator's traffic and extracts destinations from it. The dependable
path needs no root, no CA, and no app modification — which matters because the
Google Play emulator image is release-signed and cannot be rooted.

```
capture start  ->  drive the app (android-ui-driver)  ->  capture stop  ->  hosts
```

## When NOT to use

- Ratings, reviews, install counts → `play-store-listing`.
- Declared permissions, crashes, memory → `android-package-diagnostics`.
- Tapping/reading the UI → `android-ui-driver`.
- A full narrative review → `android-app-review`.
- Capturing your Mac's own traffic → this is for the emulated device.

## Two honesty boundaries

1. **It is device-wide.** `adb emu network capture` records the whole virtual
   NIC — Play Services, the OS, and every app. It **cannot attribute a
   connection to one package**. Reduce the noise with an idle baseline
   (`--baseline`); for true per-app attribution you need PCAPdroid (VpnService)
   or a rooted UID-aware capture (see references).
2. **It does not decrypt HTTPS by default.** Bodies need a proxy the app trusts.
   On a release-signed Play image you can install only a *user* CA, which Android
   7+ apps ignore, and certificate pinning defeats even a trusted CA. So the
   default deliverable is **destinations, not payloads**. Payload paths are
   documented and best-effort.

## Workflow

Run as `python3 "${CLAUDE_SKILL_DIR}/scripts/nettrace.py" <cmd>`.

1. **Capture** (no relaunch, no root — works on the running emulator):

   ```bash
   nettrace.py capture start run.pcap
   # ... drive the app with android-ui-driver ...
   nettrace.py capture stop
   ```

   For clean attribution, capture an **idle baseline** first (app not started),
   then a run, and subtract. `capture start` warns if Private DNS (DoT) is on —
   that encrypts DNS out of the capture.

2. **Extract hosts** from the pcap:

   ```bash
   nettrace.py hosts run.pcap
   nettrace.py hosts run.pcap --baseline idle.pcap        # subtract the noise
   nettrace.py hosts run.pcap --json
   ```
   ```
   packets=177  tcp=52 udp=60 quic/udp443=0
   hosts contacted (DEVICE-WIDE — not attributable to one app):
     connectivitycheck.gstatic.com  [dns]
     mtalk.google.com  [dns+tls-sni]
   ```

   DNS query names, TLS SNI, and IP:port peers are **three separate kinds of
   evidence**, never merged into a false "complete host list": DNS may be cached
   or encrypted, SNI absent under ECH, and QUIC/direct-IP show no name.

3. **Static endpoints** from the APK — the coverage complement (shows what the
   app *can* reach, including paths a run never triggered):

   ```bash
   # pull the APK first (android-package-diagnostics apk <pkg> --pull ./apks)
   nettrace.py apk-endpoints ./apks/base.apk
   ```

   Extracts `http(s)://` URLs from the APK bytes. Static: it over-reports
   (libraries, dead code) and can't see runtime-built URLs — a lead list, not
   proof of contact.

4. **HTTP(S) bodies — best-effort, only where the app trusts a user CA:**

   ```bash
   nettrace.py proxy set 10.0.2.2:8080        # saves the prior proxy setting
   mitmdump -s "${CLAUDE_SKILL_DIR}/scripts/mitm_capture.py" \
            --set flowout=flows.jsonl --listen-port 8080
   # ... install mitm's USER CA in the AVD, drive the app ...
   nettrace.py proxy clear                     # restores the prior setting exactly
   nettrace.py summarize flows.jsonl           # hosts + endpoints + statuses
   ```

   Most apps ignore the proxy or pin — verified live here (Chrome/WebView routed
   nothing through a global-`http_proxy` proxy). Treat this as a bonus for the
   apps that cooperate, not the plan.

## Output spec

- `hosts`: separate DNS / TLS-SNI / IP-peer lists with per-protocol packet
  counters and a device-wide disclaimer; QUIC/UDP-443 counted and flagged.
- `apk-endpoints`: deduped `http(s)` hosts and literal URLs, labelled static.
- `summarize`: unique hosts and endpoints (path minus query) with status counts.
- Every result names its own limits rather than implying completeness.

## Gotchas

- **`hosts` is device-wide.** Never say "the app contacted X" from a bare
  capture — say "seen during the app run". Use `--baseline` and force-stop other
  apps to tighten it.
- **Private DNS erases the DNS layer.** With DoT on, DNS is encrypted; you still
  get TLS SNI and IP peers. `capture start` warns; turn it off with
  `settings put global private_dns_mode off` for full host visibility.
- **QUIC/HTTP3 hides the host** in an encrypted Initial this tool doesn't parse;
  UDP/443 peers are listed by IP. Block UDP/443 to force apps onto TCP/TLS where
  SNI is visible.
- **The parser is a host lister, not Wireshark.** It reads classic pcap
  (Ethernet/raw-IP — what the emulator writes), single-segment TLS ClientHellos,
  and A/AAAA DNS; it rejects pcapng and unknown link types loudly. For deep
  analysis (reassembly, QUIC, decryption with a keylog) open the pcap in
  Wireshark.
- **A trailing short record is normal** — an emulator pcap is often truncated
  mid-packet unless the emulator exits cleanly; the parser stops cleanly.
- **`proxy set` saves and `proxy clear` restores** the prior `http_proxy` — a
  crash mid-run can leave the device proxied, so always `clear`. The mitm addon
  redacts auth/cookie headers and omits bodies unless you pass `--set
  bodies=true`; flows still contain sensitive data — treat them as credentials.
- **Getting bodies without root** on a non-pinned app: repackage the APK to trust
  a user CA (`references/deeper-capture.md`). Pinned / Play-Integrity / banking
  apps need a rooted research image — documented, not scripted.

## Files

- `scripts/nettrace.py` — `capture`, `hosts`, `apk-endpoints`, `proxy`,
  `summarize`, `--self-test` (offline: crafted pcap, malformed input, DNS
  compression, APK URL extraction — no device, adb, or ffmpeg).
- `scripts/mitm_capture.py` — mitmproxy addon writing JSONL for `summarize`
  (metadata-only by default; redacts auth headers).
- `references/deeper-capture.md` — no-root APK-repackage MITM, PCAPdroid per-app
  attribution, and the rooted-image + Frida escalation, with their trade-offs.
