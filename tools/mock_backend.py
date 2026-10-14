#!/usr/bin/env python3
"""Заглушка backend приёма аудио: чанковая загрузка с докачкой и проверкой sha256.

  python tools/mock_backend.py [--port 8799] [--dir received] [--fail-after N]

--fail-after N: обрывает соединение на N-м чанке (один раз) — имитация потери связи.

Протокол (см. CLAUDE.md):
  PUT    /uploads/{sha256}?name=&size=   → {"offset": n}
  POST   /uploads/{sha256}/chunk?offset=n → {"offset": n+len}; 409 при несовпадении offset
  POST   /uploads/{sha256}/complete       → {"sha256_ok": bool, ...}; при false .part удаляется
  DELETE /uploads/{sha256}                → сброс незавершённой загрузки
"""
import argparse, hashlib, json, os, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

STATE = {}          # sha -> {"name", "size", "path"}
LOCK = threading.Lock()
OPT = None
CHUNKS = {"n": 0, "failed": False}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def safe_name(raw):
    """Только базовое имя: никаких '..', разделителей и пустых строк (иначе уход из --dir)."""
    n = os.path.basename(unquote(raw or "").replace("\\", "/")).strip()
    if not n or n in (".", "..") or n.startswith("."):
        return None
    return n


def unique_final(dir_, name, sha):
    """Не перезаписывать чужой файл с тем же именем: second.wav → second.<sha8>.wav."""
    final = os.path.join(dir_, name)
    if not os.path.exists(final):
        return final
    with open(final, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() == sha:
            return final                                           # тот же файл — идемпотентно
    stem, ext = os.path.splitext(name)
    return os.path.join(dir_, f"{stem}.{sha[:8]}{ext}")


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _route(self):
        u = urlparse(self.path); parts = u.path.strip("/").split("/")
        if len(parts) not in (2, 3) or parts[0] != "uploads" or not SHA_RE.match(parts[1]):
            return None, None, None
        return parts[1], (parts[2] if len(parts) == 3 else None), parse_qs(u.query)

    def do_PUT(self):
        sha, action, q = self._route()
        if sha is None or action is not None:
            return self._json(404, {"error": "not found"})
        name = safe_name(q.get("name", [""])[0])
        if name is None:
            return self._json(400, {"error": "bad name"})
        path = os.path.join(OPT.dir, sha[:16] + ".part")           # коротко: MAX_PATH на Windows
        with LOCK:
            STATE.setdefault(sha, {"name": name, "size": int(q.get("size", ["0"])[0]), "path": path})
            off = os.path.getsize(path) if os.path.exists(path) else 0
        self._json(200, {"offset": off})

    def do_DELETE(self):
        sha, action, _ = self._route()
        if sha is None or action is not None:
            return self._json(404, {"error": "not found"})
        with LOCK:
            st = STATE.pop(sha, None)
            path = st["path"] if st else os.path.join(OPT.dir, sha[:16] + ".part")
            existed = os.path.exists(path)
            if existed:
                os.remove(path)
        self._json(200, {"reset": existed})

    def do_POST(self):
        sha, action, q = self._route()
        if sha is None or sha not in STATE:
            return self._json(404, {"error": "unknown upload"})
        st = STATE[sha]
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if action == "chunk":
            CHUNKS["n"] += 1
            if OPT.fail_after and CHUNKS["n"] == OPT.fail_after and not CHUNKS["failed"]:
                CHUNKS["failed"] = True
                self.close_connection = True
                return self.connection.close()                     # обрыв «в эфире»
            try:
                want = int(q["offset"][0])
            except (KeyError, ValueError):
                return self._json(400, {"error": "offset required"})
            with LOCK:
                cur = os.path.getsize(st["path"]) if os.path.exists(st["path"]) else 0
                if want != cur:
                    return self._json(409, {"error": "offset mismatch", "offset": cur})
                if cur + len(body) > st["size"] > 0:
                    return self._json(400, {"error": "chunk exceeds declared size", "offset": cur})
                os.makedirs(OPT.dir, exist_ok=True)
                with open(st["path"], "ab") as f:
                    f.write(body)
            return self._json(200, {"offset": cur + len(body)})
        if action == "complete":
            with LOCK:
                if not os.path.exists(st["path"]):
                    return self._json(409, {"sha256_ok": False, "error": "nothing uploaded", "reset": True})
                h = hashlib.sha256(); size = 0
                with open(st["path"], "rb") as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        h.update(block); size += len(block)
                ok = h.hexdigest() == sha
                if ok:
                    final = unique_final(OPT.dir, st["name"], sha)
                    os.replace(st["path"], final)
                    STATE.pop(sha, None)
                    return self._json(200, {"sha256_ok": True, "bytes": size, "stored": final})
                os.remove(st["path"])                              # битая загрузка: сброс, клиент начнёт заново
                STATE.pop(sha, None)
                return self._json(200, {"sha256_ok": False, "bytes": size, "stored": None, "reset": True})
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *a):
        if not OPT.quiet:
            print("[backend]", fmt % a, flush=True)


def make_server(port=8799, dir_="received", fail_after=0, quiet=False):
    """Для тестов: сервер в том же процессе (port=0 — любой свободный)."""
    global OPT
    OPT = argparse.Namespace(port=port, dir=os.path.abspath(dir_), fail_after=fail_after, quiet=quiet)
    os.makedirs(OPT.dir, exist_ok=True)
    STATE.clear(); CHUNKS.update(n=0, failed=False)
    return ThreadingHTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8799); p.add_argument("--dir", default="received")
    p.add_argument("--fail-after", type=int, default=0)
    a = p.parse_args()
    srv = make_server(a.port, a.dir, a.fail_after)
    print(f"mock backend on http://127.0.0.1:{srv.server_address[1]}, storing to {OPT.dir}/", flush=True)
    srv.serve_forever()
