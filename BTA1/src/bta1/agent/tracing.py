"""OpenTelemetry tracing, exported to a local Phoenix instance.

Why this matters more than it looks: an agent is a loop whose intermediate steps
are invisible from the outside. Without tracing, a bad run is a wall of final
output and a guess. With it, you see every model call, every tool call, the
arguments, the results, and the token counts -- interleaved in the order they
happened. Most "the agent is broken" reports turn out, in the trace, to be a
system prompt problem.

DEGRADES GRACEFULLY. If Phoenix is not running, the agent still works. A
tracing backend is not a dependency of correctness, and a student who forgot
`make phoenix` should get a warning, not a stack trace.

Both Phoenix and Langfuse speak OTLP, so swapping backends is an exporter
change, not a rewrite. Observability is a protocol, not a vendor.
"""

from __future__ import annotations

import logging

from ..config import TracingConfig

log = logging.getLogger(__name__)


def setup(cfg: TracingConfig):
    """Configure OTel and instrument the OpenAI client. Returns a tracer or None."""
    if not cfg.enabled:
        log.info("tracing disabled in config")
        return None

    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import SpanLimits, TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        log.warning("tracing unavailable (%s); continuing without it", exc)
        return None

    # Check the collector is actually there BEFORE instrumenting.
    #
    # Graceful degradation was not enough: with nothing listening, the OTLP
    # exporter retries every batch and logs a WARNING each time, which buried
    # the real output of a demo run under hundreds of lines. A student reads
    # that as breakage. One line is honest; a wall of retries is not.
    if not _endpoint_reachable(cfg.endpoint):
        log.warning(
            "no trace collector at %s -- running without tracing. "
            "Start one with `make phoenix` if you want the waterfall view.",
            cfg.endpoint,
        )
        return None

    provider = TracerProvider(
        resource=Resource.create({"service.name": cfg.service_name}),
        # The default cap is 128 attributes per span, and the OpenAI
        # instrumentation sets one per message and per tool definition -- so a
        # long conversation with five tools blew past it and emitted
        # "Attributes dict is full. Dropping the oldest key-value pair" for
        # every span. The data was being silently truncated, too.
        span_limits=SpanLimits(max_attributes=512),
    )
    # A short export timeout matters for usability, not correctness: with no
    # collector listening (a student who forgot `make phoenix`), the default
    # retry schedule makes the agent hang for ~10s at shutdown and print a wall
    # of connection errors. Three seconds keeps the failure quiet and quick.
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=cfg.endpoint, timeout=3))
    )
    trace.set_tracer_provider(provider)

    # One line, and every chat.completions.create() becomes a span with its
    # prompt, response, model and token counts attached.
    OpenAIInstrumentor().instrument(tracer_provider=provider)

    log.info("tracing to %s", cfg.endpoint)
    return trace.get_tracer(__name__)


def _endpoint_reachable(endpoint: str, timeout_s: float = 0.5) -> bool:
    """Is anything listening on the collector's host:port?

    A plain TCP connect, not an HTTP request: we only need to know whether to
    instrument at all, and half a second is enough to decide.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def shutdown() -> None:
    """Flush pending spans. Without this a short run exits before exporting."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        log.debug("tracing shutdown: %s", exc)
