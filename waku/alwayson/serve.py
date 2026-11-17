"""`waku serve`: the handlers, the worker, the timer loop and the dispatcher
in one process, without the dashboard (spec 023).

    waku serve            run until Ctrl-C or SIGTERM
    waku serve --port N   the port for incoming webhooks (default 7780)

What runs:

  one worker     claims events and starts runs (worker.py)
  a dispatcher   sends each row of the outbox (dispatcher.py)
  a timer loop   every 30 seconds, writes each due timer into the inbox
                 (timers.py)
  the sources    each configured source inserts events (sources/)
  a listener     only when a source receives webhooks; on 127.0.0.1 unless
                 WAKU_SERVE_HOST says otherwise

A second `waku serve` on the same home refuses to start. `waku dashboard` is a
separate process and may run beside this one on the same state.db.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from waku.alwayson import outbox
from waku.alwayson.dispatcher import Dispatcher, Sender
from waku.alwayson.events import Event, Inbox
from waku.alwayson.timers import CHECK_SECONDS, TimerLoop
from waku.alwayson.worker import BATCH_WAIT_SECONDS, AgentRun, AlreadyRunning, ServeLock, Worker
from waku.config import Settings

DEFAULT_PORT = 7780
# A GitHub issue event is a few kilobytes. A larger body is not one of ours.
MAX_BODY_BYTES = 1_000_000
STOP_TIMEOUT_SECONDS = 60

# A source: given the inbox, start receiving and return something with stop(),
# or None when the source is not configured.
Source = Callable[[Inbox], Any]
# A route: given the request's headers and body, return (status, JSON bytes).
Route = Callable[[Mapping[str, str], bytes], tuple[int, bytes]]


def bind_host() -> str:
    """127.0.0.1 unless WAKU_SERVE_HOST is set. A webhook from the internet
    needs a public address; choosing one is said out loud."""
    host = os.getenv("WAKU_SERVE_HOST", "") or "127.0.0.1"
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"waku serve is listening on WAKU_SERVE_HOST={host}, which other machines can "
              "reach. Every route still checks its sender's signature.")
    return host


def _handler(routes: Mapping[str, Route]):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, json.dumps({"ok": True}).encode())
            else:
                self._reply(404, b"{}")

        def do_POST(self) -> None:
            route = routes.get(self.path.split("?", 1)[0])
            if route is None:
                self._reply(404, b"{}")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True   # the unread body is not a request
                self._reply(413, b"{}")
                return
            self._reply(*route(self.headers, self.rfile.read(length)))

        def log_message(self, format, *args) -> None:  # noqa: A002
            pass   # each route says what it did; the access log adds noise

    return Handler


class Service:
    """Everything `waku serve` runs, with one start and one stop."""

    def __init__(self, settings: Settings, *, sources: Mapping[str, Source] | None = None,
                 routes: Mapping[str, Route] | None = None,
                 senders: Mapping[str, Sender] | None = None, port: int = DEFAULT_PORT,
                 run: Callable[[str, list[Event]], str | None] | None = None,
                 batch_wait: float = BATCH_WAIT_SECONDS,
                 timer_interval: float = CHECK_SECONDS,
                 log: Callable[[str], None] = print) -> None:
        self.settings, self._log = settings, log
        self._timer_interval = timer_interval
        self._sources, self._routes, self._port = dict(sources or {}), dict(routes or {}), port
        self._senders = dict(senders or {})
        self.sending = list(self._senders)     # the channels a reply can go out on
        self._run, self._batch_wait = run, batch_wait
        self._lock = ServeLock(settings.home)
        self.inbox = Inbox(settings.home)
        self.running: list[str] = []          # the sources that started
        self._handles: list[Any] = []
        self._worker: Worker | None = None
        self._ready = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self._dispatcher: Dispatcher | None = None
        self._dispatching = threading.Event()
        self.dispatcher_thread: threading.Thread | None = None
        self._timers: TimerLoop | None = None
        self._timing = threading.Event()
        self.timer_thread: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        """Where the listener is bound, or None when nothing listens."""
        return self._server.server_address[:2] if self._server else None

    def _work(self) -> None:
        # The connection and the agent are made here, on the worker's thread:
        # SQLite keeps a connection on the thread that opened it.
        from waku.db import connect

        conn = connect(self.settings.home)
        run = self._run or _agent_run(self.settings, self._log)
        self._worker = Worker(conn, run, batch_wait=self._batch_wait, log=self._log)
        self._ready.set()
        try:
            self._worker.run_forever()
        finally:
            if hasattr(run, "close"):
                run.close()
            conn.close()

    def _dispatch(self) -> None:
        from waku.db import connect

        conn = connect(self.settings.home)
        self._dispatcher = Dispatcher(conn, self._senders, owner=outbox.owner_addresses(),
                                      log=self._log)
        self._dispatching.set()
        try:
            self._dispatcher.run_forever()
        finally:
            conn.close()

    def _time(self) -> None:
        from waku.db import connect

        conn = connect(self.settings.home)
        self._timers = TimerLoop(conn, log=self._log)
        self._timing.set()
        try:
            self._timers.run_forever(self._timer_interval)
        finally:
            conn.close()

    def start(self) -> None:
        self._lock.acquire()   # raises AlreadyRunning
        try:
            self.worker_thread = threading.Thread(target=self._work, name="serve-worker",
                                                  daemon=True)
            self.worker_thread.start()
            self._ready.wait(10)
            self.dispatcher_thread = threading.Thread(target=self._dispatch,
                                                      name="serve-dispatcher", daemon=True)
            self.dispatcher_thread.start()
            self._dispatching.wait(10)
            self.timer_thread = threading.Thread(target=self._time, name="serve-timers",
                                                 daemon=True)
            self.timer_thread.start()
            self._timing.wait(10)
            if self._routes:
                self._server = ThreadingHTTPServer((bind_host(), self._port),
                                                   _handler(self._routes))
                threading.Thread(target=self._server.serve_forever, name="serve-http",
                                 daemon=True).start()
            for name, source in self._sources.items():
                try:
                    handle = source(self.inbox)
                except Exception as exc:
                    from waku.gateway.runner import safe_exception

                    self._log(f"(serve) {name} did not start: {safe_exception(exc)}")
                    continue
                if handle is not None:
                    self._handles.append(handle)
                    self.running.append(name)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """Stop receiving first, let the run in flight finish, then stop
        sending. A reply the dispatcher did not reach stays pending on disk,
        and so does a timer that came due after the loop stopped."""
        for handle in self._handles:
            try:
                handle.stop()
            except Exception as exc:
                self._log(f"(serve) a source did not stop cleanly: {type(exc).__name__}")
        self._handles, self.running = [], []
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._timers is not None:
            self._timers.stop.set()
        if self.timer_thread is not None and self.timer_thread.is_alive():
            self.timer_thread.join(STOP_TIMEOUT_SECONDS)
        if self._worker is not None:
            self._worker.stop.set()
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.worker_thread.join(STOP_TIMEOUT_SECONDS)
        if self._dispatcher is not None:
            self._dispatcher.stop.set()
        if self.dispatcher_thread is not None and self.dispatcher_thread.is_alive():
            self.dispatcher_thread.join(STOP_TIMEOUT_SECONDS)
        self._lock.release()


def _agent_run(settings: Settings, log: Callable[[str], None]) -> AgentRun:
    from waku.app import Waku
    from waku.gateway.cli import _observer  # mirror gate and tool activity here

    return AgentRun(lambda: Waku(settings=settings), observer=_observer, log=log)


def configured(settings: Settings) -> tuple[dict[str, Source], dict[str, Route]]:
    """The sources and webhook routes this machine's settings turn on. Each
    source's start returns None when its own settings are missing."""
    from waku.alwayson.sources import github, slack, telegram

    sources: dict[str, Source] = {"telegram": telegram.start, "slack": slack.start,
                                  "github": github.start}
    routes: dict[str, Route] = {}
    if webhook := github.route(Inbox(settings.home)):
        routes["/webhooks/github"] = webhook
    return sources, routes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="waku serve", description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("WAKU_SERVE_PORT", "") or DEFAULT_PORT))
    args = parser.parse_args(argv)

    from waku.config import load_settings

    settings = load_settings()
    settings.ensure_home()
    sources, routes = configured(settings)
    from waku.alwayson import senders

    service = Service(settings, sources=sources, routes=routes,
                      senders=senders.configured(settings), port=args.port)

    # Set before start, so a signal that arrives during startup still stops it.
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    try:
        service.start()
    except AlreadyRunning as exc:
        print(exc)
        return 1
    try:
        print(f"waku serve is running on {settings.home}. Ctrl-C to stop.")
        print("  sources: " + (", ".join(service.running) or
                               "none configured (docs/integrations.md lists them)"))
        if service.address:
            print("  webhooks: " + ", ".join(
                "http://{}:{}{}".format(*service.address, path) for path in routes))
        print("  replies go out on: " + ", ".join(sorted(service.sending)))
        while not stop.wait(1):
            pass
    finally:
        service.stop()
    print("waku serve stopped.")
    return 0
