"""Tests for the review step, the organise step and the reports. Run: python3 -m unittest discover -s skills/brainstorm/tests < /dev/null"""

import contextlib
import http.client
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))

import brainstorm as bs  # noqa: E402
import review  # noqa: E402
from test_brainstorm import FakeHost, data_block, output_path  # noqa: E402

DEFAULT_METHODS = ["analogy", "reverse", "person", "resources"]


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bs.main(list(args))
    return code, out.getvalue(), err.getvalue()


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "run"
        (self.root / "message.txt").write_text("Look at this.\n\nHow can a small open-source project find maintainers?</script>\n", encoding="utf-8")
        (self.root / "question.txt").write_text("How can a small open-source project find maintainers?\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def draft(self, *extra, cards=True, lang="en"):
        args = ["review", "--dir", str(self.dir), "--message-file", str(self.root / "message.txt"),
                "--question-file", str(self.root / "question.txt"), "--constraint", "No telemetry",
                "--suggest-constraint", "Two hours a week of maintainer time",
                "--model-choice", "sonnet=Sonnet", "--model-choice", "opus=Opus", "--model-default", "sonnet",
                "--measured-model", "sonnet", "--lang", lang]
        if cards:
            args += ["--cards", str(FIXTURES / "cards.jsonl"), "--cards-source", "agent"]
        code, out, err = cli(*args, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def draft_json(self):
        return json.loads((self.dir / "review" / "draft.json").read_text(encoding="utf-8"))

    def answer(self, **change):
        draft = self.draft_json()
        value = {"question": draft["question"], "constraints": [{"text": "No telemetry", "source": "user"}],
                 "cards": draft["cards"], "cards_source": draft["cards_source"], "methods": list(DEFAULT_METHODS),
                 "profile": "full", "ideas_per_agent": 10, "models": {"generate": "sonnet", "group": "opus"}, "lang": "en"}
        value.update(change)
        return value

    def confirm(self, value):
        path = self.root / "answer.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return cli("confirm-review", "--dir", str(self.dir), "--answer-file", str(path))


class DraftTests(ReviewCase):
    def test_draft_holds_every_section(self):
        result = self.draft()
        draft = self.draft_json()
        self.assertEqual(draft["question"], "How can a small open-source project find maintainers?")
        self.assertIn("Look at this.", draft["message"])
        self.assertEqual(draft["constraints"], [{"text": "No telemetry", "source": "user"},
                                                {"text": "Two hours a week of maintainer time", "source": "agent"}])
        self.assertEqual(draft["cards_source"], "agent")
        self.assertEqual(len(draft["cards"]), len(bs.read_cards(FIXTURES / "cards.jsonl")))
        items = draft["methods"]["items"]
        self.assertEqual(len(items), 18)
        self.assertEqual([m["id"] for m in items if m["default"]], DEFAULT_METHODS)
        self.assertEqual(items[0]["i18n"]["zh"]["name"], "类比迁移")
        self.assertEqual(draft["models"], {"choices": [{"id": "sonnet", "label": "Sonnet"}, {"id": "opus", "label": "Opus"}],
                                           "generate": "sonnet", "group": "sonnet", "measured": "sonnet"})
        self.assertTrue(Path(result["page"]).is_file())

    def test_page_embeds_the_draft_without_breaking_out_of_the_script_tag(self):
        self.draft()
        page = (self.dir / "review" / "review.html").read_text(encoding="utf-8")
        self.assertNotIn("__DRAFT__", page)
        self.assertEqual(page.count("</script>"), 2)
        self.assertNotIn("maintainers?</script>", page)

    def test_the_four_default_methods_match_the_measured_texts(self):
        self.draft()
        texts = {m["id"]: m["text"] for m in self.draft_json()["methods"]["items"]}
        for key, text in bs.METHODS.items():
            self.assertEqual(texts[key], text)

    def test_cards_need_a_source(self):
        code, _, err = cli("review", "--dir", str(self.dir), "--message-file", str(self.root / "message.txt"),
                           "--question-file", str(self.root / "question.txt"), "--model-choice", "a=A",
                           "--model-default", "a", "--cards", str(FIXTURES / "cards.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("--cards-source", err)

    def test_model_default_must_be_a_choice(self):
        code, _, err = cli("review", "--dir", str(self.dir), "--message-file", str(self.root / "message.txt"),
                           "--question-file", str(self.root / "question.txt"), "--model-choice", "a=A", "--model-default", "b")
        self.assertEqual(code, 1)
        self.assertIn("model", err)

    def test_models_are_required(self):
        code, _, err = cli("review", "--dir", str(self.dir), "--message-file", str(self.root / "message.txt"),
                           "--question-file", str(self.root / "question.txt"))
        self.assertEqual(code, 1)
        self.assertIn("--model-choice", err)

    def test_unknown_language_falls_back_to_english(self):
        self.draft(lang="fr")
        self.assertEqual(self.draft_json()["lang"], "en")


class ConfirmTests(ReviewCase):
    def setUp(self):
        super().setUp()
        self.draft()

    def test_valid_answer_is_written(self):
        code, _, err = self.confirm(self.answer())
        self.assertEqual(code, 0, err)
        saved = json.loads((self.dir / "review" / "confirmed.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["models"], {"generate": "sonnet", "group": "opus"})

    def test_confirming_writes_a_read_only_page_of_the_setup(self):
        self.confirm(self.answer(question="Edited question", methods=["analogy", "reverse", "substitute"]))
        page = (self.dir / "review" / "confirmed.html").read_text(encoding="utf-8")
        data = json.loads(re.search(r'<script id="draft" type="application/json">(.*?)</script>', page, re.S).group(1))
        self.assertTrue(data["readonly"])
        self.assertEqual(data["question"], "Edited question")
        self.assertEqual(data["selected_methods"], ["analogy", "reverse", "substitute"])
        self.assertEqual(data["models"]["group"], "opus")
        self.assertEqual(data["constraints"], [{"text": "No telemetry", "source": "user"}])
        self.assertEqual(data["token"], "")

    def test_rejections(self):
        cases = {
            "methods": self.answer(methods=["analogy", "reverse"]),
            "unknown method": self.answer(methods=DEFAULT_METHODS + ["nope"]),
            "model": self.answer(models={"generate": "gpt", "group": "sonnet"}),
            "question": self.answer(question="   "),
            "card": self.answer(cards=[{"id": "bad id!", "text": "x"}]),
            "cards_source": self.answer(cards_source="user"),
            "profile": self.answer(profile="huge"),
            "ideas_per_agent": self.answer(ideas_per_agent=0),
            "constraint": self.answer(constraints=[{"text": "", "source": "user"}]),
        }
        for name, value in cases.items():
            code, _, err = self.confirm(value)
            self.assertEqual(code, 1, name)
            self.assertFalse((self.dir / "review" / "confirmed.json").exists(), name)

    def test_extra_keys_are_rejected(self):
        code, _, _ = self.confirm({**self.answer(), "seed": 3})
        self.assertEqual(code, 1)


class ServeTests(ReviewCase):
    def setUp(self):
        super().setUp()
        self.draft()
        self.server = review.make_server(self.dir, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_until_confirmed, kwargs={"timeout": 30}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        conn.close()
        return response.status, data

    def test_page_carries_a_fresh_token(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        token = re.search(r'"token": ?"([0-9a-f]{32,})"', page)
        self.assertIsNotNone(token)
        self.assertNotIn(token.group(1), (self.dir / "review" / "draft.json").read_text(encoding="utf-8"))

    def test_confirm_needs_the_token_and_a_local_origin(self):
        body = json.dumps(self.answer())
        status, _ = self.request("POST", "/confirm", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/confirm", body, {"Content-Type": "application/json",
                                                            "X-Review-Token": self.server.token,
                                                            "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse((self.dir / "review" / "confirmed.json").exists())

    def test_bad_answer_is_refused_and_good_one_ends_the_server(self):
        headers = {"Content-Type": "application/json", "X-Review-Token": self.server.token,
                   "Origin": f"http://127.0.0.1:{self.port}"}
        status, text = self.request("POST", "/confirm", json.dumps(self.answer(methods=["analogy"])), headers)
        self.assertEqual(status, 400)
        self.assertIn("method", text)
        status, _ = self.request("POST", "/confirm", json.dumps(self.answer()), headers)
        self.assertEqual(status, 200)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue((self.dir / "review" / "confirmed.json").is_file())


class StartMixin:
    def start(self, **change):
        self.draft()
        code, _, err = self.confirm(self.answer(**change))
        self.assertEqual(code, 0, err)
        code, out, err = cli("init", "--dir", str(self.dir), "--from-review", "--seed", "7")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def state(self):
        return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))


class FromReviewTests(StartMixin, ReviewCase):
    def test_state_takes_the_confirmed_values(self):
        self.start(methods=["analogy", "reverse", "substitute"], profile="economy")
        state = self.state()
        self.assertEqual(state["question"], "How can a small open-source project find maintainers?")
        self.assertEqual(state["constraints"], ["No telemetry"])
        self.assertEqual(set(state["methods"]), {"analogy", "reverse", "substitute"})
        self.assertEqual(state["methods"]["analogy"], bs.METHODS["analogy"])
        self.assertEqual(state["cards_source"], "agent")
        self.assertEqual(state["models"], {"generate": "sonnet", "group": "opus"})
        self.assertTrue(state["organize"])
        self.assertEqual(state["profile"], "economy")

    def test_init_without_a_confirmed_review_fails(self):
        self.draft()
        code, _, err = cli("init", "--dir", str(self.dir), "--from-review")
        self.assertEqual(code, 1)
        self.assertIn("confirm", err)

    def test_tasks_carry_the_chosen_model(self):
        self.start(profile="economy")
        host = FakeHost(self.dir)
        first = host.cli("next", "--dir", str(self.dir))
        self.assertTrue(all(task["model"] == "sonnet" for task in first["tasks"]))
        for task in first["tasks"]:
            host.generate(task)
        group = host.cli("next", "--dir", str(self.dir))
        self.assertEqual(group["purpose"], "group")
        self.assertTrue(all(task["model"] == "opus" for task in group["tasks"]))
        self.assertIn("model", group["instructions"])


class DirectInitTests(unittest.TestCase):
    def test_direct_cards_need_a_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = cli("init", "--dir", str(Path(tmp) / "run"), "--question", "q",
                               "--cards", str(FIXTURES / "cards.jsonl"))
            self.assertEqual(code, 1)
            self.assertIn("--cards-source", err)

    def test_direct_report_keeps_the_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            host = FakeHost(run)
            host.cli("init", "--dir", str(run), "--question", "Plain question?", "--profile", "economy", "--seed", "3")
            done = host.run()
            self.assertIn("> Plain question?", Path(done["report"]).read_text(encoding="utf-8"))
            self.assertNotIn("confirmed.html", Path(done["report"]).read_text(encoding="utf-8"))

    def test_direct_runs_have_no_model_and_no_organise_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            host = FakeHost(run)
            host.cli("init", "--dir", str(run), "--question", "q", "--profile", "economy", "--seed", "3")
            action = host.cli("next", "--dir", str(run))
            self.assertTrue(all("model" not in task for task in action["tasks"]))
            done = host.run()
            self.assertEqual(done["action"], "done")
            self.assertFalse(any(a.get("purpose") == "organize" for a in host.actions))


class OrganizeHost(FakeHost):
    """Also answers the organise task: themes by mechanism number parity, unless told otherwise."""

    def __init__(self, run_dir, organize=None, **kw):
        super().__init__(run_dir, **kw)
        self.organize_rule = organize
        self.organize_calls = 0

    def organize(self, task):
        self.organize_calls += 1
        prompt = Path(task["prompt"]).read_text(encoding="utf-8")
        target = output_path(prompt)
        rows = data_block(prompt, "Mechanisms:")
        rule = self.organize_rule(self.organize_calls, rows) if self.organize_rule else None
        if rule == "missing":
            return
        if isinstance(rule, str):
            target.write_text(rule, encoding="utf-8")
            return
        ids = [row["id"] for row in rows]
        value = rule or {"themes": [{"name": "Even", "mechanisms": ids[1::2]}, {"name": "Odd", "mechanisms": ids[0::2]}],
                         "titles": {key: f"Short {key}" for key in ids}}
        target.write_text(json.dumps(value), encoding="utf-8")

    def run(self, max_steps=200):
        extra = []
        for _ in range(max_steps):
            action = self.cli("next", "--dir", str(self.run_dir), *extra)
            extra = []
            self.actions.append(action)
            if action["action"] == "done":
                return action
            if action["action"] == "confirm_pressure":
                extra = ["--pressure", self.pressure]
                continue
            if action["action"] == "wait":
                extra = ["--give-up"]
                continue
            for task in action["tasks"]:
                {"group": self.group, "organize": self.organize}.get(action["purpose"], self.generate)(task)
        raise AssertionError("run did not finish")


class OrganizeTests(StartMixin, ReviewCase):
    def finished(self, lang="en", organize=None):
        self.start(profile="economy", lang=lang)
        host = OrganizeHost(self.dir, organize=organize)
        done = host.run()
        return host, done

    def report(self):
        return (self.dir / "report.md").read_text(encoding="utf-8")

    def test_organise_runs_once_at_the_end(self):
        host, done = self.finished()
        purposes = [a.get("purpose") for a in host.actions if a["action"] == "dispatch"]
        self.assertEqual(purposes[-1], "organize")
        self.assertEqual(purposes.count("organize"), 1)
        self.assertTrue(Path(done["report_html"]).is_file())
        organise = [a for a in host.actions if a.get("purpose") == "organize"][0]
        self.assertEqual(organise["tasks"][0]["model"], "opus")

    def test_report_follows_the_themes_and_reading_order(self):
        self.finished()
        text = self.report()
        state = self.state()
        beyond = [row for row in state["mechanisms"] if not row["in_baseline"]]
        self.assertIn("## Even", text)
        self.assertIn("## Odd", text)
        numbers = [int(n) for n in re.findall(r"^### (\d+)\. ", text, re.M)]
        self.assertEqual(numbers, list(range(1, len(state["mechanisms"]) + 1)))
        self.assertIn(f"### 1. Short ", text)
        self.assertLess(text.index("## Even"), text.index("## Odd"))
        self.assertTrue(beyond)

    def test_mechanisms_left_out_by_the_organiser_still_appear(self):
        def partial(call, rows):
            ids = [row["id"] for row in rows]
            return {"themes": [{"name": "Only one", "mechanisms": ids[-1:] + ["m-999"]}], "titles": {ids[-1]: "Short"}}
        self.finished(organize=partial)
        text = self.report()
        state = self.state()
        self.assertEqual(len(re.findall(r"^### \d+\. ", text, re.M)), len(state["mechanisms"]))
        self.assertIn("## Other", text)
        self.assertNotIn("m-999", text)

    def test_unreadable_answer_is_retried_once_then_the_report_is_flat(self):
        host, done = self.finished(organize=lambda call, rows: "not json")
        self.assertEqual(host.organize_calls, 2)
        state = self.state()
        self.assertIn("organize_unreadable", [row["reason"] for row in state["failures"]])
        self.assertEqual(len(re.findall(r"^### \d+\. ", self.report(), re.M)), len(state["mechanisms"]))

    def test_second_attempt_can_succeed(self):
        host, _ = self.finished(organize=lambda call, rows: "not json" if call == 1 else None)
        self.assertEqual(host.organize_calls, 2)
        self.assertIn("## Even", self.report())

    def test_reviewed_report_links_the_setup_instead_of_repeating_it(self):
        _, done = self.finished()
        text = self.report()
        self.assertIn("(review/confirmed.html)", text)
        self.assertNotIn("> How can a small open-source project", text)
        page = Path(done["report_html"]).read_text(encoding="utf-8")
        data = json.loads(re.search(r'<script id="report" type="application/json">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(data["setup"], "review/confirmed.html")

    def test_chinese_report(self):
        self.finished(lang="zh")
        text = self.report()
        self.assertTrue(text.startswith("# 头脑风暴结果"))
        self.assertIn("基线之外", text)
        self.assertNotIn("Beyond the baseline", text)

    def test_html_report_embeds_every_mechanism_and_the_themes(self):
        _, done = self.finished()
        page = Path(done["report_html"]).read_text(encoding="utf-8")
        data = json.loads(re.search(r'<script id="report" type="application/json">(.*?)</script>', page, re.S).group(1))
        state = self.state()
        self.assertEqual(len(data["mechanisms"]), len(state["mechanisms"]))
        self.assertEqual(sum(len(m["ideas"]) for m in data["mechanisms"]), len(state["ideas"]))
        self.assertEqual([t["name"] for t in data["themes"]], ["Even", "Odd"])
        self.assertEqual([m["n"] for m in data["mechanisms"]], list(range(1, len(state["mechanisms"]) + 1)))


if __name__ == "__main__":
    unittest.main()
