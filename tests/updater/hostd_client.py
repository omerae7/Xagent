"""کلاینت آزمون کارگزار میزبان: درخواست امضاشده به سوکت، انتظار برای پایان کار و چاپ نتیجه.

python3 hostd_client.py <socket> <env_file> <op> [args-json]
خروجی: JSON {"status", "exit_code", "output"} ؛ کد خروج ۰ فقط اگر کار با موفقیت پایان یابد.
"""

import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import time

sock, env_file, op = sys.argv[1:4]
args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
secret = next(line.split("=", 1)[1].strip() for line in open(env_file) if line.startswith("XAGENT_SECRET_KEY="))


def canonical(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def call(req_op, a):
    req = {"v": 1, "op": req_op, "args": a, "ts": int(time.time()), "nonce": secrets.token_hex(16), "actor": "e2e"}
    key = hmac.new(secret.encode(), b"xagent-hostd-request-v1", hashlib.sha256).digest()
    req["mac"] = hmac.new(key, canonical(req), hashlib.sha256).hexdigest()
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(20)
    c.connect(sock)
    c.sendall(json.dumps(req).encode() + b"\n")
    line = c.makefile().readline()
    c.close()
    return json.loads(line)


r = call("run", {"op": op, "args": args})
if not r.get("ok"):
    print(json.dumps({"status": "rejected", "error": r.get("error"), "code": r.get("code")}, ensure_ascii=False))
    sys.exit(3)
jobs = os.path.join(os.path.dirname(sock), "jobs")
deadline = time.time() + 900
st = {}
while time.time() < deadline:
    try:
        st = json.load(open(os.path.join(jobs, r["job"] + ".json")))
    except (OSError, ValueError):
        st = {}
    if st.get("status") not in (None, "starting", "running"):
        break
    time.sleep(0.5)
out = open(os.path.join(jobs, r["job"] + ".out"), encoding="utf-8", errors="replace").read()
print(json.dumps({"status": st.get("status"), "exit_code": st.get("exit_code"), "output": out}, ensure_ascii=False))
sys.exit(0 if st.get("status") == "succeeded" else 1)
