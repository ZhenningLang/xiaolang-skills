"""Local page service: authorization, blindness, durable answers, restart behavior."""

import http.client
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from evals.brainstorm import serve, sheet
from evals.brainstorm.backends import FakeBackend
from evals.brainstorm.budget import BudgetLedger
from evals.brainstorm.tests.test_sheet import A, ANSWER, B, EXAM_ID, ROOT, copy_run, fixture_exam, key_of, public_of


LINEAGE = re.compile(r"cfg_canary|model_canary|method_canary|card_canary|i-[0-9a-f]{12}|m-\d{3}"
                     r"|idea_id|mechanism_id|config_reps|representative")


class ServerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run_dir = copy_run(self.root)
        self.exam = fixture_exam()

    def issue(self, sheet_id="s_all", **options):
        sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id=sheet_id, seed=7, all_view=True, **options)

    def start(self, sheet_id="s_all"):
        server = serve.make_server(self.run_dir, sheet_id)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        self.port = server.server_address[1]
        self.token = server.token
        return server

    def raw(self, method, path, *, body=None, headers=None, host=None, port=None):
        """A real socket request: the path and headers reach the server exactly as written."""
        port = port or self.port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(connection.close)
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if body is not None and "Content-Length" not in (headers or {}):
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())

    def get(self, path="/api/sheet", token=True):
        return self.raw("GET", f"{path}?t={self.token}" if token else path)

    def post(self, payload, *, origin=True, content_type="application/json", raw_body=None):
        headers = {"Content-Type": content_type}
        if origin is True:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            headers["Origin"] = origin
        body = raw_body if raw_body is not None else json.dumps(payload, ensure_ascii=False).encode()
        return self.raw("POST", f"/api/answers?t={self.token}", body=body, headers=headers)

    def answer(self, item_id, **changes):
        return {"sheet_id": "s_all", "answers": [{"item_id": item_id, **ANSWER, **changes}]}

    def disk(self, sheet_id="s_all"):
        path = self.run_dir / "sheets" / sheet_id / "answers.json"
        return path.read_bytes() if path.exists() else None

    def items(self, sheet_id="s_all"):
        return public_of(self.run_dir, sheet_id)["items"]

    def assertJsonRefusal(self, result, code):
        status, body, headers = result
        self.assertEqual(status, code, body)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        data = json.loads(body)
        self.assertIs(data["ok"], False)
        self.assertTrue(data["error"])


class AuthorizationTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.issue()
        self.start()

    def test_binds_only_to_loopback_on_a_random_port(self):
        server = serve.make_server(self.run_dir, "s_all")
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertNotEqual(server.server_address[1], self.port)
        self.assertNotEqual(server.token, self.token)

    def test_missing_or_wrong_token_is_a_json_403(self):
        for path in ("/", "/api/sheet", "/?t=", "/?t=wrong", "/api/sheet?t=wrong", f"/?t={self.token}&t=wrong"):
            with self.subTest(path):
                self.assertJsonRefusal(self.raw("GET", path), 403)

    def test_wrong_host_is_refused_even_with_the_token(self):
        for host in ("evil.example", f"localhost:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port + 1}"):
            with self.subTest(host):
                self.assertJsonRefusal(self.raw("GET", f"/?t={self.token}", host=host), 403)

    def test_bad_posts_are_refused_and_disk_is_unchanged(self):
        item = self.items()[0]["item_id"]
        payload = self.answer(item)
        # Only the size is declared: the server refuses without reading, so a real body would race a reset.
        oversized = self.raw("POST", f"/api/answers?t={self.token}", body=b"{}", headers={
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Length": str(serve.MAX_BODY + 1)})
        chunked = self.raw("POST", f"/api/answers?t={self.token}", body=b"{}", headers={
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Length": "2", "Transfer-Encoding": "chunked"})
        cases = [("wrong origin", self.post(payload, origin="http://evil.example"), 403),
                 ("null origin", self.post(payload, origin="null"), 403),
                 ("other port origin", self.post(payload, origin=f"http://127.0.0.1:{self.port + 1}"), 403),
                 ("missing origin", self.post(payload, origin=False), 403),
                 ("form type", self.post(payload, content_type="text/plain"), 400),
                 ("too large", oversized, 400), ("chunked", chunked, 400)]
        for name, result, code in cases:
            with self.subTest(name):
                self.assertJsonRefusal(result, code)
                self.assertIsNone(self.disk())
        self.assertJsonRefusal(self.raw("POST", "/api/answers", body=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}), 403)
        self.assertIsNone(self.disk())

    def test_no_path_reaches_any_file_even_with_the_token(self):
        secrets = [(self.run_dir / "sheets/s_all/sheet.key.json").read_text(encoding="utf-8")[:40].encode()]
        for path in ("/sheet.key.json", "/../sheet.key.json", "/%2e%2e/sheet.key.json", "/%2e%2e%2fsheet.key.json",
                     "/sheet.json", "/answers.json", "/web/index.html", "/index.html", "//sheet.key.json",
                     "/api/sheet/../../sheet.key.json", "/api/answers"):
            with self.subTest(path):
                result = self.get(path)
                self.assertJsonRefusal(result, 403)
                self.assertFalse(any(secret in result[1] for secret in secrets))
                self.assertNotIn(b"mechanism_id", result[1])

    def test_other_verbs_are_refused(self):
        for verb in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND"):
            with self.subTest(verb):
                self.assertJsonRefusal(self.raw(verb, f"/?t={self.token}"), 403)
        status, _, _ = self.raw("HEAD", f"/?t={self.token}")
        self.assertEqual(status, 403)

    def test_security_headers_on_every_response(self):
        for result in (self.get("/"), self.get("/api/sheet"), self.raw("GET", "/nope")):
            headers = result[2]
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Python", headers.get("Server", ""))


class BlindTests(ServerCase):
    def assertBlind(self, data):
        self.assertIsNone(LINEAGE.search(data.decode()), data[:400])

    def exercise(self, sheet_id):
        self.start(sheet_id)
        status, page, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertBlind(page)
        status, body, _ = self.get("/api/sheet")
        self.assertEqual(status, 200)
        self.assertBlind(body)
        state = json.loads(body)
        self.assertEqual(set(state), {"sheet_id", "kind", "question", "constraints", "items", "answers", "progress"})
        for item in state["items"]:
            self.assertEqual(set(item), {"item_id", "text", "variants"})
        key = key_of(self.run_dir, sheet_id)
        self.assertEqual({key["items"][row["item_id"]]["type"] for row in state["items"]}, {"target", "check_bad", "repeat"})
        item = state["items"][0]["item_id"]
        ok = self.post({"sheet_id": sheet_id, "answers": [{"item_id": item, **ANSWER}]})
        self.assertEqual(ok[0], 200)
        self.assertBlind(ok[1])
        for result in (self.post({"sheet_id": sheet_id, "answers": [{"item_id": "x-0", **ANSWER}]}),
                       self.post({"sheet_id": "other", "answers": []}), self.post({"nope": 1}),
                       self.post(None, raw_body=b"{"), self.raw("GET", "/?t=wrong"), self.get("/nope")):
            self.assertBlind(result[1])

    def test_lineage_inside_the_original_text_never_reaches_a_response(self):
        taint = ("来源 model_canary_x，采用method_canary_y，编号m-001和i-0123456789ab。"
                 "又如card_canary_1-方法、方法-method_canary_y、cfg_canary_a_配置、ｍ－００１。")
        for path in (self.run_dir / EXAM_ID).glob("*/rep-*/ideas.jsonl"):
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            path.write_text("".join(json.dumps({**row, "text": row["text"] + taint}, ensure_ascii=False) + "\n" for row in lines), encoding="utf-8")
        self.issue()
        self.start()
        body = self.get()[1]
        self.assertBlind(body)
        self.assertNotIn("ｍ－００１", json.loads(body)["items"][0]["text"])
        self.assertIn("[已隐去]", json.loads(body)["items"][0]["text"])

    def test_full_view_exposes_no_lineage(self):
        self.issue()
        self.exercise("s_all")

    def test_comparison_view_exposes_no_lineage(self):
        sheet.make_sheet(self.run_dir, self.exam, kind="score", sheet_id="s_cmp", seed=7, configs=[A, B], per_unit=6,
                         backend=FakeBackend(responses=sheet.fake_rewrite),
                         ledger=BudgetLedger(self.root / "ledger.jsonl", "50"), model="rewrite_model")
        self.exercise("s_cmp")

    def test_group_view_exposes_no_lineage(self):
        sheet.make_sheet(self.run_dir, self.exam, kind="group", sheet_id="s_grp", seed=7, count=10)
        self.start("s_grp")
        body = self.get("/api/sheet")[1]
        self.assertBlind(body)
        self.assertEqual(json.loads(body)["answers"], {"groups": []})

    def test_item_ids_cannot_be_derived_from_lineage(self):
        self.issue()
        key = key_of(self.run_dir, "s_all")
        self.assertTrue(all(re.fullmatch(r"x-[0-9a-f]{16}", item) for item in key["items"]))

    def test_service_works_without_the_key_and_never_mentions_it(self):
        self.issue()
        (self.run_dir / "sheets/s_all/sheet.key.json").unlink()
        self.start()
        self.assertEqual(self.get("/api/sheet")[0], 200)
        self.assertEqual(self.post(self.answer(self.items()[0]["item_id"]))[0], 200)
        source = Path(serve.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sheet.key", source)
        self.assertNotIn("KEY_NAME", source)
        self.assertNotIn("load_key", source)

    def test_public_readers_never_open_the_key(self):
        self.issue()
        key_path = self.run_dir / "sheets/s_all/sheet.key.json"
        key_path.unlink()
        key_path.mkdir()  # Opening it as a file would now raise; the public readers must not try.
        sheet.public_state(self.run_dir, "s_all")
        sheet.merge_answers(self.run_dir, "s_all", self.answer(self.items()[0]["item_id"]))
        sheet.load_public_sheet(self.run_dir, "s_all")

    def test_extra_fields_in_sheet_json_refuse_to_start(self):
        self.issue()
        path = self.run_dir / "sheets/s_all/sheet.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        original = json.dumps(data)
        for broken in ({**data, "exam_id": EXAM_ID},
                       {**data, "items": [{**data["items"][0], "mechanism_id": "m-001"}]}):
            path.write_text(json.dumps(broken))
            with self.assertRaisesRegex(ValueError, "unexpected"):
                serve.make_server(self.run_dir, "s_all")
        path.write_text(original)
        serve.make_server(self.run_dir, "s_all").server_close()


class AnswerTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.issue()
        self.start()

    def test_batches_merge_by_item_and_are_on_disk_when_success_returns(self):
        items = self.items()
        status, body, _ = self.post(self.answer(items[0]["item_id"]))
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "done": 1, "total": 20}))
        self.assertIn(items[0]["item_id"], json.loads(self.disk())["answers"])
        status, body, _ = self.post(self.answer(items[1]["item_id"], care=False, fatal_reason="成本太高"))
        self.assertEqual(json.loads(body)["done"], 2)
        stored = json.loads(self.disk())
        self.assertEqual(set(stored), {"sheet_id", "kind", "answers"})
        self.assertEqual(stored["answers"][items[1]["item_id"]], {"unthought": True, "care": False, "fatal_reason": "成本太高", "want_to_try": False})
        state = json.loads(self.get()[1])
        self.assertEqual(state["progress"], {"done": 2, "total": 20})
        self.assertEqual(state["answers"], stored["answers"])
        self.post(self.answer(items[0]["item_id"], want_to_try=True))
        self.assertIs(json.loads(self.disk())["answers"][items[0]["item_id"]]["want_to_try"], True)
        self.assertEqual(json.loads(self.get()[1])["progress"]["done"], 2)

    def test_any_invalid_row_rejects_the_whole_batch(self):
        good, other = self.items()[0]["item_id"], self.items()[1]["item_id"]
        valid = {"item_id": good, **ANSWER}
        self.post({"sheet_id": "s_all", "answers": [{"item_id": other, **ANSWER}]})
        before = self.disk()
        bad_rows = [{"item_id": "x-unknown", **ANSWER}, {"item_id": other, **ANSWER, "unthought": 1},
                    {"item_id": other, **ANSWER, "care": "true"}, {"item_id": other, **ANSWER, "want_to_try": None},
                    {"item_id": other, **ANSWER, "fatal_reason": "   "}, {"item_id": other, **ANSWER, "fatal_reason": ""},
                    {"item_id": other, **ANSWER, "fatal_reason": 3}, {"item_id": other, **ANSWER, "extra": 1},
                    {"item_id": other, "unthought": True}, {"item_id": 7, **ANSWER}, valid]
        for row in bad_rows:
            with self.subTest(row):
                self.assertJsonRefusal(self.post({"sheet_id": "s_all", "answers": [valid, row]}), 400)
                self.assertEqual(self.disk(), before)
        for payload in ({"sheet_id": "other", "answers": [valid]}, {"answers": [valid]},
                        {"sheet_id": "s_all", "answers": valid}, {"sheet_id": "s_all", "answers": [valid], "x": 1},
                        {"sheet_id": "s_all", "groups": [[good]]}, [valid]):
            with self.subTest(payload):
                self.assertJsonRefusal(self.post(payload), 400)
                self.assertEqual(self.disk(), before)
        duplicate = ('{"sheet_id":"s_all","sheet_id":"s_all","answers":[]}').encode()
        self.assertJsonRefusal(self.post(None, raw_body=duplicate), 400)
        repeated = ('{"sheet_id":"s_all","answers":[{"item_id":"%s","item_id":"%s","unthought":true,"care":true,'
                    '"fatal_reason":null,"want_to_try":false}]}' % (good, good)).encode()
        self.assertJsonRefusal(self.post(None, raw_body=repeated), 400)
        self.assertJsonRefusal(self.post(None, raw_body=b"not json"), 400)
        self.assertEqual(self.disk(), before)

    def test_error_messages_are_readable_chinese(self):
        status, body, _ = self.post({"sheet_id": "s_all", "answers": [{"item_id": self.items()[0]["item_id"], **ANSWER, "fatal_reason": " "}]})
        self.assertEqual(status, 400)
        self.assertRegex(json.loads(body)["error"], "[一-鿿]")

    def test_concurrent_posts_lose_nothing(self):
        items = self.items()
        results = []
        def send(item):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            body = json.dumps(self.answer(item["item_id"])).encode()
            connection.request("POST", f"/api/answers?t={self.token}", body=body, headers={
                "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
            results.append(connection.getresponse().status)
            connection.close()
        threads = [threading.Thread(target=send, args=(item,)) for item in items]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(results, [200] * len(items))
        self.assertEqual(set(json.loads(self.disk())["answers"]), {item["item_id"] for item in items})

    def test_closed_sheet_returns_403_and_keeps_answers(self):
        items = self.items()
        self.post({"sheet_id": "s_all", "answers": [{"item_id": item["item_id"], **ANSWER} for item in items]})
        sheet.reveal(self.run_dir, "s_all")
        before = self.disk()
        self.assertJsonRefusal(self.post(self.answer(items[0]["item_id"], care=False)), 403)
        self.assertEqual(self.disk(), before)
        self.assertEqual(self.get()[0], 200)


class GroupAnswerTests(ServerCase):
    def setUp(self):
        super().setUp()
        sheet.make_sheet(self.run_dir, self.exam, kind="group", sheet_id="s_grp", seed=7, count=6)
        self.start("s_grp")
        self.ids = [row["item_id"] for row in self.items("s_grp")]

    def test_groups_replace_as_a_whole_and_may_cover_only_some_items(self):
        status, body, _ = self.post({"sheet_id": "s_grp", "groups": [self.ids[:2], self.ids[2:3]]})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "done": 3, "total": 6}))
        status, body, _ = self.post({"sheet_id": "s_grp", "groups": [self.ids[:1]]})
        self.assertEqual(json.loads(body)["done"], 1)
        self.assertEqual(json.loads(self.disk("s_grp"))["groups"], [self.ids[:1]])
        self.assertEqual(json.loads(self.get()[1])["answers"], {"groups": [self.ids[:1]]})

    def test_bad_groups_are_rejected(self):
        self.post({"sheet_id": "s_grp", "groups": [self.ids[:1]]})
        before = self.disk("s_grp")
        for groups in ([[]], [self.ids[:1], self.ids[:1]], [[self.ids[0], self.ids[0]]], [["x-unknown"]], [[3]], ["x"], "x"):
            with self.subTest(groups):
                self.assertJsonRefusal(self.post({"sheet_id": "s_grp", "groups": groups}), 400)
                self.assertEqual(self.disk("s_grp"), before)


