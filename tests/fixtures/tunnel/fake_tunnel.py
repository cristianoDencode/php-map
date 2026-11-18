"""Túnel de mentira para os testes do túnel: imprime a URL e dorme (como o ngrok)."""

import os
import subprocess
import sys
import time

mode = os.environ.get("FAKE_TUNNEL_MODE", "ok")
pidfile = os.environ.get("FAKE_TUNNEL_PID")
if pidfile:
    with open(pidfile, "w", encoding="utf-8") as out:
        out.write(str(os.getpid()))
print(
    "t=2026-11-09 lvl=info msg=args port=" + (sys.argv[1] if len(sys.argv) > 1 else "?"), flush=True
)
if mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_TUNNEL_CHILD"], "w", encoding="utf-8") as out:
        out.write(str(child.pid))
if mode == "http":
    print("started tunnel url=http://inseguro.exemplo", flush=True)
elif mode not in ("no-url", "exit-early"):
    print("started tunnel url=https://teste.exemplo", flush=True)
if mode == "secret":
    print(
        "authtoken=" + os.environ.get("NGROK_AUTHTOKEN", "") + " ecm_abcdefghijklmnopqrstuvwx",
        flush=True,
    )
if mode == "die":
    time.sleep(1.0)
    sys.exit(3)
if mode == "exit-early":
    sys.exit(2)
time.sleep(60)
