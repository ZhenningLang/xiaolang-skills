"""The review step before a run: a draft, a local page to edit and confirm it, and the checks on the answer.

Standard library only. Validation errors are ValueError, so the brainstorm CLI reports them the same way
whichever copy of the brainstorm module loaded this one.
"""

import hmac
import http.server
import json
from pathlib import Path
import re
import secrets
import sys
import time
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brainstorm as bs  # noqa: E402

HERE = Path(__file__).resolve().parent
PAGE = HERE / "review_page.html"
METHOD_I18N = HERE.parent / "methods" / "i18n"
LANGS = ("en", "zh")
RUN_PROFILES = ("full", "economy")
MAX_IDEAS_PER_AGENT = 30
MAX_BODY = 2_000_000
ANSWER_KEYS = {"question", "constraints", "cards", "cards_source", "methods", "profile", "ideas_per_agent", "models", "lang"}


def review_dir(run_dir):
    return Path(run_dir).resolve() / "review"


def method_i18n():
    """{method id: {lang: {"name": ..., "text": ...}}} from methods/i18n/<lang>.json."""
    out = {}
    for path in sorted(METHOD_I18N.glob("*.json")):
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            out.setdefault(key, {})[path.stem] = value
    return out


def library():
    """The bundled methods in file order; the four defaults are the measured texts."""
    return bs.read_methods(bs.METHOD_LIBRARY)


def page_html(draft, token="", confirm_url="/confirm"):
    data = dict(draft, token=token, confirm_url=confirm_url)
    # "<" is escaped so that no text in the draft can close the script element.
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return PAGE.read_text(encoding="utf-8").replace("__DRAFT__", blob)


def parse_choice(text):
    key, sep, label = text.partition("=")
    if not sep or not key.strip() or not label.strip():
        raise ValueError(f"--model-choice {text!r}: expected id=Label")
    return {"id": key.strip(), "label": label.strip()}


