"""mitmproxy addon: write one JSON line per response, for `nettrace.py summarize`.

    mitmdump -s mitm_capture.py --set flowout=flows.jsonl --listen-port 8080

Metadata by default — bodies and cookies/auth headers are NOT written unless you
pass `--set bodies=true`, because captured flows routinely contain tokens and
personal data. Query strings are dropped from the summarized path (they carry
ids). This addon is loaded by mitmproxy, not run directly.
"""
import json
import time

# mitmproxy is imported by the mitmdump process that loads this addon; import it
# lazily so a bare `python3 mitm_capture.py` (e.g. a tooling sweep) does not fail.
try:
    from mitmproxy import ctx, http
except ImportError:
    ctx = http = None

REDACT = {"authorization", "cookie", "set-cookie", "proxy-authorization",
          "x-auth-token", "x-api-key"}


def load(loader):
    loader.add_option("flowout", str, "flows.jsonl", "JSONL output path")
    loader.add_option("bodies", bool, False, "include request/response bodies (sensitive)")


def _headers(h):
    return {k: ("<redacted>" if k.lower() in REDACT else v) for k, v in h.items()}


def response(flow: http.HTTPFlow):
    rec = {
        "ts": round(time.time(), 3),
        "method": flow.request.method,
        "scheme": flow.request.scheme,
        "host": flow.request.pretty_host,
        "path": flow.request.path,
        "status": flow.response.status_code,
        "reqHeaders": _headers(flow.request.headers),
        "respType": flow.response.headers.get("content-type", ""),
    }
    if ctx.options.bodies:
        rec["reqBody"] = flow.request.get_text(strict=False)
        rec["respBody"] = (flow.response.get_text(strict=False) or "")[:20000]
    with open(ctx.options.flowout, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
