#!/usr/bin/env python3
"""Заглушка backend приёма аудио: чанковая загрузка с докачкой и проверкой sha256.

  python tools/mock_backend.py [--port 8799] [--dir received] [--fail-after N]

--fail-after N: обрывает соединение на N-м чанке (один раз) — имитация потери связи.
"""
import argparse, hashlib, json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATE = {}          # sha -> {"name", "size", "path"}
OPT = None
CHUNKS = {"n": 0, "failed": False}


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_PUT(self):
        u = urlparse(self.path); q = parse_qs(u.query); parts = u.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "uploads":
            return self._json(404, {"error": "not found"})
        sha = parts[1]
        path = os.path.join(OPT.dir, sha[:16] + ".part")   # коротко: MAX_PATH на Windows
        STATE.setdefault(sha, {"name": q.get("name", ["x"])[0], "size": int(q.get("size", ["0"])[0]), "path": path})
        off = os.path.getsize(path) if os.path.exists(path) else 0
        self._json(200, {"offset": off})

    def do_POST(self):
        u = urlparse(self.path); q = parse_qs(u.query); parts = u.path.strip("/").split("/")
        if len(parts) != 3 or parts[1] not in STATE:
            return self._json(404, {"error": "unknown upload"})
        st = STATE[parts[1]]
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if parts[2] == "chunk":
            CHUNKS["n"] += 1
            if OPT.fail_after and CHUNKS["n"] == OPT.fail_after and not CHUNKS["failed"]:
                CHUNKS["failed"] = True
                self.close_connection = True
                return self.connection.close()                     # обрыв «в эфире»
            cur = os.path.getsize(st["path"]) if os.path.exists(st["path"]) else 0
            if int(q["offset"][0]) != cur:
                return self._json(409, {"error": "offset mismatch", "offset": cur})
            os.makedirs(OPT.dir, exist_ok=True)
            with open(st["path"], "ab") as f:
                f.write(body)
            return self._json(200, {"offset": cur + len(body)})
        if parts[2] == "complete":
            data = open(st["path"], "rb").read()
            ok = hashlib.sha256(data).hexdigest() == parts[1]
            final = os.path.join(OPT.dir, st["name"])
            if ok:
                os.replace(st["path"], final)
            return self._json(200, {"sha256_ok": ok, "bytes": len(data), "stored": final if ok else None})
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *a):
        print("[backend]", fmt % a, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8799); p.add_argument("--dir", default="received")
    p.add_argument("--fail-after", type=int, default=0)
    OPT = p.parse_args(); OPT.dir = os.path.abspath(OPT.dir); os.makedirs(OPT.dir, exist_ok=True)
    print(f"mock backend on http://127.0.0.1:{OPT.port}, storing to {OPT.dir}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", OPT.port), H).serve_forever()
