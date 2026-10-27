#!/usr/bin/env python3
"""Optional sync backend for the practice app.

Stores one JSON document per user in SQLite and merges incoming data into it.
Standard library only. The user name comes from the X-Remote-User header, which
the reverse proxy sets from its own authentication. Never expose this server
directly: anyone who can reach it could send that header themselves.

Endpoints:
  GET  /api/data  -> the stored document (empty if none)
  PUT  /api/data  -> merge the body into the stored document, return the result
  GET  /healthz   -> "ok" (no user header needed)
"""
import json
import math
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("DB_PATH", "/data/sync.db")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY = 200 * 1024
MAX_CHARS = 2000
USER_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")


def empty():
    return {"wrong": {}, "last": None, "lastAt": 0, "resetAt": 0}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def clean(doc):
    """Validate and normalise a client document. Raises ValueError if invalid."""
    if not isinstance(doc, dict):
        raise ValueError("body must be an object")
    out = empty()
    for k in ("lastAt", "resetAt"):
        v = doc.get(k, 0)
        if not _num(v) or v < 0:
            raise ValueError(k + " must be a non-negative number")
        out[k] = v
    wrong = doc.get("wrong", {})
    if not isinstance(wrong, dict) or len(wrong) > MAX_CHARS:
        raise ValueError("wrong must be an object")
    for ch, e in wrong.items():
        if not isinstance(ch, str) or not 1 <= len(ch) <= 8 or not isinstance(e, dict):
            raise ValueError("bad wrong entry")
        c, t = e.get("c"), e.get("t")
        w = e.get("w", c)
        if not (_num(c) and _num(t) and _num(w)) or c < 0 or t < 0 or w < 0:
            raise ValueError("bad wrong entry values")
        out["wrong"][ch] = {"c": c, "t": t, "w": w}
    last = doc.get("last")
    if last is not None:
        if not isinstance(last, dict) or len(last) > MAX_CHARS:
            raise ValueError("last must be an object or null")
        for ch, n in last.items():
            if not isinstance(ch, str) or not 1 <= len(ch) <= 8 or not _num(n) or n < 0:
                raise ValueError("bad last entry")
        out["last"] = dict(last)
    return out


def merge(a, b):
    """Merge two documents. Merging a document into itself is a no-op, and the per-character
    part is commutative.

    - resetAt: the later one wins, and entries last missed before it are dropped.
    - per character: c = max, t = max, w = the one from the newer t (the smaller w on a tie,
      so a decay recorded on one device is kept).
    - last session: the one with the later lastAt.
    """
    reset = max(a["resetAt"], b["resetAt"])
    wrong = {}
    for ch in set(a["wrong"]) | set(b["wrong"]):
        es = [d["wrong"][ch] for d in (a, b) if ch in d["wrong"] and d["wrong"][ch]["t"] >= reset]
        if not es:
            continue
        newest = max(e["t"] for e in es)
        wrong[ch] = {
            "c": max(e["c"] for e in es),
            "t": newest,
            "w": min(e["w"] for e in es if e["t"] == newest),
        }
    src = b if b["lastAt"] > a["lastAt"] else a
    last = src["last"] if src["lastAt"] >= reset else None
    return {"wrong": wrong, "last": last, "lastAt": max(a["lastAt"], b["lastAt"]), "resetAt": reset}


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS data (user TEXT PRIMARY KEY, body TEXT NOT NULL)")
    return db


def load(db, user):
    row = db.execute("SELECT body FROM data WHERE user=?", (user,)).fetchone()
    return json.loads(row[0]) if row else empty()


class Handler(BaseHTTPRequestHandler):
    server_version = "syncd"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj=None, text=None):
        body = (text if text is not None else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if text is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _user(self):
        u = self.headers.get("X-Remote-User", "")
        if not u:
            self._send(401, {"error": "login required"})
            return None
        if not USER_RE.match(u):
            self._send(400, {"error": "bad user name"})
            return None
        return u

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, text="ok")
        if self.path != "/api/data":
            return self._send(404, {"error": "not found"})
        user = self._user()
        if user is None:
            return
        db = connect()
        try:
            self._send(200, load(db, user))
        finally:
            db.close()

    def do_PUT(self):
        if self.path != "/api/data":
            return self._send(404, {"error": "not found"})
        user = self._user()
        if user is None:
            return
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411, {"error": "Content-Length required"})
        if n < 0 or n > MAX_BODY:
            return self._send(413, {"error": "body too large"})
        try:
            incoming = clean(json.loads(self.rfile.read(n)))
        except (ValueError, UnicodeDecodeError) as e:
            return self._send(400, {"error": str(e)})
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            merged = merge(load(db, user), incoming)
            db.execute(
                "INSERT INTO data (user, body) VALUES (?, ?) "
                "ON CONFLICT(user) DO UPDATE SET body=excluded.body",
                (user, json.dumps(merged)),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        self._send(200, merged)


def main():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    connect().close()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
