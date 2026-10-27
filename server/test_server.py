import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
import server  # noqa: E402


def doc(wrong=None, last=None, last_at=0, reset_at=0):
    return {"wrong": wrong or {}, "last": last, "lastAt": last_at, "resetAt": reset_at}


def e(c, t, w=None):
    return {"c": c, "t": t, "w": c if w is None else w}


class MergeTests(unittest.TestCase):
    def test_max_count_newest_weight(self):
        a = doc({"あ": e(5, 100, 4)})
        b = doc({"あ": e(3, 200, 1)})
        m = server.merge(a, b)["wrong"]["あ"]
        self.assertEqual(m, {"c": 5, "t": 200, "w": 1})

    def test_tie_keeps_decay(self):
        a = doc({"あ": e(4, 100, 3)})
        b = doc({"あ": e(4, 100, 2)})
        self.assertEqual(server.merge(a, b)["wrong"]["あ"]["w"], 2)

    def test_union_and_commutative(self):
        a = doc({"あ": e(1, 10)})
        b = doc({"い": e(2, 20)})
        self.assertEqual(server.merge(a, b), server.merge(b, a))
        self.assertEqual(set(server.merge(a, b)["wrong"]), {"あ", "い"})

    def test_idempotent(self):
        a = doc({"あ": e(3, 10, 2)}, {"あ": 1}, 5)
        self.assertEqual(server.merge(a, a), a)

    def test_reset_drops_old_entries_and_session(self):
        old = doc({"あ": e(9, 100)}, {"あ": 2}, 100)
        reset = doc({}, None, 0, 500)
        m = server.merge(old, reset)
        self.assertEqual(m["wrong"], {})
        self.assertIsNone(m["last"])
        self.assertEqual(m["resetAt"], 500)

    def test_entries_after_reset_survive(self):
        a = doc({"あ": e(1, 600)}, None, 0, 500)
        b = doc({"い": e(5, 100)})
        self.assertEqual(set(server.merge(a, b)["wrong"]), {"あ"})

    def test_last_session_newer_wins(self):
        a = doc(last={"あ": 1}, last_at=10)
        b = doc(last={"い": 2}, last_at=20)
        self.assertEqual(server.merge(a, b)["last"], {"い": 2})
        self.assertEqual(server.merge(b, a)["last"], {"い": 2})


class CleanTests(unittest.TestCase):
    def test_rejects_bad_input(self):
        for bad in (
            [],
            {"wrong": []},
            {"wrong": {"a": {"c": "x", "t": 1}}},
            {"wrong": {"a": {"c": 1, "t": -1}}},
            {"wrong": {"": {"c": 1, "t": 1}}},
            {"wrong": {"a" * 9: {"c": 1, "t": 1}}},
            {"wrong": {"a": {"c": float("inf"), "t": 1}}},
            {"lastAt": -1},
            {"last": {"a": "x"}},
            {"resetAt": True},
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                server.clean(bad)

    def test_fills_missing_weight(self):
        out = server.clean({"wrong": {"あ": {"c": 4, "t": 9}}})
        self.assertEqual(out["wrong"]["あ"]["w"], 4)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.connect().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, body=None, user="alice", raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if user:
            req.add_header("X-Remote-User", user)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    def test_healthz_needs_no_user(self):
        self.assertEqual(self.call("GET", "/healthz", user=None), (200, "ok"))

    def test_missing_user_is_401(self):
        self.assertEqual(self.call("GET", "/api/data", user=None)[0], 401)
        self.assertEqual(self.call("PUT", "/api/data", doc(), user=None)[0], 401)

    def test_bad_user_is_400(self):
        self.assertEqual(self.call("GET", "/api/data", user="a/b")[0], 400)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("PUT", "/nope", doc())[0], 404)

    def test_new_user_gets_empty(self):
        code, body = self.call("GET", "/api/data", user="fresh")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), server.empty())

    def test_roundtrip_and_merge_across_devices(self):
        self.call("PUT", "/api/data", doc({"あ": e(3, 100, 3)}), user="bob")
        code, body = self.call("PUT", "/api/data", doc({"い": e(1, 50), "あ": e(2, 200, 1)}), user="bob")
        self.assertEqual(code, 200)
        got = json.loads(self.call("GET", "/api/data", user="bob")[1])
        self.assertEqual(got, json.loads(body))
        self.assertEqual(got["wrong"]["あ"], {"c": 3, "t": 200, "w": 1})
        self.assertIn("い", got["wrong"])

    def test_users_are_isolated(self):
        self.call("PUT", "/api/data", doc({"あ": e(1, 1)}), user="u1")
        got = json.loads(self.call("GET", "/api/data", user="u2")[1])
        self.assertEqual(got["wrong"], {})

    def test_reset_clears_server_copy(self):
        self.call("PUT", "/api/data", doc({"あ": e(5, 100)}), user="carol")
        code, body = self.call("PUT", "/api/data", doc(reset_at=1000), user="carol")
        self.assertEqual(json.loads(body)["wrong"], {})
        stale = self.call("PUT", "/api/data", doc({"あ": e(5, 100)}), user="carol")
        self.assertEqual(json.loads(stale[1])["wrong"], {})

    def test_oversized_body_is_413(self):
        self.assertEqual(self.call("PUT", "/api/data", raw=b"x" * (server.MAX_BODY + 1))[0], 413)

    def test_invalid_json_is_400(self):
        self.assertEqual(self.call("PUT", "/api/data", raw=b"{nope")[0], 400)
        self.assertEqual(self.call("PUT", "/api/data", {"wrong": {"a": {"c": "x", "t": 1}}})[0], 400)

    def test_concurrent_puts_do_not_lose_entries(self):
        def put(i):
            self.call("PUT", "/api/data", doc({"c%d" % i: e(1, 100 + i)}), user="race")
        ts = [threading.Thread(target=put, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        got = json.loads(self.call("GET", "/api/data", user="race")[1])
        self.assertEqual(len(got["wrong"]), 20)


if __name__ == "__main__":
    unittest.main()
