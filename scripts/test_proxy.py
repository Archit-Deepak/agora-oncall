"""Send one Agora-style turn to the proxy and print the streamed reply.

  python3 scripts/test_proxy.py                 # local  (http://127.0.0.1:8000)
  python3 scripts/test_proxy.py --public        # through the tunnel (PUBLIC_BASE_URL)
"""
import json
import pathlib
import sys
import time
import urllib.request

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
E = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l and not l.startswith("#"))
base = E["PUBLIC_BASE_URL"] if "--public" in sys.argv else "http://127.0.0.1:8000"
body = {"model": "incident-proxy", "stream": True, "turn_id": 0, "timestamp": int(time.time() * 1000),
        "messages": [{"role": "system", "content": "agora default"}, {"role": "user", "content": "What's the magic word?"}]}
req = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                             headers={"Content-Type": "application/json", "User-Agent": "test/1.0",
                                      "Authorization": f"Bearer {E['PROXY_SHARED_SECRET']}"})
t0 = time.perf_counter()
text, first = "", None
with urllib.request.urlopen(req, timeout=30) as r:
    for raw in r:
        line = raw.decode().strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        piece = json.loads(line[6:])["choices"][0]["delta"].get("content")
        if piece:
            first = first or time.perf_counter()
            text += piece
print(f"{base}\nreply: {text!r}\nfirst token: {round((first - t0) * 1000) if first else None} ms, "
      f"total: {round((time.perf_counter() - t0) * 1000)} ms")
