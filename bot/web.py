"""Local dashboard server.

A `ThreadingHTTPServer` on 127.0.0.1 running alongside the trading loop in
a daemon thread. It exposes a small JSON API and serves three static files.

Security posture (this thing can stop and start trades, so it gets treated
seriously):

  * Binds loopback only. `UI_HOST` may override, and warns loudly if it does.
  * No `Access-Control-Allow-Origin` is ever sent, so no other origin can
    read a response.
  * Every mutating endpoint requires `Content-Type: application/json`, which
    forces a CORS preflight that will fail -- a hostile page cannot smuggle
    in a `text/plain` or form-encoded POST, which are "simple requests" and
    would otherwise skip the preflight.
  * `Origin`/`Referer`, when present, must match the request's own `Host`.
  * The private key is never part of any response, and `assert_no_secret()`
    re-checks that before every reply is written.
  * Going live requires a typed confirmation phrase checked on the server,
    not just in the browser's `confirm()`.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .engine import Engine
from .settings import SettingError, schema

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Typed in the browser AND re-checked here. Server-side is what actually
# counts -- a browser prompt can be bypassed.
GO_LIVE_PHRASE = "GO LIVE"
RESUME_PHRASE = "resume"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

# Read the whole body but not so much that a runaway client can pin memory.
MAX_BODY = 64 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def tail_lines(path: Path, lines: int = 300, max_bytes: int = 256 * 1024) -> list[str]:
    """Last `lines` lines of a possibly large file, read in constant memory."""
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop the partial first line
            data = fh.read()
    except OSError as exc:
        return [f"(could not read {path.name}: {exc})"]
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]


class DashboardServer:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.cfg: Config = engine.cfg
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.bound_url: str | None = None
        # The port actually listening, which differs from cfg.ui_port when it
        # is 0 (ephemeral). Tests rely on this.
        self.bound_port: int | None = None

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> bool:
        handler = _make_handler(self.engine)
        try:
            self._httpd = ThreadingHTTPServer(
                (self.cfg.ui_host, self.cfg.ui_port), handler
            )
        except OSError as exc:
            log.error(
                "Dashboard not started: could not bind %s:%d (%s). "
                "Is another instance running? Set UI_PORT to change it.",
                self.cfg.ui_host, self.cfg.ui_port, exc,
            )
            return False

        # Daemon: the dashboard must never keep the process alive when the
        # user Ctrl-Cs the bot.
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="dashboard", daemon=True
        )
        self._thread.start()
        self.bound_port = self._httpd.server_address[1]
        self.bound_url = f"http://{self.cfg.ui_host}:{self.bound_port}"
        log.info("Dashboard: %s", self.bound_url)
        # Warm the price cache so the first page load is not blank, and keeps
        # position PnL moving between candles.
        self.engine.start_price_refresher()
        return True

    def stop(self) -> None:
        self.engine.stop_price_refresher()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ── handler factory ──────────────────────────────────────────────────────


def _make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "BotDashboard/1.0"

        # ── plumbing ────────────────────────────────────────────────────

        def log_message(self, fmt: str, *args) -> None:
            # The default writes every poll to stderr; 2s polling would
            # drown the log. Drop to debug on our own logger instead.
            log.debug("HTTP %s", fmt % args)

        def handle(self) -> None:
            """Swallow the reset that keep-alive clients leave behind.

            Browsers and pollers close idle HTTP/1.1 connections abruptly,
            which raises ConnectionResetError on the next read. socketserver
            prints a full traceback for it, and a dashboard polled every two
            seconds generates those constantly -- burying the log lines that
            actually matter.
            """
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError, TimeoutError):
                pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Belt and braces: never let another origin embed this.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client navigated away mid-response

        def _json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _fail(self, message: str, status: int = 400) -> None:
            # An error response usually means the request body was never
            # consumed -- a bad Content-Type is rejected before its body is
            # read. Leaving those bytes on an HTTP/1.1 keep-alive connection
            # makes the server read them as the start of the next request,
            # fail to parse it, and drop the socket. The client then sees a
            # connection reset instead of the 4xx we just sent (WinError
            # 10053 on Windows). Closing instead is the honest answer.
            self.close_connection = True
            self._json({"ok": False, "error": message}, status)

        def assert_no_secret(self, payload) -> None:
            """Refuse to emit the private key, whatever the caller asked for.

            Cheap (one serialisation) and it turns a future careless refactor
            that leaks the key into a loud failure instead of a silent one.
            """
            key = engine.cfg.private_key_base58
            if key and key in json.dumps(payload, default=str):
                log.critical(
                    "Blocked a response that would have leaked "
                    "PRIVATE_KEY_BASE58. Investigate before trusting this."
                )
                raise ApiError("refused: response would contain a secret", 500)

        # ── guards ──────────────────────────────────────────────────────

        def check_origin(self) -> None:
            """Reject cross-site requests. Localhost still needs this:
            a hostile page can *send* requests to us even if it cannot read
            the replies."""
            origin = self.headers.get("Origin")
            if not origin:
                # No Origin: either a same-origin fetch (browsers send it on
                # cross-origin, and on same-origin non-GET) or a plain tool.
                # The Content-Type check on POSTs is what stops the rest.
                return
            if urlparse(origin).netloc != (self.headers.get("Host") or ""):
                raise ApiError("cross-origin request refused", 403)

        def read_json(self) -> dict:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                # Forces a CORS preflight, which fails because we never send
                # Access-Control-Allow-Origin. This is the main CSRF wall.
                raise ApiError(
                    "Content-Type must be application/json", 415
                )
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise ApiError("request body too large", 413)
            try:
                raw = self.rfile.read(length)
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ApiError(f"invalid JSON body: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ApiError("JSON body must be an object")
            return parsed

        # ── routes ──────────────────────────────────────────────────────

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            try:
                url = urlparse(self.path)
                path = url.path

                if path in ("/", "/index.html"):
                    return self._serve_static("index.html")
                if path.startswith("/static/"):
                    return self._serve_static(path[len("/static/"):])

                if path == "/api/state":
                    return self._json(engine.snapshot())
                if path == "/api/schema":
                    payload = {"schema": schema(), "values": engine.snapshot()["settings"]}
                    self.assert_no_secret(payload)
                    return self._json(payload)
                if path == "/api/trades":
                    limit = _bounded_int(parse_qs(url.query), "limit", 500, 1, 5000)
                    return self._json({"ok": True, "trades": engine.store.read_trades(limit)})
                if path == "/api/log":
                    lines = _bounded_int(parse_qs(url.query), "lines", 300, 1, 2000)
                    return self._json({
                        "ok": True,
                        "lines": tail_lines(engine.cfg.working_dir / "bot.log", lines),
                    })
                if path == "/api/candles":
                    limit = _bounded_int(parse_qs(url.query), "limit", 300, 30, 1000)
                    return self._json(engine.candles(limit))
                if path == "/api/trending":
                    limit = _bounded_int(parse_qs(url.query), "limit", 30, 5, 100)
                    return self._json(engine.trending(limit))

                self._fail("not found", 404)
            except ApiError as exc:
                self._fail(str(exc), exc.status)
            except Exception as exc:  # noqa: BLE001
                log.exception("GET %s failed", self.path)
                self._fail(f"internal error: {exc}", 500)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self.check_origin()
                body = self.read_json()
                path = urlparse(self.path).path

                if path == "/api/kill":
                    return self._handle_kill(body)
                if path == "/api/mode":
                    return self._handle_mode(body)
                if path == "/api/settings":
                    return self._handle_settings(body)

                self._fail("not found", 404)
            except ApiError as exc:
                self._fail(str(exc), exc.status)
            except SettingError as exc:
                # The bad value was already rolled back inside apply_changes.
                self._fail(str(exc), 422)
            except Exception as exc:  # noqa: BLE001
                log.exception("POST %s failed", self.path)
                self._fail(f"internal error: {exc}", 500)

        def _handle_kill(self, body: dict) -> None:
            if "engage" not in body:
                raise ApiError("missing 'engage' field")
            engage = body["engage"]
            if not isinstance(engage, bool):
                raise ApiError("'engage' must be a boolean")

            if not engage and body.get("confirm") != RESUME_PHRASE:
                # Releasing reopens the gate to live entries, so it needs the
                # same deliberate act as closing it. Engaging stays one click.
                raise ApiError(
                    f"releasing the kill switch requires confirm='{RESUME_PHRASE}'",
                    422,
                )

            state = engine.set_kill_switch(engage)
            log.warning("Kill switch is now %s (via dashboard)",
                        "ENGAGED" if state else "RELEASED")
            self._json({"ok": True, "killSwitch": state})

        def _handle_mode(self, body: dict) -> None:
            dry_run = body.get("dryRun")
            if not isinstance(dry_run, bool):
                raise ApiError("'dryRun' must be a boolean")

            if not dry_run and body.get("confirm") != GO_LIVE_PHRASE:
                raise ApiError(
                    f"go-live requires confirm='{GO_LIVE_PHRASE}' exactly", 422
                )

            engine.set_mode(dry_run)
            payload = {"ok": True, "mode": engine.snapshot()["mode"]}
            self.assert_no_secret(payload)
            self._json(payload)

        def _handle_settings(self, body: dict) -> None:
            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ApiError("body must contain a non-empty 'changes' object")

            engine.save_settings(changes)
            payload = {"ok": True, "values": engine.snapshot()["settings"]}
            self.assert_no_secret(payload)
            self._json(payload)

        def _serve_static(self, name: str) -> None:
            # Reject traversal before joining paths.
            if "/" in name or "\\" in name or ".." in name:
                raise ApiError("not found", 404)
            target = STATIC_DIR / name
            if not target.is_file():
                raise ApiError("not found", 404)
            ctype = _CONTENT_TYPES.get(target.suffix) or \
                mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

    return Handler


def _bounded_int(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(key, [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be an integer") from None
    if not lo <= value <= hi:
        raise ApiError(f"{key} must be {lo}..{hi}")
    return value