def build_draft(args):
    run_dir = Path(args.dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError(f"{run_dir} is not empty; use a new directory for each run")
    if not args.model_choice:
        raise ValueError("give the models this host can run with --model-choice id=Label (repeat), and --model-default")
    choices = [parse_choice(text) for text in args.model_choice]
    ids = [row["id"] for row in choices]
    if len(set(ids)) != len(ids):
        raise ValueError("--model-choice: repeated model id")
    if args.model_default not in ids:
        raise ValueError("--model-default must be one of the --model-choice ids")
    if args.measured_model is not None and args.measured_model not in ids:
        raise ValueError("--measured-model must be one of the --model-choice ids")
    if args.cards and args.cards_source is None:
        raise ValueError("--cards needs --cards-source user (the user supplied them) or agent (you wrote them)")
    question = Path(args.question_file).read_text(encoding="utf-8").strip()
    if not question:
        raise ValueError("the question draft is empty")
    message = Path(args.message_file).read_text(encoding="utf-8") if args.message_file else ""
    cards = bs.read_cards(args.cards) if args.cards else []
    names = method_i18n()
    items = [{"id": row["id"], "text": row["text"], "default": row["id"] in bs.METHODS,
              "i18n": names.get(row["id"], {})} for row in library()]
    constraints = ([{"text": text.strip(), "source": "user"} for text in args.constraint or [] if text.strip()]
                   + [{"text": text.strip(), "source": "agent"} for text in args.suggest_constraint or [] if text.strip()])
    return {"lang": args.lang if args.lang in LANGS else "en", "run": run_dir.name, "message": message.strip(),
            "question": question, "constraints": constraints, "cards": cards,
            "cards_source": args.cards_source if cards else None,
            "methods": {"min": bs.AGENTS, "items": items}, "profile": args.profile,
            "ideas_per_agent": args.ideas_per_agent,
            "models": {"choices": choices, "generate": args.model_default, "group": args.model_default,
                       "measured": args.measured_model}}


def write_draft(args):
    draft = build_draft(args)
    folder = review_dir(args.dir)
    bs.atomic_write(folder / "draft.json", json.dumps(draft, ensure_ascii=False, indent=1) + "\n")
    bs.atomic_write(folder / "review.html", page_html(draft))
    return {"draft": str(folder / "draft.json"), "page": str(folder / "review.html"),
            "next": "Run serve-review in the background, open the URL it prints for the user, and wait for it to exit."}


def load_draft(run_dir):
    path = review_dir(run_dir) / "draft.json"
    if not path.is_file():
        raise ValueError(f"no review draft at {path}; run review first")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_answer(draft, value):
    """The confirmed setup, checked against the draft it came from. Returns it unchanged or raises ValueError."""
    if not isinstance(value, dict) or set(value) != ANSWER_KEYS:
        raise ValueError("answer: expected exactly the keys " + ", ".join(sorted(ANSWER_KEYS)))
    if not isinstance(value["question"], str) or not value["question"].strip():
        raise ValueError("question: must not be empty")
    if not isinstance(value["constraints"], list):
        raise ValueError("constraints: expected a list")
    for row in value["constraints"]:
        if (not isinstance(row, dict) or set(row) != {"text", "source"} or not isinstance(row["text"], str)
                or not row["text"].strip() or row["source"] not in ("user", "agent")):
            raise ValueError("constraint: expected {text, source} with nonempty text and source user or agent")
    cards = value["cards"]
    if not isinstance(cards, list):
        raise ValueError("cards: expected a list")
    seen = set()
    for row in cards:
        if (not isinstance(row, dict) or set(row) != {"id", "text"} or not isinstance(row["id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", row["id"]) or row["id"] in seen
                or not isinstance(row["text"], str) or not row["text"].strip()):
            raise ValueError("card: expected {id, text} with a unique id of A-Z a-z 0-9 _ . - and nonempty text")
        seen.add(row["id"])
    if value["cards_source"] != draft["cards_source"] and cards:
        raise ValueError("cards_source: does not match the draft")
    known = [row["id"] for row in draft["methods"]["items"]]
    methods = value["methods"]
    if (not isinstance(methods, list) or len(set(methods)) != len(methods)
            or any(key not in known for key in methods)):
        raise ValueError("methods: expected distinct method ids from the draft")
    if len(methods) < bs.AGENTS:
        raise ValueError(f"methods: pick at least {bs.AGENTS}, one for each agent of a batch")
    if value["profile"] not in RUN_PROFILES:
        raise ValueError("profile: expected full or economy")
    ipa = value["ideas_per_agent"]
    if type(ipa) is not int or not 1 <= ipa <= MAX_IDEAS_PER_AGENT:
        raise ValueError(f"ideas_per_agent: expected a whole number from 1 to {MAX_IDEAS_PER_AGENT}")
    choices = {row["id"] for row in draft["models"]["choices"]}
    models = value["models"]
    if not isinstance(models, dict) or set(models) != {"generate", "group"} or not set(models.values()) <= choices:
        raise ValueError("models: expected generate and group, each one of the draft's model choices")
    if value["lang"] not in LANGS:
        raise ValueError("lang: unknown language")
    return value


def save_answer(run_dir, value):
    draft = load_draft(run_dir)
    validate_answer(draft, value)
    path = review_dir(run_dir) / "confirmed.json"
    bs.atomic_write(path, json.dumps(value, ensure_ascii=False, indent=1) + "\n")
    return path


def load_confirmed(run_dir):
    """Draft and confirmed answer, both checked; raises ValueError when the user has not confirmed."""
    draft = load_draft(run_dir)
    path = review_dir(run_dir) / "confirmed.json"
    if not path.is_file():
        raise ValueError("the review is not confirmed yet: run serve-review and wait for the user, or confirm-review")
    return draft, validate_answer(draft, json.loads(path.read_text(encoding="utf-8")))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, kind="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send(404, "not found")
            return
        self.send(200, page_html(self.server.draft, self.server.token), "text/html; charset=utf-8")

    def do_POST(self):
        server = self.server
        if self.path != "/confirm":
            self.send(404, "not found")
            return
        origin = self.headers.get("Origin")
        port = server.server_address[1]
        if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self.send(403, "origin not allowed")
            return
        if not hmac.compare_digest(self.headers.get("X-Review-Token", ""), server.token):
            self.send(403, "missing or wrong token")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self.send(400, "empty or oversized body")
            return
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            path = save_answer(server.run_dir, value)
        except (ValueError, UnicodeDecodeError) as exc:
            self.send(400, str(exc))
            return
        server.confirmed = str(path)
        self.send(200, json.dumps({"confirmed": str(path)}), "application/json")


class ReviewServer(http.server.HTTPServer):
    def __init__(self, run_dir, port):
        self.run_dir = Path(run_dir).resolve()
        self.draft = load_draft(run_dir)
        self.token = secrets.token_hex(16)
        self.confirmed = None
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def serve_until_confirmed(self, timeout):
        """Answer requests until the user confirms or the time runs out. Returns the confirmed path or None."""
        deadline = time.monotonic() + timeout
        while self.confirmed is None:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.timeout = min(left, 1.0)
            self.handle_request()
        return self.confirmed


def make_server(run_dir, port=0):
    return ReviewServer(run_dir, port)


def serve(args):
    server = make_server(args.dir, args.port)
    try:
        print(json.dumps({"url": server.url, "instructions": "Show the user this URL. This command exits when the "
                          "user confirms (exit 0) or after the timeout (exit 3)."}), flush=True)
        if args.open:
            webbrowser.open(server.url)
        confirmed = server.serve_until_confirmed(args.timeout)
    finally:
        server.server_close()
    if confirmed is None:
        print(json.dumps({"confirmed": None, "reason": "timeout"}), flush=True)
        return 3
    print(json.dumps({"confirmed": confirmed, "next": "run init --from-review"}), flush=True)
    return 0


def confirm_from_file(args):
    value = json.loads(Path(args.answer_file).read_text(encoding="utf-8"))
    return {"confirmed": str(save_answer(args.dir, value)), "next": "run init --from-review"}
