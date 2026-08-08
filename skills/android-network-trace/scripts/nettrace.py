#!/usr/bin/env python3
"""See what an Android app talks to. Standard library only.

The dependable, no-root path on a Google Play (non-rootable) emulator:

    capture start  ->  drive the app  ->  capture stop  ->  hosts

`capture` uses `adb emu network capture`, which records the emulator's virtual
NIC to a pcap on a RUNNING emulator (no relaunch, no root, no CA). `hosts` then
extracts, from that pcap, the DNS query names, TLS SNI server names, and IP:port
peers — three SEPARATE kinds of evidence, never merged into a false "complete"
list.

Two honesty boundaries this tool will not cross:
  * It is DEVICE-WIDE. The capture includes Play Services, the OS, and every
    other app — it cannot attribute a connection to one package. Use an idle
    baseline (`hosts --baseline idle.pcap run.pcap`) to subtract the noise.
  * It does NOT decrypt HTTPS. Bodies need a proxy the app trusts; on a
    release-signed Play image you can install only a USER CA, which Android 7+
    apps ignore, and pinning defeats even a trusted CA. See references.

For request/response bodies where the app DOES trust a user CA, `proxy set`
points the device at mitmproxy (saving and later restoring the prior setting),
and `summarize` distils a mitmdump capture. That path is best-effort.

Exit codes: 0 ok | 1 nothing found / unusable input | 2 usage or device error.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys

ADB_TIMEOUT = 60
MAX_PACKETS = 500_000          # backstop against a huge/hostile pcap
MAX_NAME = 253                 # DNS name length ceiling
HOSTNAME_RE = re.compile(rb"^[A-Za-z0-9._-]{1,253}$")


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def find_adb() -> str:
    exe = shutil.which("adb")
    if exe:
        return exe
    for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.path.expanduser("~/Library/Android/sdk"),
                 os.path.expanduser("~/Android/Sdk")):
        if root:
            cand = os.path.join(root, "platform-tools", "adb")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    die("adb not found. Install Android platform-tools or set ANDROID_HOME.")


class Device:
    def __init__(self, serial: str | None):
        self.adb = find_adb()
        self.serial = serial or os.environ.get("ANDROID_SERIAL") or self._pick()

    def _pick(self) -> str:
        out = subprocess.run([self.adb, "devices"], capture_output=True,
                             text=True, timeout=ADB_TIMEOUT).stdout
        devs = [l.split("\t")[0] for l in out.splitlines()[1:]
                if l.strip() and l.endswith("\tdevice")]
        if not devs:
            die("no device attached.", 1)
        if len(devs) > 1:
            die(f"{len(devs)} devices attached: {', '.join(devs)}. Pass --serial.")
        if not devs[0].startswith("emulator-"):
            die(f"{devs[0]} is not an emulator. `adb emu network capture` is an "
                f"emulator console command; on a physical device use PCAPdroid "
                f"(VpnService) instead. See references/deeper-capture.md.", 2)
        return devs[0]

    def emu(self, *args, timeout=ADB_TIMEOUT) -> str:
        r = subprocess.run([self.adb, "-s", self.serial, "emu", *args],
                           capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()

    def shell(self, *args, timeout=ADB_TIMEOUT) -> str:
        quoted = [shlex.quote(str(a)) for a in args]  # runs on the DEVICE shell
        r = subprocess.run([self.adb, "-s", self.serial, "shell", *quoted],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").replace("\r\n", "\n")


# ---------------------------------------------------------------- pcap parsing

PCAP_LE = b"\xd4\xc3\xb2\xa1"
PCAP_BE = b"\xa1\xb2\xc3\xd4"
PCAP_LE_NS = b"\x4d\x3c\xb2\xa1"
PCAP_BE_NS = b"\xa1\xb2\x3c\x4d"
PCAPNG = b"\x0a\x0d\x0d\x0a"


def parse_pcap(path: str):
    """Yield (linktype, frame_bytes). Fails closed on formats it does not own —
    it is a best-effort host lister, not a forensic reassembler."""
    try:
        data = open(path, "rb").read()
    except OSError as e:
        die(f"cannot read {path}: {e}")
    if len(data) < 24:
        die(f"{path} is too small to be a pcap ({len(data)} bytes)", 1)
    magic = data[:4]
    if magic == PCAPNG:
        die(f"{path} is pcapng, which this parser does not read. Convert it: "
            f"`tshark -F pcap -r in.pcapng -w out.pcap`, or capture with "
            f"`adb emu network capture` which writes classic pcap.", 1)
    if magic in (PCAP_LE, PCAP_LE_NS):
        endian = "<"
    elif magic in (PCAP_BE, PCAP_BE_NS):
        endian = ">"
    else:
        die(f"{path} is not a pcap file (magic {magic.hex()})", 1)
    linktype = struct.unpack(endian + "I", data[20:24])[0]
    off, n = 24, 0
    while off + 16 <= len(data):
        if n >= MAX_PACKETS:
            yield linktype, b"", False          # capped: results are incomplete
            return
        incl = struct.unpack(endian + "I", data[off + 8:off + 12])[0]
        off += 16
        if incl == 0 or off + incl > len(data) or incl > 262_144:
            # truncated or absurd record length: stop and mark incomplete. Emulator
            # pcaps cut mid-packet land here — honest to flag, not an error.
            yield linktype, b"", False
            return
        yield linktype, data[off:off + incl], True
        off += incl
        n += 1


def _l4(linktype: int, frame: bytes):
    """(l4proto, l3proto, payload, src, dst) or None. Bounded, no reassembly."""
    if linktype == 1:            # Ethernet
        if len(frame) < 14:
            return None
        etype = int.from_bytes(frame[12:14], "big")
        rest = frame[14:]
        if etype == 0x8100 and len(rest) >= 4:   # one VLAN tag
            etype = int.from_bytes(rest[2:4], "big")
            rest = rest[4:]
    elif linktype == 101:        # raw IP
        rest = frame
        etype = 0x0800 if (rest and rest[0] >> 4 == 4) else 0x86DD
    else:
        return "UNSUPPORTED_LINKTYPE", linktype, b"", None, None

    if etype == 0x0800:          # IPv4
        if len(rest) < 20:
            return None
        ihl = (rest[0] & 0x0F) * 4
        proto = rest[9]
        src = str(ipaddress.IPv4Address(rest[12:16]))
        dst = str(ipaddress.IPv4Address(rest[16:20]))
        return proto, 4, rest[ihl:], src, dst
    if etype == 0x86DD:          # IPv6 (base header only; extension chains skipped)
        if len(rest) < 40:
            return None
        proto = rest[6]
        src = str(ipaddress.IPv6Address(rest[8:24]))
        dst = str(ipaddress.IPv6Address(rest[24:40]))
        return proto, 6, rest[40:], src, dst
    return None


def _dns_name(payload: bytes, pos: int) -> tuple[str, int]:
    """Parse a DNS name with compression. Bounded against pointer loops."""
    labels, jumps, cur, end_after = [], 0, pos, None
    while cur < len(payload):
        length = payload[cur]
        if length == 0:
            cur += 1
            break
        if length & 0xC0 == 0xC0:       # compression pointer
            if cur + 1 >= len(payload):
                break
            if end_after is None:
                end_after = cur + 2
            cur = ((length & 0x3F) << 8) | payload[cur + 1]
            jumps += 1
            if jumps > 32:              # loop guard
                break
            continue
        cur += 1
        if cur + length > len(payload) or length > 63:
            break
        labels.append(payload[cur:cur + length])
        cur += length
        if sum(len(l) for l in labels) > MAX_NAME:
            break
    name = b".".join(labels)
    return (name.decode("ascii", "replace") if name else ""), (end_after or cur)


def _dns_queries(payload: bytes) -> list[str]:
    if len(payload) < 12:
        return []
    qd = int.from_bytes(payload[4:6], "big")
    names, pos = [], 12
    for _ in range(min(qd, 32)):
        name, pos = _dns_name(payload, pos)
        pos += 4  # qtype + qclass
        if name and HOSTNAME_RE.match(name.encode()):
            names.append(name.lower())
    return names


def _tls_sni(payload: bytes) -> str | None:
    """Server name from a ClientHello at the start of this TCP payload. Single
    segment only: a ClientHello split across TCP segments is not reassembled
    (documented limitation)."""
    p = payload
    if len(p) < 45 or p[0] != 0x16 or p[1] != 0x03:   # TLS handshake record
        return None
    if p[5] != 0x01:                                   # ClientHello
        return None
    try:
        i = 43                          # record(5)+hs(4)+ver(2)+random(32)
        sid = p[i]; i += 1 + sid        # session id
        cs = int.from_bytes(p[i:i + 2], "big"); i += 2 + cs   # cipher suites
        cm = p[i]; i += 1 + cm          # compression methods
        i += 2                          # extensions length
        while i + 4 <= len(p):
            etype = int.from_bytes(p[i:i + 2], "big")
            elen = int.from_bytes(p[i + 2:i + 4], "big")
            i += 4
            if etype == 0x0000:         # server_name
                # server_name_list(2) + type(1) + name_len(2) + name
                nlen = int.from_bytes(p[i + 3:i + 5], "big")
                name = p[i + 5:i + 5 + nlen]
                if HOSTNAME_RE.match(name):
                    return name.decode("ascii", "replace").lower()
                return None
            i += elen
    except (IndexError, struct.error):
        return None
    return None


def scan(path: str) -> dict:
    dns, sni = {}, {}
    peers = {}
    stats = {"packets": 0, "udp": 0, "tcp": 0, "quic": 0, "other_l4": 0,
             "unsupported_linktype": None, "complete": True}
    for linktype, frame, complete in parse_pcap(path):
        stats["complete"] = complete
        if not frame:
            continue                            # completeness sentinel, not a packet
        stats["packets"] += 1
        r = _l4(linktype, frame)
        if not r:
            continue
        if r[0] == "UNSUPPORTED_LINKTYPE":
            stats["unsupported_linktype"] = r[1]
            die(f"{path} has link-layer type {r[1]}, which this parser does not "
                f"handle (it reads Ethernet and raw-IP captures, which is what "
                f"`adb emu network capture` and emulator -tcpdump produce).", 1)
        proto, _ver, l4, src, dst = r
        if proto == 17:            # UDP
            stats["udp"] += 1
            sport = int.from_bytes(l4[0:2], "big") if len(l4) >= 2 else 0
            dport = int.from_bytes(l4[2:4], "big") if len(l4) >= 4 else 0
            if 53 in (sport, dport):
                for name in _dns_queries(l4[8:]):
                    dns[name] = dns.get(name, 0) + 1
            elif 443 in (sport, dport):
                stats["quic"] += 1     # UDP/443 is very likely QUIC/HTTP3
                peers[f"{dst}:{dport}"] = peers.get(f"{dst}:{dport}", 0) + 1
        elif proto == 6:           # TCP
            stats["tcp"] += 1
            if len(l4) >= 13:
                doff = (l4[12] >> 4) * 4
                payload = l4[doff:]
                dport = int.from_bytes(l4[2:4], "big")
                if payload:
                    name = _tls_sni(payload)
                    if name:
                        sni[name] = sni.get(name, 0) + 1
                peers[f"{dst}:{dport}"] = peers.get(f"{dst}:{dport}", 0) + 1
        else:
            stats["other_l4"] += 1
    return {"dns": dns, "sni": sni, "peers": peers, "stats": stats}


# ---------------------------------------------------------------- static extraction

URL_RE = re.compile(rb"https?://([A-Za-z0-9.-]{2,253})([/:][\x21-\x7e]{0,200})?")
# domains that are framework/asset noise, not the app's own endpoints
NOISE = ("schemas.android.com", "www.w3.org", "xmlpull.org", "java.sun.com",
         "goo.gl", "example.com", "googleapis.com/robots", "w3.org")


def extract_endpoints(apk_path: str) -> dict:
    """Regex hostnames/URLs out of an APK's bytes (dex, resources, manifest).
    Static coverage complement: shows endpoints the app CAN reach, including ones
    a dynamic capture never triggered. It over-reports (dead code, libraries) and
    cannot see runtime-built or obfuscated URLs — a lead list, not ground truth."""
    import zipfile
    urls, hosts = {}, {}
    try:
        zf = zipfile.ZipFile(apk_path)
    except (OSError, zipfile.BadZipFile) as e:
        die(f"{apk_path} is not a readable APK (zip): {e}", 1)
    scanned, total = 0, 0
    MAX_TOTAL = 512 * 1024 * 1024        # cap total decompressed bytes read
    with zf:
        for info in zf.infolist()[:20000]:            # cap member count
            if info.file_size > 64 * 1024 * 1024:      # skip huge members
                continue
            if total + info.file_size > MAX_TOTAL:
                break                                  # decompression-bomb backstop
            if not (info.filename.endswith((".dex", ".xml", ".arsc", ".json"))
                    or info.filename.startswith(("res/", "assets/"))):
                continue
            try:
                blob = zf.read(info)
            except (OSError, zipfile.BadZipFile):
                continue
            total += len(blob)
            scanned += 1
            # Only full http(s):// URLs. A bare-hostname regex on dex bytes is a
            # firehose of reversed-domain package names (androidx.compose,
            # io.ktor...) indistinguishable from hosts — URLs are the high-signal
            # part and are what an app actually calls.
            for m in URL_RE.finditer(blob):
                host = m.group(1).decode("ascii", "replace").lower().rstrip(".")
                url = m.group(0).decode("ascii", "replace").rstrip("\"'<>)(];,")
                if host and "." in host and not any(host.endswith(n) or n in url
                                                    for n in NOISE):
                    urls[url[:200]] = urls.get(url[:200], 0) + 1
                    hosts[host] = hosts.get(host, 0) + 1
    return {"urls": urls, "hosts": hosts, "entriesScanned": scanned}


def cmd_apk_endpoints(dev, a) -> int:
    res = extract_endpoints(a.apk)
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    hosts = sorted(res["hosts"])
    print(f"scanned {res['entriesScanned']} apk entries; {len(hosts)} candidate hosts "
          f"(STATIC — over-reports libraries/dead code; not proof of contact):")
    for h in hosts:
        print(f"  {h}")
    if res["urls"]:
        print(f"\n{len(res['urls'])} literal URLs (top 20):")
        for u, c in sorted(res["urls"].items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {c:3}  {u}")
    return 0 if hosts else 1


# ---------------------------------------------------------------- commands


def cmd_capture(dev: Device, a) -> int:
    if a.action == "start":
        pdns = dev.shell("settings", "get", "global", "private_dns_mode").strip()
        if pdns and pdns not in ("off", "null"):
            print(f"warning: Private DNS is {pdns!r} — DNS queries are encrypted "
                  f"(DoT) and will NOT appear in the capture. Turn it off for full "
                  f"host visibility:  adb shell settings put global "
                  f"private_dns_mode off", file=sys.stderr)
        path = os.path.abspath(a.path or "capture.pcap")
        out = dev.emu("network", "capture", "start", path)
        if "OK" not in out:
            die(f"could not start capture: {out}", 1)
        print(f"capturing (device-wide) -> {path}\n"
              f"drive the app now, then: nettrace.py capture stop")
        return 0
    out = dev.emu("network", "capture", "stop")
    if "OK" not in out:
        die(f"could not stop capture: {out} (was one running?)", 1)
    print("capture stopped")
    return 0


def cmd_hosts(dev, a) -> int:
    res = scan(a.pcap)
    base = set()
    if a.baseline:
        b = scan(a.baseline)
        base = set(b["dns"]) | set(b["sni"])
    dns = {k: v for k, v in res["dns"].items() if k not in base}
    sni = {k: v for k, v in res["sni"].items() if k not in base}

    if a.json:
        out = {"dnsQueries": dns, "tlsServerNames": sni,
               "ipPeers": res["peers"], "stats": res["stats"],
               "baselineSubtracted": bool(a.baseline)}
        if a.baseline:                          # keep raw sets too, not just the diff
            out["dnsQueriesRaw"] = res["dns"]
            out["tlsServerNamesRaw"] = res["sni"]
        print(json.dumps(out, indent=1))
        return 0
    s = res["stats"]
    print(f"packets={s['packets']}  tcp={s['tcp']} udp={s['udp']} "
          f"quic/udp443={s['quic']}"
          + ("  (baseline subtracted)" if a.baseline else "")
          + ("" if s["complete"] else "  INCOMPLETE(capture truncated or capped)"))
    names = sorted(set(dns) | set(sni))
    if names:
        print(f"\nhosts contacted (DEVICE-WIDE — not attributable to one app):")
        for n in names:
            tags = []
            if n in dns:
                tags.append("dns")
            if n in sni:
                tags.append("tls-sni")
            print(f"  {n}  [{'+'.join(tags)}]")
    else:
        print("\nno hostnames recovered.")
    if s["quic"]:
        print(f"\nnote: {s['quic']} UDP/443 packets are QUIC/HTTP3 — their host is "
              f"in the encrypted QUIC Initial, which this tool does not parse. "
              f"IP peers are listed; block UDP/443 to force apps onto TCP/TLS.")
    print(f"\n{len(res['peers'])} IP:port peers seen (a CDN IP may serve many hosts; "
          f"direct-IP or ECH traffic shows no name). `hosts --json` lists them.")
    return 0 if names or res["peers"] else 1


# Android keeps the HTTP proxy in TWO representations: the composite `http_proxy`
# ("host:port") that the connectivity stack reads, AND the split
# `global_http_proxy_host`/`_port`/`_exclusion_list` keys. Clearing only the
# composite leaves the split keys routing traffic (a real split-brain — observed
# breaking an app after a capture). Manage all of them so set/clear are symmetric.
AUX_PROXY_KEYS = ("global_http_proxy_host", "global_http_proxy_port",
                  "global_http_proxy_exclusion_list")


def _get(dev: Device, key: str) -> str:
    return dev.shell("settings", "get", "global", key).strip()


def _put_or_delete(dev: Device, key: str, val: str) -> None:
    if val and val.lower() != "null":
        dev.shell("settings", "put", "global", key, val)
    else:
        dev.shell("settings", "delete", "global", key)


def cmd_proxy(dev: Device, a) -> int:
    statefile = _proxy_state(dev.serial)   # keyed to the ADB serial (multi-emulator safe)
    if a.action == "set":
        prior = _get(dev, "http_proxy")
        prior_aux = {k: _get(dev, k) for k in AUX_PROXY_KEYS}
        try:
            # O_EXCL: if state already exists, a proxy is already set by this tool.
            # Refuse to nest, so `clear` never restores a value the tool itself set.
            fd = os.open(statefile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            die(f"this tool already set a proxy on {dev.serial} "
                f"(state: {statefile}). `proxy clear` it before setting again.", 2)
        with os.fdopen(fd, "w") as fh:
            json.dump({"prior": prior, "applied": a.hostport, "prior_aux": prior_aux}, fh)
        # Clear the split keys so they can't override the composite we set.
        for k in AUX_PROXY_KEYS:
            dev.shell("settings", "delete", "global", k)
        dev.shell("settings", "put", "global", "http_proxy", a.hostport)
        print(f"proxy set to {a.hostport} (prior saved: {prior or '<none>'})")
        print("start mitmproxy on the host, e.g.:\n"
              f"  mitmdump -s \"${{CLAUDE_SKILL_DIR}}/scripts/mitm_capture.py\" "
              f"--set flowout=flows.jsonl --listen-port "
              f"{a.hostport.rsplit(':', 1)[-1]}")
        print("NOTE: HTTPS bodies decode only where the app trusts a USER CA AND "
              "does not pin (Android 7+ ignores user CAs by default); many apps "
              "ignore the proxy entirely. Best-effort — the pcap path is the default.")
        return 0
    # clear -> restore the saved prior, but only if nobody changed it since
    if not os.path.exists(statefile):
        print("no proxy state for this device — nothing to clear.")
        return 0
    try:
        st = json.load(open(statefile))
    except ValueError:
        st = {"prior": "", "applied": None}
    current = _get(dev, "http_proxy")
    if st.get("applied") and current not in ("", "null", st["applied"]):
        print(f"warning: http_proxy is now {current!r}, not the {st['applied']!r} this "
              f"tool set — something else changed it; leaving it as-is.", file=sys.stderr)
        os.remove(statefile)
        return 0
    prior = (st.get("prior") or "").strip()
    if prior and prior.lower() != "null":
        dev.shell("settings", "put", "global", "http_proxy", prior)
        print(f"proxy restored to prior value: {prior}")
    else:
        dev.shell("settings", "delete", "global", "http_proxy")
        print("proxy cleared (no prior value to restore)")
    # Restore the split keys we saved (deleting any we cleared) — this is the half
    # the old code missed, which left global_http_proxy_host/port routing traffic.
    for k in AUX_PROXY_KEYS:
        _put_or_delete(dev, k, (st.get("prior_aux") or {}).get(k, ""))
    os.remove(statefile)
    return 0


def _proxy_state(serial: str) -> str:
    import tempfile
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", serial or "default")
    return os.path.join(tempfile.gettempdir(), f"nettrace-proxy-{safe}.json")


def cmd_summarize(dev, a) -> int:
    """Distil the mitmdump JSONL from the bundled addon into hosts+endpoints."""
    try:
        lines = [json.loads(l) for l in open(a.flows) if l.strip()]
    except OSError as e:
        die(f"cannot read {a.flows}: {e}", 1)
    except ValueError as e:
        die(f"{a.flows} is not the JSONL this skill's mitm addon writes: {e}", 1)
    hosts, endpoints = {}, {}
    for r in lines:
        h = r.get("host", "?")
        hosts[h] = hosts.get(h, 0) + 1
        key = f"{r.get('method', '?')} {h}{(r.get('path') or '').split('?')[0]}"
        ep = endpoints.setdefault(key, {"count": 0, "statuses": {}})
        ep["count"] += 1
        st = str(r.get("status", "?"))
        ep["statuses"][st] = ep["statuses"].get(st, 0) + 1
    if a.json:
        print(json.dumps({"hosts": hosts, "endpoints": endpoints,
                          "flows": len(lines)}, indent=1))
        return 0
    print(f"{len(lines)} flows across {len(hosts)} hosts:")
    for h, c in sorted(hosts.items(), key=lambda kv: -kv[1]):
        print(f"  {c:4}  {h}")
    print(f"\n{len(endpoints)} endpoints (path without query):")
    for key, ep in sorted(endpoints.items(), key=lambda kv: -kv[1]["count"])[:40]:
        st = ",".join(f"{k}×{v}" for k, v in ep["statuses"].items())
        print(f"  {ep['count']:4}  {key}  [{st}]")
    return 0 if lines else 1


# ---------------------------------------------------------------- self-test


def _build_pcap(records: list[bytes]) -> bytes:
    out = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 262144, 1)
    for frame in records:
        out += struct.pack("<IIII", 0, 0, len(frame), len(frame)) + frame
    return out


def _eth(payload: bytes, etype: int) -> bytes:
    return b"\x00" * 6 + b"\x11" * 6 + etype.to_bytes(2, "big") + payload


def _ipv4(proto: int, l4: bytes, dst="93.184.216.34") -> bytes:
    hdr = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 64, proto, 0, 0]) + \
        bytes(int(x) for x in "10.0.2.16".split(".")) + \
        bytes(int(x) for x in dst.split("."))
    return _eth(hdr + l4, 0x0800)


def self_test() -> int:
    import tempfile
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    # DNS query for example.com over UDP/53
    dnsq = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x07example\x03com\x00\x00\x01\x00\x01")
    udp = struct.pack(">HHHH", 50000, 53, 8 + len(dnsq), 0) + dnsq
    # DNS with a compression pointer (name = api.example.com pointing back)
    dnsc = (b"\xAB\xCD\x01\x00\x00\x02\x00\x00\x00\x00\x00\x00"  # QDCOUNT=2
            b"\x07example\x03com\x00\x00\x01\x00\x01"       # Q1 name @12
            b"\x03api\xC0\x0C\x00\x01\x00\x01")             # Q2 api -> ptr to @12
    udpc = struct.pack(">HHHH", 50001, 53, 8 + len(dnsc), 0) + dnsc
    # TLS ClientHello with SNI = test.example.org
    host = b"test.example.org"
    sni_ext = b"\x00\x00" + (len(host) + 5).to_bytes(2, "big") + \
        (len(host) + 3).to_bytes(2, "big") + b"\x00" + len(host).to_bytes(2, "big") + host
    body = b"\x03\x03" + b"\x00" * 32 + b"\x00" + b"\x00\x02\x00\x2f" + b"\x01\x00" + \
        len(sni_ext).to_bytes(2, "big") + sni_ext
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    rec = b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs
    tcp = struct.pack(">HHIIBBHHH", 40000, 443, 0, 0, (5 << 4), 0, 0, 0, 0) + rec

    pcap = _build_pcap([_ipv4(17, udp), _ipv4(17, udpc), _ipv4(6, tcp)])
    with tempfile.NamedTemporaryFile("wb", suffix=".pcap", delete=False) as fh:
        fh.write(pcap); path = fh.name
    try:
        res = scan(path)
        check("DNS query name extracted", "example.com" in res["dns"])
        check("compressed DNS name resolved", "api.example.com" in res["dns"])
        check("TLS SNI extracted", "test.example.org" in res["sni"])
        check("TCP peer recorded", any(p.endswith(":443") for p in res["peers"]))
        check("packet count correct", res["stats"]["packets"] == 3)
    finally:
        os.remove(path)

    # malformed / hostile inputs must fail closed, not hang or crash
    with tempfile.NamedTemporaryFile("wb", suffix=".pcap", delete=False) as fh:
        fh.write(b"\xd4\xc3\xb2\xa1" + b"\x00" * 20 +
                 struct.pack("<IIII", 0, 0, 99999, 99999) + b"\x16\x03"); trunc = fh.name
    try:
        r = scan(trunc)  # truncated record -> stop cleanly AND flag incomplete
        check("truncated pcap does not crash", True)
        check("truncated pcap flagged incomplete", r["stats"]["complete"] is False)
    except SystemExit:
        check("truncated pcap does not crash", True)
    except Exception:
        check("truncated pcap does not crash", False)
    finally:
        os.remove(trunc)

    # pointer-loop DNS name must terminate
    loop = b"\xC0\x0C" + b"\x00" * 4
    try:
        _dns_name(b"\x00" * 12 + loop, 12)
        check("DNS pointer loop terminates", True)
    except Exception:
        check("DNS pointer loop terminates", False)

    # flow summarizer
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(json.dumps({"host": "api.x.com", "method": "GET",
                             "path": "/v1/u?token=secret", "status": 200}) + "\n")
        fh.write(json.dumps({"host": "api.x.com", "method": "GET",
                             "path": "/v1/u?token=other", "status": 200}) + "\n")
        flows = fh.name

    class A:
        pass
    a = A(); a.flows = flows; a.json = True
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_summarize(None, a)
    out = json.loads(buf.getvalue())
    os.remove(flows)
    check("summarizer counts hosts", out["hosts"]["api.x.com"] == 2)
    check("summarizer strips query string from endpoint key",
          any("token" not in k for k in out["endpoints"]) and
          all("?" not in k for k in out["endpoints"]))

    # apk-endpoints: URL extraction from a zip (an APK is a zip)
    import zipfile
    with tempfile.NamedTemporaryFile("wb", suffix=".apk", delete=False) as fh:
        apk = fh.name
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("classes.dex", b"junk https://api.real.example/v1/x?k=1 more "
                                  b"androidx.compose.foundation io.ktor.client "
                                  b"http://cdn.real.example/a.png tail")
        z.writestr("res/values/strings.xml", b"<s>https://track.real.example/e</s>")
    try:
        e = extract_endpoints(apk)
        check("apk URL host extracted", "api.real.example" in e["hosts"])
        check("apk second-file host extracted", "track.real.example" in e["hosts"])
        check("apk http host extracted", "cdn.real.example" in e["hosts"])
        check("apk does NOT emit package names as hosts",
              "androidx.compose.foundation" not in e["hosts"]
              and "io.ktor.client" not in e["hosts"])
    finally:
        os.remove(apk)

    # proxy set/clear must manage BOTH the composite http_proxy and the split
    # global_http_proxy_* keys, or clear leaves a split-brain that breaks the app.
    class FakeDev:
        serial = "selftest-proxy"

        def __init__(self, store):
            self.store = store

        def shell(self, *args, timeout=None):
            if args[:3] == ("settings", "get", "global"):
                return self.store.get(args[3], "null")
            if args[:3] == ("settings", "put", "global"):
                self.store[args[3]] = args[4]; return ""
            if args[:3] == ("settings", "delete", "global"):
                self.store.pop(args[3], None); return ""
            return ""

    import types
    sf = _proxy_state(FakeDev.serial)
    if os.path.exists(sf):
        os.remove(sf)
    # No prior proxy at all: set then clear must remove every proxy key.
    store = {}
    dev = FakeDev(store)
    cmd_proxy(dev, types.SimpleNamespace(action="set", hostport="10.0.2.2:8080"))
    check("set writes composite http_proxy", store.get("http_proxy") == "10.0.2.2:8080")
    check("set clears split keys so they can't override",
          not any(k in store for k in AUX_PROXY_KEYS))
    cmd_proxy(dev, types.SimpleNamespace(action="clear"))
    check("clear removes the composite http_proxy", "http_proxy" not in store)
    check("clear leaves no split keys behind (the fixed bug)",
          not any(k in store for k in AUX_PROXY_KEYS))
    # A legit pre-existing split proxy must be restored, not clobbered.
    store = {"global_http_proxy_host": "1.2.3.4", "global_http_proxy_port": "9"}
    dev = FakeDev(store)
    cmd_proxy(dev, types.SimpleNamespace(action="set", hostport="10.0.2.2:8080"))
    check("set stashes and clears a pre-existing split proxy",
          "global_http_proxy_host" not in store)
    cmd_proxy(dev, types.SimpleNamespace(action="clear"))
    check("clear restores the pre-existing split proxy",
          store.get("global_http_proxy_host") == "1.2.3.4"
          and store.get("global_http_proxy_port") == "9")

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(prog="nettrace",
                                description="See what an Android app talks to.")
    p.add_argument("--serial")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="start/stop a device-wide pcap on a running emulator")
    c.add_argument("action", choices=["start", "stop"])
    c.add_argument("path", nargs="?", help="pcap output path (start)")

    h = sub.add_parser("hosts", help="extract DNS names, TLS SNI and IP peers from a pcap")
    h.add_argument("pcap")
    h.add_argument("--baseline", help="an idle-capture pcap to subtract (noise reduction)")
    h.add_argument("--json", action="store_true")

    pr = sub.add_parser("proxy", help="point the device at mitmproxy (best-effort HTTPS)")
    pr.add_argument("action", choices=["set", "clear"])
    pr.add_argument("hostport", nargs="?", default="10.0.2.2:8080")

    sm = sub.add_parser("summarize", help="distil a mitmdump JSONL into hosts+endpoints")
    sm.add_argument("flows")
    sm.add_argument("--json", action="store_true")

    ae = sub.add_parser("apk-endpoints",
                        help="static: regex hostnames/URLs out of a pulled APK")
    ae.add_argument("apk")
    ae.add_argument("--json", action="store_true")

    a = p.parse_args(argv)
    if a.cmd in ("hosts", "summarize", "apk-endpoints"):   # read a file, no device
        return {"hosts": cmd_hosts, "summarize": cmd_summarize,
                "apk-endpoints": cmd_apk_endpoints}[a.cmd](None, a)
    dev = Device(a.serial)
    try:
        return {"capture": cmd_capture, "proxy": cmd_proxy}[a.cmd](dev, a)
    except subprocess.TimeoutExpired:
        die("adb timed out — the device may be busy or disconnected.")


if __name__ == "__main__":
    sys.exit(main())