class ProcessTests(ServerCase):
    def launch(self, sheet_id="s_all"):
        process = subprocess.Popen([sys.executable, "-m", "evals.brainstorm", "serve", "--run-dir", str(self.run_dir), "--sheet-id", sheet_id],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.wait, timeout=10)
        self.addCleanup(process.kill)
        url = urlsplit(process.stdout.readline().strip())
        self.assertEqual(url.hostname, "127.0.0.1")
        self.port, self.token = url.port, parse_qs(url.query)["t"][0]
        return process

    def test_answers_survive_a_real_process_restart(self):
        self.issue()
        items = self.items()
        first = self.launch()
        old_port, old_token = self.port, self.token
        self.assertEqual(self.post(self.answer(items[0]["item_id"], fatal_reason="不成"))[0], 200)
        self.assertEqual(self.post(self.answer(items[1]["item_id"]))[0], 200)
        first.terminate()
        first.wait(timeout=10)
        self.launch()
        self.assertNotEqual((self.port, self.token), (old_port, old_token))
        state = json.loads(self.get()[1])
        self.assertEqual(state["progress"], {"done": 2, "total": 20})
        self.assertEqual(state["answers"][items[0]["item_id"]]["fatal_reason"], "不成")

    def test_corrupted_answers_refuse_to_start_without_overwriting(self):
        self.issue()
        path = self.run_dir / "sheets/s_all/answers.json"
        for text in ("{broken", '{"sheet_id":"s_all","kind":"score","answers":{"x-unknown":{}}}',
                     '{"sheet_id":"other","kind":"score","answers":{}}'):
            with self.subTest(text):
                path.write_text(text)
                result = subprocess.run([sys.executable, "-m", "evals.brainstorm", "serve", "--run-dir", str(self.run_dir),
                                         "--sheet-id", "s_all"], cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(path.read_text(), text)

    def test_url_is_flushed_first_and_interrupt_exits_zero(self):
        self.issue()
        process = self.launch()
        self.assertEqual(self.get()[0], 200)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertNotIn(self.token, process.stderr.read())
