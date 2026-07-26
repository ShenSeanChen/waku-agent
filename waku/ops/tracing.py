"""Trace — one trace per run (the LLM-Ops box, first step).

Two outputs from the same events:

1. JSONL, always on: every turn appends readable lines to
   .waku/traces/<date>.jsonl. A trace is just "what happened, in order" —
   open the file and read your agent's mind. Zero dependencies.

2. OpenTelemetry spans, when OTEL_EXPORTER_OTLP_ENDPOINT is set: the same
   events as a span tree any OTel backend can render. For a local dashboard:

       pip install 'waku-agent[tracing]'
       phoenix serve                                # localhost:6006
       OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python -m waku

   Langfuse cloud speaks OTel too — point the endpoint + auth headers there
   instead. The instrumentation below doesn't know or care which.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from waku.config import Settings


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class TraceEncodingError(UnicodeError):
    """A legacy trace cannot be safely read or appended as UTF-8."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            f"Trace file is not valid UTF-8: {path}. It may have been written by an older "
            "Waku version using the Windows system encoding. Move it out of the traces directory "
            "and keep it as a backup, then restart Waku if it is today's trace. The file "
            "was not modified."
        )


def iter_trace_lines(path: Path) -> Iterator[str]:
    """Yield one UTF-8 trace line at a time with a useful legacy-file error."""
    try:
        with path.open("r", encoding="utf-8") as trace:
            yield from trace
    except UnicodeDecodeError as exc:
        raise TraceEncodingError(path) from exc


class Tracer:
    """Doubles as a loop Observer: pass `tracer.event` anywhere an observer
    goes and every loop step lands in the trace."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.home / "traces" / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        self._otel_tracer = self._init_otel(settings)
        self._span_ctx = None
        self._trace_encoding_checked = False

    def _init_otel(self, settings: Settings):
        if not settings.otel_endpoint:
            return None
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": "waku-agent"}))
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True))
            )
            trace.set_tracer_provider(provider)
            self._otel_provider = provider
            return trace.get_tracer("waku")
        except ImportError:
            print("(tracing) OTEL endpoint set but opentelemetry not installed — "
                  "pip install 'waku-agent[tracing]'. JSONL tracing still on.")
            return None

    def _write(self, record: dict) -> None:
        # An older Windows release may have created this daily file in GBK.
        # Refuse to make a mixed-encoding JSONL file: validate once, explain how
        # to preserve the old file, and never guess or rewrite user data.
        if not self._trace_encoding_checked:
            if self.path.exists():
                for _ in iter_trace_lines(self.path):
                    pass
            self._trace_encoding_checked = True
        record["ts"] = _now()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _record_usage(self, event: dict) -> None:
        """Append one LLM call's token usage to a PERMANENT ledger (usage.jsonl).
        Unlike traces (which can be reset for a clean demo), this is the running
        record of what you've actually spent — never wiped, summarized per day on
        the dashboard. Tokens are the ground truth; dollar cost is derived from
        them (pricing can change), so we store tokens + provider/model."""
        usage = event.get("usage", {})
        record = {"ts": _now(), "provider": self.settings.provider,
                  "model": self.settings.model or "", "kind": "loop",
                  "in": usage.get("in", 0), "out": usage.get("out", 0)}
        with (self.settings.home / "usage.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    # ---- the Observer: called by the loop for every llm/tool/gate/... event
    def event(self, kind: str, event: dict) -> None:
        if kind == "text":
            return  # streaming token deltas are for the live UI, not the trace
        if kind == "llm":
            self._record_usage(event)
            # stamp WHICH brain answered — in a multi-model world (shootouts,
            # live model switching) a trace without the model is half a trace
            event = {"provider": self.settings.provider,
                     "model": self.settings.model or "", **event}
        self._write({"type": kind, **event})
        if self._otel_tracer and self._span_ctx is not None:
            with self._otel_tracer.start_as_current_span(
                f"{kind}.{event.get('tool', event.get('decision', ''))}".rstrip("."),
                attributes={
                    "openinference.span.kind": {"llm": "LLM", "tool": "TOOL"}.get(kind, "CHAIN"),
                    **{f"waku.{k}": json.dumps(v, default=str) for k, v in event.items()},
                },
            ):
                pass

    # ---- one run = one root span + turn_start/turn_end JSONL markers
    @contextmanager
    def turn(self, user_message: str):
        self._write({"type": "turn_start", "user_message": user_message})
        if self._otel_tracer:
            with self._otel_tracer.start_as_current_span(
                "agent_run",
                attributes={"openinference.span.kind": "AGENT", "waku.user_message": user_message},
            ) as span:
                self._span_ctx = span
                try:
                    yield self
                finally:
                    self._span_ctx = None
        else:
            yield self

    def end_turn(self, reply: str, iterations: int) -> None:
        self._write({"type": "turn_end", "reply": reply, "iterations": iterations})
        if getattr(self, "_otel_provider", None):
            # flush per turn: the trace should survive even a killed process
            self._otel_provider.force_flush(timeout_millis=2000)


def compose(*observers) -> callable:
    """Fan one loop event out to several observers (gateway display + tracer)."""
    active = [o for o in observers if o]
    def fanout(kind: str, event: dict) -> None:
        for obs in active:
            obs(kind, event)
    return fanout
