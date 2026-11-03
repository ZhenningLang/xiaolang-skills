"""Loopback-only page service. It exposes three routes and has no access to lineage."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs

from .budget import cli_boundary
from .sheet import AnswerError, SheetClosed, decode_payload, load_answers, load_public_sheet, merge_answers, public_state


HOST = "127.0.0.1"
PAGE = Path(__file__).resolve().parent / "web" / "index.html"
MAX_BODY = 1024 * 1024
REQUEST_TIMEOUT = 30
MAX_QUERY_FIELDS = 4
BAD_ADDRESS = "页面地址不对，请重新从命令行打开"
BAD_FORMAT = "提交的内容格式不对，请刷新页面再试"
NO_PAGE = "没有这个页面"
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT

    def version_string(self):
        return "local"

    def log_message(self, format, *args):
        """No access log: the request line carries the token."""

    def send_error(self, code, message=None, explain=None):
        # Malformed requests stay 400; everything else, including unknown verbs, is a plain refusal.
        self.reply(400 if code == 400 else 403, {"ok": False, "error": "请求不对，请重新从命令行打开页面"})

    def reply(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        for name, value in (("Content-Type", content_type), ("Content-Length", str(len(data))),
                            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                            ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", CSP)):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def authorize(self):
        """Host blocks DNS rebinding; the token is compared before the path is ever considered."""
        if self.headers.get("Host") != self.server.address:
            raise Refused(403, BAD_ADDRESS)
        path, _, query = self.path.partition("?")
        try:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS).get("t", [])
        except ValueError:
            values = []
        if len(values) != 1 or not hmac.compare_digest(values[0].encode(), self.server.token.encode()):
            raise Refused(403, BAD_ADDRESS)
        return path

    def read_body(self):
        if self.headers.get("Origin") != f"http://{self.server.address}":
            raise Refused(403, "请求来源不对，请重新从命令行打开页面")
        if self.headers.get_content_type() != "application/json":
            raise Refused(400, BAD_FORMAT)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise Refused(400, BAD_FORMAT) from None
        if self.headers.get("Transfer-Encoding") or length < 0 or length > MAX_BODY:
            raise Refused(400, "提交的内容太大了，请刷新页面再试")
        return self.rfile.read(length)

    def handle_route(self, action):
        try:
            action(self.authorize())
        except Refused as exc:
            self.reply(exc.code, {"ok": False, "error": str(exc)})
        except SheetClosed as exc:
            self.reply(403, {"ok": False, "error": str(exc)})
        except AnswerError as exc:
            self.reply(400, {"ok": False, "error": str(exc)})
        except (ValueError, OSError):
            # Fixed text: paths in the underlying error must not reach the page.
            self.reply(400, {"ok": False, "error": "答案文件读写失败，已填的内容还在，请重试"})

    def do_GET(self):
        def action(path):
            if path == "/":
                self.reply(200, self.server.page, "text/html; charset=utf-8")
            elif path == "/api/sheet":
                self.reply(200, public_state(self.server.run_dir, self.server.sheet_id))
            else:
                raise Refused(403, NO_PAGE)
        self.handle_route(action)

    def do_POST(self):
        def action(path):
            if path != "/api/answers":
                raise Refused(403, NO_PAGE)
            payload = decode_payload(self.read_body())
            self.reply(200, merge_answers(self.server.run_dir, self.server.sheet_id, payload))
        self.handle_route(action)


class LocalServer(ThreadingHTTPServer):
    # The default backlog of 5 resets simultaneous connections instead of queueing them.
    request_queue_size = 64


def make_server(run_dir, sheet_id):
    """Validate before binding: a tampered sheet or corrupted answers must never start."""
    sheet = load_public_sheet(run_dir, sheet_id)
    load_answers(run_dir, sheet)
    server = LocalServer((HOST, 0), Handler)
    server.run_dir, server.sheet_id = run_dir, sheet_id
    server.token = secrets.token_urlsafe(32)
    server.page = PAGE.read_bytes()
    server.address = f"{HOST}:{server.server_address[1]}"
    server.url = f"http://{server.address}/?t={server.token}"
    return server


def run_cli(args):
    with make_server(args.run_dir, args.sheet_id) as server:
        print(server.url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass  # Ctrl-C is how the author closes the page service; it is a normal exit.


def register(subparsers):
    parser = subparsers.add_parser("serve", help="Serve one sheet on loopback with a one-time token")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.set_defaults(func=lambda args: cli_boundary(run_cli, args))
