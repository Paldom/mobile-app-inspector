#!/usr/bin/env python3
"""Static analysis of an APK or AAB *file* — before it ever runs, no device.

Complements the on-device `android-package-diagnostics` (an installed package)
and `android-network-trace` (live traffic): this one takes apart the binary on
disk. It answers "what's in this file, who signed it, what can it reach, and is
it safe to run".

Two layers, by design:
  * A stdlib INVENTORY core this script fully owns and verifies: composition
    (dex, native ABIs, splits), manifest/permissions (aapt2), and signer
    identity (apksigner). Plus AAB -> universal APK normalization (bundletool).
  * DETECT-AND-DEGRADE orchestration of the heavy tools (jadx decompile, apkid
    packer triage, apkleaks secrets, exodus trackers, MobSF SAST): each runs
    ONLY if its tool is installed, and the report names every layer that was
    skipped-because-absent with the command to add it. It never claims a layer
    ran when the tool is missing.

Standard library only; the external tools are optional and discovered at runtime.

Exit codes: 0 ok | 1 bad/again input | 2 usage or missing required SDK tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# dangerous permission suffixes — a small static reference (the device's live
# list is authoritative; android-package-diagnostics uses that). Kept short and
# obvious rather than pretending to be exhaustive.
DANGEROUS_HINT = (
    "CAMERA",
    "RECORD_AUDIO",
    "ACCESS_FINE_LOCATION",
    "ACCESS_COARSE_LOCATION",
    "READ_CONTACTS",
    "WRITE_CONTACTS",
    "READ_SMS",
    "SEND_SMS",
    "RECEIVE_SMS",
    "READ_CALL_LOG",
    "READ_PHONE_STATE",
    "READ_EXTERNAL_STORAGE",
    "WRITE_EXTERNAL_STORAGE",
    "READ_MEDIA_IMAGES",
    "READ_MEDIA_VIDEO",
    "READ_MEDIA_AUDIO",
    "BODY_SENSORS",
    "ACCESS_BACKGROUND_LOCATION",
    "READ_CALENDAR",
    "WRITE_CALENDAR",
    "BLUETOOTH_CONNECT",
    "BLUETOOTH_SCAN",
    "POST_NOTIFICATIONS",
    "GET_ACCOUNTS",
    "CALL_PHONE",
)


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- tool finding


def _sdk_tool(name: str) -> str | None:
    """Newest build-tools copy of an SDK binary (aapt2, apksigner)."""
    exe = shutil.which(name)
    if exe:
        return exe
    for root in (
        os.environ.get("ANDROID_HOME"),
        os.path.expanduser("~/Library/Android/sdk"),
        os.path.expanduser("~/Android/Sdk"),
    ):
        bt = os.path.join(root or "", "build-tools")
        if os.path.isdir(bt):
            for ver in sorted(os.listdir(bt), reverse=True):
                cand = os.path.join(bt, ver, name)
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return None


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------- normalization


def to_apk(path: str, workdir: str) -> tuple[str, str]:
    """Return (apk_path, kind). An AAB is normalized to a universal APK via
    bundletool; an .apks/.xapk zip yields its base/universal member; an APK is
    used as-is. Fails loudly when a needed tool is absent."""
    low = path.lower()
    if low.endswith(".apk"):
        return path, "apk"
    if low.endswith(".aab"):
        bt = shutil.which("bundletool")
        if not bt:
            die(
                "that's an .aab; normalizing it to an analyzable APK needs "
                "bundletool (brew install bundletool). An AAB's manifest is "
                "protobuf, so aapt2 can't read it directly.",
                2,
            )
        apks = os.path.join(workdir, "out.apks")
        r = run([bt, "build-apks", "--bundle", path, "--output", apks, "--mode=universal"])
        if r.returncode != 0 or not os.path.isfile(apks):
            die(f"bundletool failed: {(r.stderr or r.stdout).strip()[:300]}", 1)
        return _extract_universal(apks, workdir), "aab"
    if low.endswith((".apks", ".xapk", ".apkm", ".zip")):
        return _extract_universal(path, workdir), "bundle"
    die(f"unsupported file type: {path} (expected .apk, .aab, .apks, or .xapk)")


def _extract_universal(apks_zip: str, workdir: str) -> str:
    try:
        z = zipfile.ZipFile(apks_zip)
    except (OSError, zipfile.BadZipFile) as e:
        die(f"{apks_zip} is not a readable APK-set zip: {e}", 1)
    with z:
        names = z.namelist()
        pick = (
            next((n for n in names if n.endswith("universal.apk")), None)
            or next((n for n in names if n.endswith("base-master.apk")), None)
            or next((n for n in names if n.endswith("base.apk")), None)
            or next((n for n in names if n.endswith(".apk")), None)
        )
        if not pick:
            die(f"{apks_zip} contains no APK to analyze", 1)
        info = z.getinfo(pick)
        if info.file_size > 512 * 1024 * 1024:  # decompression-bomb backstop
            die(
                f"{pick} is implausibly large ({info.file_size} bytes) — refusing "
                f"to extract a possible zip bomb",
                1,
            )
        dst = os.path.join(workdir, "extracted.apk")
        with z.open(pick) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, length=1 << 20)
        return dst


# ---------------------------------------------------------------- inventory


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


MAX_ZIP_ENTRIES = 200_000  # backstop against a hostile archive


def composition(apk: str) -> dict:
    """What the APK zip physically contains — dex, native ABIs, assets, certs.
    Pure stdlib; reads NO member content (only the zip index), so a decompression
    bomb cannot expand here. Entry count is capped."""
    try:
        z = zipfile.ZipFile(apk)
    except (OSError, zipfile.BadZipFile) as e:
        die(f"{apk} is not a readable APK (zip): {e}", 1)
    dex, abis, cert_files = 0, {}, []
    asset_bytes = res_bytes = total = 0
    with z:
        for i, info in enumerate(z.infolist()):
            if i >= MAX_ZIP_ENTRIES:
                break
            total += info.file_size
            n = info.filename
            if n.endswith(".dex"):
                dex += 1
            elif n.startswith("lib/") and n.count("/") >= 2:
                abi = n.split("/")[1]
                abis[abi] = abis.get(abi, 0) + info.file_size
            elif n.startswith("assets/"):
                asset_bytes += info.file_size
            elif n.startswith("res/") or n == "resources.arsc":
                res_bytes += info.file_size
            elif n.startswith("META-INF/") and n.endswith((".RSA", ".EC", ".DSA")):
                cert_files.append(n)
    return {
        "dexFiles": dex,
        "nativeAbis": sorted(abis),
        "nativeBytesByAbi": abis,
        "assetBytes": asset_bytes,
        "resourceBytes": res_bytes,
        "uncompressedBytes": total,
        "signatureBlockFiles": cert_files,
    }


BADGING_PKG = re.compile(r"package: name='([^']+)' versionCode='([^']*)' versionName='([^']*)'")


def manifest(apk: str) -> dict:
    aapt2 = _sdk_tool("aapt2")
    if not aapt2:
        return {
            "error": "aapt2 not found (Android SDK build-tools); "
            "manifest/permissions unavailable. brew install "
            "android-commandlinetools, or use the SDK build-tools."
        }
    r = run([aapt2, "dump", "badging", apk])
    if r.returncode != 0:
        return {"error": f"aapt2 could not read the manifest: {r.stderr.strip()[:200]}"}
    out = r.stdout
    m = BADGING_PKG.search(out)
    perms = sorted(set(re.findall(r"uses-permission: name='([^']+)'", out)))
    abi = re.search(r"native-code: (.+)", out)
    launch = re.search(r"launchable-activity: name='([^']+)'", out)
    sdk = re.search(r"(?:^|\n)minSdkVersion:'(\d+)'", out)
    target = re.search(r"targetSdkVersion:'(\d+)'", out)
    return {
        "package": m.group(1) if m else None,
        "versionCode": m.group(2) if m else None,
        "versionName": m.group(3) if m else None,
        "minSdk": sdk.group(1) if sdk else None,
        "targetSdk": target.group(1) if target else None,
        "launchableActivity": launch.group(1) if launch else None,
        "nativeCode": abi.group(1).replace("'", "") if abi else None,
        "permissions": perms,
        "dangerousPermissions": [p for p in perms if p.rsplit(".", 1)[-1] in DANGEROUS_HINT],
    }


# known commercial-packer / anti-analysis native lib names (heuristic — apkid is
# authoritative when installed; this is a stdlib fallback since its wheel often
# fails to build).
PACKER_LIBS = (
    "libjiagu",
    "libdexhelper",
    "libsecexe",
    "libsecmain",
    "libapptamper",
    "libtup",
    "libmobisec",
    "libpreverify1",
    "libddog",
    "libnesec",
    "libbaiduprotect",
    "libexecmain",
    "libtosprotection",
    "libnqshield",
    "libkonyjsvm",
)


def manifest_posture(apk: str) -> dict:
    """Exported components and the security-relevant application flags — the
    densest 'is it safe' signal, from aapt2 xmltree. Zero extra dependencies."""
    aapt2 = _sdk_tool("aapt2")
    if not aapt2:
        return {"status": "skipped", "reason": "aapt2 not found"}
    r = run([aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", apk])
    if r.returncode != 0:
        return {"status": "failed", "reason": r.stderr.strip()[:200]}
    exported = {"activity": 0, "service": 0, "receiver": 0, "provider": 0}
    cur = None
    for line in r.stdout.splitlines():
        s = line.strip()
        m = re.match(r"E: (activity|service|receiver|provider)\b", s)
        if m:
            cur = m.group(1)
        elif s.startswith("E: "):
            cur = None
        elif cur and re.search(r"android:exported\([^)]*\)=true", s):
            exported[cur] += 1
            cur = None

    def flag(name):
        m = re.search(rf"android:{name}\([^)]*\)=(true|false|0x[0-9a-f]+|@0x[0-9a-f]+)", r.stdout)
        return m.group(1) if m else None

    return {
        "status": "ran",
        "exportedComponents": exported,
        "exportedTotal": sum(exported.values()),
        "debuggable": flag("debuggable") == "true",
        "allowBackup": flag("allowBackup") != "false",  # default true if unset
        "usesCleartextTraffic": flag("usesCleartextTraffic") == "true",
        "hasNetworkSecurityConfig": flag("networkSecurityConfig") is not None,
    }


BOOL_TRUE = ("=true", "0xffffffff", ")-1")


def _attr(line: str, name: str) -> str | None:
    m = re.search(rf'android:{name}\([^)]*\)="([^"]*)"', line)
    return m.group(1) if m else None


def exported_inventory(apk: str) -> dict:
    """Name every exported component and the deep links it claims.

    `manifest_posture` counts them; a count cannot be probed. This walks the same
    aapt2 xmltree by indentation so each component carries its own name, actions
    and URI schemes — the input `android-intent-probe` fires at the running app to
    find out whether an exported entry point is actually reachable.
    """
    aapt2 = _sdk_tool("aapt2")
    if not aapt2:
        return {"status": "skipped", "reason": "aapt2 not found"}
    r = run([aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", apk])
    if r.returncode != 0:
        return {"status": "failed", "reason": r.stderr.strip()[:200]}

    comps, cur, cur_indent, in_filter = [], None, -1, False
    for line in r.stdout.splitlines():
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        m = re.match(r"E: (activity|activity-alias|service|receiver|provider)\b", s)
        if m:
            cur = {
                "type": m.group(1).replace("activity-alias", "activity"),
                "name": None,
                "exported": None,
                "permission": None,
                "authorities": None,
                "actions": [],
                "schemes": [],
                "hosts": [],
                "paths": [],
            }
            comps.append(cur)
            cur_indent, in_filter = indent, False
            continue
        if cur is None:
            continue
        if s.startswith("E: ") and indent <= cur_indent:
            cur = None  # left this component's subtree
            continue
        if s.startswith("E: intent-filter"):
            in_filter = True
        elif s.startswith("A: "):
            if cur["name"] is None and (v := _attr(s, "name")) and not in_filter:
                cur["name"] = v
            if "android:exported(" in s:
                cur["exported"] = any(t in s for t in BOOL_TRUE)
            if (v := _attr(s, "permission")) and cur["permission"] is None:
                cur["permission"] = v
            if v := _attr(s, "authorities"):
                cur["authorities"] = v
            if in_filter:
                if ((v := _attr(s, "name")) and v.startswith("android.intent.action")) or (
                    (v := _attr(s, "name")) and "." in v
                ):
                    cur["actions"].append(v)
                if v := _attr(s, "scheme"):
                    cur["schemes"].append(v)
                if v := _attr(s, "host"):
                    cur["hosts"].append(v)
                for pa in ("pathPrefix", "path", "pathPattern"):
                    if v := _attr(s, pa):
                        cur["paths"].append(v)

    for c in comps:
        # An intent filter implies exported when the attribute is absent, except on
        # API 31+ where it must be declared. Report None as "unknown", never false.
        if c["exported"] is None and c["actions"]:
            c["exported"] = True
            c["exportedInferred"] = True
        for k in ("actions", "schemes", "hosts", "paths"):
            c[k] = sorted(set(c[k]))
    named = [c for c in comps if c["name"]]
    return {
        "status": "ran",
        "components": named,
        "exportedNames": [c["name"] for c in named if c["exported"]],
        "note": "exported=true (or an intent-filter with no explicit attribute). "
        "An exported component is an entry point, not a vulnerability — "
        "probe it before reporting anything about it.",
    }


def heuristic_packer(apk: str) -> dict:
    """Stdlib packer/obfuscator heuristic for when apkid is absent. Flags known
    packer native libs and an unusually low dex count with large encrypted-looking
    assets. Clearly labelled heuristic — apkid is authoritative."""
    try:
        z = zipfile.ZipFile(apk)
    except (OSError, zipfile.BadZipFile):
        return {"status": "failed"}
    hits = []
    with z:
        names = [n.lower() for n in z.namelist()]
        for lib in PACKER_LIBS:
            if any(lib in n for n in names):
                hits.append(lib)
    return {
        "status": "ran",
        "heuristic": True,
        "knownPackerLibs": sorted(set(hits)),
        "suspected": bool(hits),
        "note": "heuristic only — install apkid for authoritative packer/"
        "obfuscator detection (--layers packer)",
    }


# Play publishing deadlines, as dated policy rather than folklore. Update these
# when Google moves them; every gate below states the date it enforces.
TARGET_SDK_FLOOR = 35  # required for new apps and updates (from 2025-08-31)
PAGE_ALIGN_BYTES = 16 * 1024  # 16 KB ELF LOAD alignment, required from 2025-11-01


def play_readiness(apk: str, man: dict, post: dict, sign: dict, comp: dict) -> dict:
    """Deterministic Play publishing gates: pass/fail, no judgement, no score.

    These outrank security triage in a review — an app that cannot be published is
    blocked regardless of how its findings are ranked. Every gate is read from the
    binary, so none of them is an opinion.
    """
    gates = []

    def gate(name, status, detail):
        gates.append({"gate": name, "status": status, "detail": detail})

    tgt = man.get("targetSdk")
    try:
        tgt_i = int(str(tgt))
    except (TypeError, ValueError):
        tgt_i = None
    if tgt_i is None:
        gate("targetSdk", "unknown", "targetSdk not readable (aapt2 missing?)")
    else:
        gate(
            "targetSdk",
            "pass" if tgt_i >= TARGET_SDK_FLOOR else "fail",
            f"targets API {tgt_i}; Play requires >= {TARGET_SDK_FLOOR} for new apps "
            f"and updates (since 2025-08-31)",
        )

    abis = comp.get("nativeAbis") or []
    if not abis:
        gate("64-bit native code", "pass", "no native code — the requirement does not apply")
    elif "arm64-v8a" in abis:
        gate("64-bit native code", "pass", f"ships arm64-v8a (ABIs: {', '.join(abis)})")
    else:
        gate(
            "64-bit native code", "fail", f"native code without arm64-v8a (ABIs: {', '.join(abis)})"
        )

    align = native_page_alignment(apk)
    gate("16 KB page alignment", align["status"], align["detail"])

    schemes = sign.get("schemes") or {}
    if not schemes:
        gate(
            "Signing scheme",
            "unknown",
            sign.get("reason") or sign.get("error") or "signer not read",
        )
    elif any(schemes.get(k) for k in ("v2", "v3", "v3.1", "v4")):
        # v1 alongside v2/v3 is a backward-compatibility artefact, not the Janus bug.
        gate(
            "Signing scheme",
            "pass",
            "signed with "
            + ", ".join(k for k, v in schemes.items() if v)
            + " (v1 alongside v2/v3 is a compatibility artefact, not Janus)",
        )
    else:
        gate(
            "Signing scheme",
            "fail",
            "v1-only signature — vulnerable to Janus (CVE-2017-13156) and will not "
            "install on Android 11+",
        )

    if post.get("debuggable") is True:
        gate("Not debuggable", "fail", 'android:debuggable="true" in a shipped build')
    elif post.get("debuggable") is False:
        gate("Not debuggable", "pass", "android:debuggable is not set")

    dn = sign.get("signerDN") or ""
    if sign.get("debugKey") or "CN=Android Debug" in dn:
        gate("Release signing key", "fail", "signed with the Android debug certificate")
    elif dn:
        gate("Release signing key", "pass", f"signed by {dn.split(',')[0]}")

    failed = [g for g in gates if g["status"] == "fail"]
    return {
        "gates": gates,
        "blocking": len(failed),
        "note": "Deterministic publishing gates read from the binary. They are "
        "release-blocking regardless of security severity, and they carry "
        "no score.",
    }


def native_page_alignment(apk: str) -> dict:
    """Are the 64-bit .so LOAD segments 16 KB aligned? (Play, from 2025-11-01.)

    Read straight out of the ELF program headers with stdlib struct — no readelf, no
    extraction to disk. Only 64-bit ELFs matter; 32-bit ABIs are exempt.
    """
    try:
        z = zipfile.ZipFile(apk)
    except (OSError, zipfile.BadZipFile):
        return {"status": "unknown", "detail": "APK not readable as a zip"}
    bad, checked = [], 0
    with z:
        libs = [
            n
            for n in z.namelist()
            if n.startswith("lib/") and n.endswith(".so") and ("arm64-v8a" in n or "x86_64" in n)
        ]
        for name in libs[:60]:  # a sample is enough to catch a misbuilt lib
            try:
                with z.open(name) as fh:
                    head = fh.read(64)
                    if head[:4] != b"\x7fELF" or head[4] != 2:
                        continue  # not a 64-bit ELF
                    e_phoff = struct.unpack_from("<Q", head, 32)[0]
                    e_phentsize = struct.unpack_from("<H", head, 54)[0]
                    e_phnum = struct.unpack_from("<H", head, 56)[0]
                    if not e_phnum or e_phoff <= 0:
                        continue
                    need = e_phoff + e_phentsize * e_phnum
                    if need > 4 * 1024 * 1024:
                        continue  # refuse to read an absurd header table
                    blob = head + fh.read(need - len(head))
                checked += 1
                worst = 0
                for i in range(e_phnum):
                    off = e_phoff + i * e_phentsize
                    if off + 56 > len(blob):
                        break
                    p_type = struct.unpack_from("<I", blob, off)[0]
                    if p_type != 1:  # PT_LOAD
                        continue
                    p_align = struct.unpack_from("<Q", blob, off + 48)[0]
                    worst = max(worst, p_align) if worst == 0 else min(worst, p_align)
                if worst and worst < PAGE_ALIGN_BYTES:
                    bad.append(f"{name} (align {worst})")
            except (OSError, struct.error, ValueError):
                continue
    if not checked:
        return {
            "status": "pass",
            "detail": "no 64-bit native libraries — the requirement does not apply",
        }
    if bad:
        return {
            "status": "fail",
            "detail": f"{len(bad)} of {checked} 64-bit .so files have LOAD segments "
            f"below {PAGE_ALIGN_BYTES} bytes: " + "; ".join(bad[:4]),
        }
    return {
        "status": "pass",
        "detail": f"all {checked} 64-bit .so files align LOAD segments to "
        f">= {PAGE_ALIGN_BYTES} bytes",
    }


def signer(apk: str) -> dict:
    aps = _sdk_tool("apksigner")
    if not aps:
        return {"error": "apksigner not found (Android SDK build-tools)"}
    r = run([aps, "verify", "--verbose", "--print-certs", apk])
    out = (r.stdout or "") + (r.stderr or "")
    schemes = {
        m.group(1): m.group(2) == "true"
        for m in re.finditer(r"Verified using (v[\d.]+) scheme[^:]*:\s*(true|false)", out)
    }
    dn = re.search(r"Signer #1 certificate DN:\s*(.+)", out)
    sha = re.search(r"Signer #1 certificate SHA-256 digest:\s*([0-9a-f]+)", out)
    return {
        "verified": "Verified" in out and r.returncode == 0,
        "schemes": schemes,
        "signerDN": dn.group(1).strip() if dn else None,
        "certSha256": sha.group(1) if sha else None,
        "debugKey": bool(dn and "Android Debug" in dn.group(1)),
    }


# ---------------------------------------------------------------- heavy layers


# Each: (layer, availability check, runner). Runner returns a small result dict
# or a {"skipped": reason} — it must NEVER pretend to have run.
def layer_jadx(apk: str, workdir: str) -> dict:
    exe = shutil.which("jadx")
    if not exe:
        return {"skipped": "jadx not installed (brew install jadx) — no decompilation"}
    out = os.path.join(workdir, "jadx")
    r = run([exe, "-d", out, "-r", "--no-debug-info", "-j", "4", apk], timeout=900)
    src = os.path.join(out, "sources")
    files = sum(len(fs) for _, _, fs in os.walk(src)) if os.path.isdir(src) else 0
    return {
        "ran": True,
        "outputDir": out,
        "javaFiles": files,
        "note": "jadx completes with per-method errors on obfuscated code; "
        "validate output, do not trust a zero exit as full coverage",
        "warnings": r.returncode != 0,
    }


def layer_apkid(apk: str, workdir: str) -> dict:
    exe = shutil.which("apkid")
    if not exe:
        return {
            "skipped": "apkid not installed (pipx install apkid — needs a "
            "working native build for yara-python-dex) — no packer/obfuscator triage"
        }
    r = run([exe, "-j", apk])
    try:
        return {"ran": True, "result": json.loads(r.stdout)}
    except ValueError:
        return {"ran": True, "raw": r.stdout.strip()[:2000]}


def layer_apkleaks(apk: str, workdir: str) -> dict:
    exe = shutil.which("apkleaks")
    if not exe:
        return {
            "skipped": "apkleaks not installed (pipx install apkleaks) — "
            "no hardcoded secret/endpoint hunt. android-network-trace "
            "apk-endpoints gives a stdlib URL-only first look."
        }
    out = os.path.join(workdir, "apkleaks.json")
    run([exe, "-f", apk, "-j", "-o", out], timeout=900)
    try:
        return {"ran": True, "result": json.load(open(out))}
    except (OSError, ValueError):
        return {"ran": True, "note": "apkleaks produced no parseable JSON"}


def layer_exodus(apk: str, workdir: str) -> dict:
    if not shutil.which("docker"):
        return {
            "skipped": "docker not available — exodus tracker detection skipped "
            "(docker run exodusprivacy/exodus-standalone)"
        }
    if not _docker_image("exodusprivacy/exodus-standalone"):
        return {
            "skipped": "exodus image not pulled (docker pull "
            "exodusprivacy/exodus-standalone) — no tracker detection"
        }
    d = os.path.dirname(os.path.abspath(apk))
    r = run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{d}:/app",
            "-i",
            "exodusprivacy/exodus-standalone",
            "-j",
            f"/app/{os.path.basename(apk)}",
        ],
        timeout=900,
    )
    try:
        return {"ran": True, "result": json.loads(r.stdout)}
    except ValueError:
        return {"ran": True, "raw": (r.stdout or r.stderr).strip()[:2000]}


def _docker_image(name: str) -> bool:
    r = run(["docker", "image", "inspect", name], timeout=30)
    return r.returncode == 0


LAYERS = {
    "decompile": layer_jadx,
    "packer": layer_apkid,
    "secrets": layer_apkleaks,
    "trackers": layer_exodus,
}


# ---------------------------------------------------------------- commands


def cmd_report(a) -> int:
    if not os.path.isfile(a.file):
        die(f"no such file: {a.file}", 1)
    workdir = tempfile.mkdtemp(prefix="apkscan-")
    try:
        digest = sha256(a.file)
        apk, kind = to_apk(a.file, workdir)
        rep = {
            "input": os.path.abspath(a.file),
            "inputSha256": digest,
            "inputKind": kind,
            "composition": composition(apk),
            "manifest": manifest(apk),
            "manifestPosture": manifest_posture(apk),
            "exportedInventory": exported_inventory(apk),
            "heuristicPacker": heuristic_packer(apk),
            "layers": {},
        }
        # apksigner on a bundletool-derived universal APK reports bundletool's
        # DEBUG key, not the app's signer — a confidently-wrong identity. Only
        # trust the signer for real APK input.
        if kind == "apk":
            rep["signer"] = signer(apk)
        else:
            rep["signer"] = {
                "status": "not_applicable",
                "reason": "input is an AAB/bundle; the analyzable APK "
                "was re-signed with a bundletool debug key. The shipped "
                "signer cannot be determined from an AAB (the delivered "
                "APK is signed downstream — e.g. Play App Signing or another "
                "store/workflow — and any upload signature is not the "
                "delivered signer). Get it from an installed copy: apksigner "
                "on the device APK.",
            }
        want = a.layers.split(",") if a.layers else list(LAYERS)
        for name in want:
            fn = LAYERS.get(name)
            if not fn:
                continue
            try:
                rep["layers"][name] = fn(apk, workdir)
            except subprocess.TimeoutExpired:
                rep["layers"][name] = {"skipped": "tool timed out"}
        for _name, v in rep["layers"].items():  # 3-state status per layer
            v.setdefault("status", "skipped" if "skipped" in v else "ran")
        # Publishing gates last: they need the manifest, posture and signer above.
        rep["playReadiness"] = play_readiness(
            apk, rep["manifest"], rep["manifestPosture"], rep["signer"], rep["composition"]
        )
        rep["layersSkipped"] = [n for n, v in rep["layers"].items() if v.get("status") == "skipped"]
        rep["analysisCaveats"] = _caveats(rep)
        if a.json:
            print(json.dumps(rep, indent=1))
        else:
            _print_report(rep)
        return 0
    finally:
        if not a.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        elif a.keep:
            print(f"(intermediates kept in {workdir})", file=sys.stderr)


def _caveats(rep: dict) -> list[str]:
    """Explicit reasons the report is NOT a clean bill of health — so an agent
    never reads skipped/absent layers as 'nothing found'."""
    out = []
    incomplete = [n for n, v in rep.get("layers", {}).items() if v.get("status") != "ran"]
    if incomplete:
        out.append(
            f"layers not completed ({', '.join(incomplete)}) — a "
            f"skipped/failed layer is NOT a clean result; treat its findings "
            f"as unavailable, not empty"
        )
    if rep.get("inputKind") in ("aab", "bundle"):
        out.append(
            "analysis covers the universal/base APK only — on-demand feature "
            "modules and asset packs are NOT included, and the derived APK's "
            "hash/size match no real install"
        )
    hp = rep.get("heuristicPacker", {})
    if hp.get("suspected"):
        out.append(
            "known packer libs present — decompilation likely sees stubs; "
            "trust the packer layer over jadx output"
        )
    dec = rep.get("layers", {}).get("decompile", {})
    if dec.get("status") == "ran":
        out.append(
            "decompiler completes even on obfuscated code; java-file count "
            "is surface reached, not proof of full coverage"
        )
    post = rep.get("manifestPosture", {})
    if post.get("debuggable"):
        out.append("manifest is DEBUGGABLE — unusual for a release build")
    out.append(
        "static presence != runtime use; a signature verifying != a "
        "trusted signer (compare certSha256 to a known-good build)"
    )
    return out


def _print_report(r: dict) -> None:
    m, s, c = r["manifest"], r["signer"], r["composition"]
    print(
        f"{m.get('package')}  {m.get('versionName')} (code {m.get('versionCode')})"
        f"  [{r['inputKind']}]"
    )
    print(f"  sha256      {r['inputSha256']}")
    print(f"  sdk         min {m.get('minSdk')} / target {m.get('targetSdk')}")
    print(
        f"  composition {c['dexFiles']} dex, ABIs {c['nativeAbis'] or ['none']}, "
        f"{c['uncompressedBytes'] // 1024} KB uncompressed"
    )
    if s.get("status") == "not_applicable":
        print(f"  signer      n/a ({r['inputKind']} re-signed by bundletool; see report)")
    elif s.get("certSha256"):
        print(f"  signer      {s['certSha256']}")
        print(f"              {s.get('signerDN')}")
        print(
            f"              schemes {', '.join(k for k, v in s['schemes'].items() if v) or 'none'}"
            + ("  *DEBUG KEY*" if s.get("debugKey") else "")
        )
    perms = m.get("permissions", [])
    dang = m.get("dangerousPermissions", [])
    print(f"  permissions {len(perms)} requested, {len(dang)} likely-dangerous")
    for p in dang:
        print(f"                - {p.rsplit('.', 1)[-1]}")
    post = r.get("manifestPosture", {})
    if post.get("status") == "ran":
        flags = [k for k in ("debuggable", "usesCleartextTraffic") if post.get(k)]
        if not post.get("allowBackup"):
            flags.append("backup-disabled")
        print(
            f"  posture     {post['exportedTotal']} exported components"
            + (f"; {', '.join(flags)}" if flags else "")
            + ("" if post.get("hasNetworkSecurityConfig") else "; no NSC")
        )
    hp = r.get("heuristicPacker", {})
    if hp.get("suspected"):
        print(f"  packer(heur) known packer libs: {', '.join(hp['knownPackerLibs'])}")
    print("  analysis layers:")
    for name, v in r["layers"].items():
        if "skipped" in v:
            print(f"    - {name:9} SKIPPED: {v['skipped']}")
        else:
            detail = f"{v.get('javaFiles')} java files" if name == "decompile" else "ran"
            print(f"    - {name:9} {detail}")
    print("  caveats:")
    for c in r.get("analysisCaveats", []):
        print(f"    - {c}")
    print("  (see references/toolkit.md for MobSF/Ghidra/native-.so escalations)")


def cmd_tools(a) -> int:
    """Report which analysis tools are installed — an honest capability map."""
    checks = [
        ("aapt2", bool(_sdk_tool("aapt2")), "manifest/permissions", "SDK build-tools"),
        ("apksigner", bool(_sdk_tool("apksigner")), "signer identity", "SDK build-tools"),
        ("bundletool", bool(shutil.which("bundletool")), "AAB -> APK", "brew install bundletool"),
        ("jadx", bool(shutil.which("jadx")), "decompile", "brew install jadx"),
        ("apkid", bool(shutil.which("apkid")), "packer/obfuscator", "pipx install apkid"),
        ("apkleaks", bool(shutil.which("apkleaks")), "secrets/endpoints", "pipx install apkleaks"),
        ("docker", bool(shutil.which("docker")), "exodus/MobSF", "install Docker Desktop"),
    ]
    width = max(len(n) for n, *_ in checks)
    for name, ok, purpose, how in checks:
        print(
            f"[{'ok ' if ok else 'MISS'}] {name.ljust(width)}  {purpose}"
            + ("" if ok else f"  ->  {how}")
        )
    core = all(ok for n, ok, *_ in checks if n in ("aapt2", "apksigner"))
    print(
        f"\ncore inventory {'available' if core else 'INCOMPLETE (need SDK build-tools)'}; "
        f"heavy layers run only when their tool is present."
    )
    return 0 if core else 1


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    fails = []

    def check(name, cond):
        (print(f"PASS {name}") if cond else fails.append(name) or print(f"FAIL {name}"))

    # composition on a crafted APK-shaped zip
    with tempfile.TemporaryDirectory() as td:
        apk = os.path.join(td, "t.apk")
        with zipfile.ZipFile(apk, "w") as z:
            z.writestr("classes.dex", b"x" * 100)
            z.writestr("classes2.dex", b"y" * 50)
            z.writestr("lib/arm64-v8a/libfoo.so", b"z" * 200)
            z.writestr("lib/x86_64/libfoo.so", b"w" * 200)
            z.writestr("assets/data.bin", b"a" * 40)
            z.writestr("resources.arsc", b"r" * 10)
            z.writestr("META-INF/CERT.RSA", b"c" * 5)
        c = composition(apk)
        check("counts dex files", c["dexFiles"] == 2)
        check("finds native ABIs sorted", c["nativeAbis"] == ["arm64-v8a", "x86_64"])
        check("sums native bytes per abi", c["nativeBytesByAbi"]["arm64-v8a"] == 200)
        check("finds signature block file", c["signatureBlockFiles"] == ["META-INF/CERT.RSA"])
        check("sha256 stable", len(sha256(apk)) == 64)

        # to_apk: universal extraction from an .apks zip
        apks = os.path.join(td, "out.apks")
        with zipfile.ZipFile(apks, "w") as z:
            z.writestr("splits/base-master.apk", b"PK-fake")
            z.writestr("universal.apk", b"PK-universal")
        got = _extract_universal(apks, td)
        check("apks extraction prefers universal.apk", Path(got).read_bytes() == b"PK-universal")

    # heavy layers must degrade, never crash, when a tool is absent
    with tempfile.TemporaryDirectory() as td:
        fake = os.path.join(td, "x.apk")
        open(fake, "wb").close()
        for name, fn in LAYERS.items():
            try:
                r = fn(fake, td)
                # jadx IS installed here, so 'decompile' may run; others skip.
                ok = ("skipped" in r) or ("ran" in r)
                check(f"layer {name} returns skip-or-ran (no crash)", ok)
            except Exception:
                check(f"layer {name} returns skip-or-ran (no crash)", False)

    # heuristic packer flags a known packer lib in a crafted zip
    with tempfile.TemporaryDirectory() as td:
        packed = os.path.join(td, "p.apk")
        with zipfile.ZipFile(packed, "w") as z:
            z.writestr("lib/arm64-v8a/libjiagu.so", b"x")
            z.writestr("classes.dex", b"y")
        hp = heuristic_packer(packed)
        check(
            "heuristic packer flags libjiagu",
            hp["suspected"] and "libjiagu" in hp["knownPackerLibs"],
        )
        clean = os.path.join(td, "c.apk")
        with zipfile.ZipFile(clean, "w") as z:
            z.writestr("classes.dex", b"y")
        check(
            "heuristic packer clean on ordinary apk", heuristic_packer(clean)["suspected"] is False
        )

    # caveats: any non-ran layer (skipped OR failed) is "not clean"; AAB scope noted
    cav = _caveats(
        {
            "layers": {"secrets": {"status": "skipped"}, "decompile": {"status": "failed"}},
            "heuristicPacker": {},
            "manifestPosture": {},
            "inputKind": "apk",
        }
    )
    check(
        "caveats flag skipped+failed layers as not-clean",
        any("not completed" in c and "secrets" in c and "decompile" in c for c in cav),
    )
    check("caveats always include static!=runtime", any("static presence" in c for c in cav))
    check(
        "aab input adds a universal-only-coverage caveat",
        any(
            "on-demand feature modules" in c
            for c in _caveats(
                {"layers": {}, "heuristicPacker": {}, "manifestPosture": {}, "inputKind": "aab"}
            )
        ),
    )

    # exercise the FULL to_apk AAB path with a STUB bundletool (verifies our
    # build-apks invocation + universal extraction without a real .aab fixture).
    import stat as _stat

    with tempfile.TemporaryDirectory() as td:
        aab = os.path.join(td, "app.aab")
        Path(aab).write_bytes(b"fake-aab")
        stub_dir = os.path.join(td, "bin")
        os.makedirs(stub_dir)
        stub = os.path.join(stub_dir, "bundletool")
        # stub reads --output path and writes an .apks zip containing universal.apk
        open(stub, "w").write(  # noqa: SIM115 - self-test fixture write; the handle is scoped to this statement and restructuring the literal is riskier than the leak
            "#!/usr/bin/env python3\n"
            "import sys, zipfile\n"
            "args=sys.argv\n"
            "out=args[args.index('--output')+1]\n"
            "assert '--mode=universal' in args\n"
            "z=zipfile.ZipFile(out,'w'); z.writestr('universal.apk', b'UNIV'); z.close()\n"
        )
        os.chmod(stub, os.stat(stub).st_mode | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)
        old = os.environ["PATH"]
        os.environ["PATH"] = stub_dir + os.pathsep + old
        try:
            apk, kind = to_apk(aab, td)
            check(
                "to_apk routes .aab through bundletool build-apks --mode=universal",
                kind == "aab" and Path(apk).read_bytes() == b"UNIV",
            )
        finally:
            os.environ["PATH"] = old

    check(
        "dangerous-permission hint catches CAMERA",
        "CAMERA" in DANGEROUS_HINT and "INTERNET" not in DANGEROUS_HINT,
    )
    check(
        "badging regex parses a package line",
        BADGING_PKG.search("package: name='com.x' versionCode='7' versionName='1.2'").group(1)
        == "com.x",
    )
    import re as _re

    sample = "minSdkVersion:'23'\ntargetSdkVersion:'30'"
    check(
        "minSdk regex does not match targetSdkVersion",
        _re.search(r"(?:^|\n)minSdkVersion:'(\d+)'", sample).group(1) == "23",
    )

    # ---- Play publishing gates: deterministic, and no gate may be an opinion.
    def gates_of(man, post, sign, comp):
        return {
            g["gate"]: g["status"] for g in play_readiness("x.apk", man, post, sign, comp)["gates"]
        }

    modern = gates_of(
        {"targetSdk": "35"},
        {"debuggable": False},
        {"schemes": {"v1": True, "v2": True, "v3": True}, "signerDN": "CN=Acme"},
        {"nativeAbis": []},
    )
    check("targetSdk at the floor passes", modern["targetSdk"] == "pass")
    check(
        "v1+v2/v3 is NOT reported as Janus (the known false positive)",
        modern["Signing scheme"] == "pass",
    )
    check("no native code exempts the 64-bit gate", modern["64-bit native code"] == "pass")

    stale = gates_of(
        {"targetSdk": "33"},
        {"debuggable": True},
        {"schemes": {"v1": True}, "signerDN": "CN=Android Debug", "debugKey": True},
        {"nativeAbis": ["armeabi-v7a"]},
    )
    check("targetSdk below the floor fails", stale["targetSdk"] == "fail")
    check(
        "v1-only signing fails (Janus / will not install on 11+)", stale["Signing scheme"] == "fail"
    )
    check("debuggable release fails", stale["Not debuggable"] == "fail")
    check("debug certificate fails", stale["Release signing key"] == "fail")
    check("32-bit-only native code fails", stale["64-bit native code"] == "fail")
    check(
        "unreadable targetSdk is 'unknown', never a silent pass",
        gates_of({}, {}, {}, {})["targetSdk"] == "unknown",
    )

    # 16 KB ELF alignment, read from real program headers in a synthetic APK.
    def elf64(p_align: int) -> bytes:
        eh = bytearray(64)
        eh[0:4] = b"\x7fELF"
        eh[4] = 2  # 64-bit
        struct.pack_into("<Q", eh, 32, 64)  # e_phoff
        struct.pack_into("<H", eh, 54, 56)  # e_phentsize
        struct.pack_into("<H", eh, 56, 1)  # e_phnum
        ph = bytearray(56)
        struct.pack_into("<I", ph, 0, 1)  # PT_LOAD
        struct.pack_into("<Q", ph, 48, p_align)
        return bytes(eh + ph)

    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, "good.apk")
        with zipfile.ZipFile(good, "w") as z:
            z.writestr("lib/arm64-v8a/libok.so", elf64(16384))
            z.writestr("lib/armeabi-v7a/libold.so", b"not-an-elf")
        check("16 KB-aligned 64-bit lib passes", native_page_alignment(good)["status"] == "pass")

        bad = os.path.join(td, "bad.apk")
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("lib/arm64-v8a/libbad.so", elf64(4096))
        check("4 KB-aligned 64-bit lib fails", native_page_alignment(bad)["status"] == "fail")

        pure = os.path.join(td, "pure.apk")
        with zipfile.ZipFile(pure, "w") as z:
            z.writestr("classes.dex", b"dex")
        check(
            "an app with no 64-bit .so is exempt, not failed",
            native_page_alignment(pure)["status"] == "pass",
        )

    pr = play_readiness("x", {"targetSdk": "35"}, {}, {}, {})
    check(
        "play readiness emits gates and a blocking count, never a score",
        set(pr) == {"gates", "blocking", "note"}
        and all(set(g) == {"gate", "status", "detail"} for g in pr["gates"]),
    )

    print(f"RESULT: {'ok' if not fails else 'fail'}")
    return 1 if fails else 0


def main(argv=None) -> int:
    if "--self-test" in (argv if argv is not None else sys.argv[1:]):
        return self_test()
    p = argparse.ArgumentParser(
        prog="apkscan", description="Static analysis of an APK or AAB file."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="inventory + orchestrate available analysis layers")
    r.add_argument("file", help="path to .apk / .aab / .apks / .xapk")
    r.add_argument(
        "--layers", help="comma list: decompile,packer,secrets,trackers (default: all available)"
    )
    r.add_argument("--json", action="store_true")
    r.add_argument("--keep", action="store_true", help="keep intermediates (jadx output, etc.)")

    sub.add_parser("tools", help="show which analysis tools are installed")

    a = p.parse_args(argv)
    return {"report": cmd_report, "tools": cmd_tools}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
