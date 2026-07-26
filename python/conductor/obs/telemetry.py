"""OpenTelemetry setup + Conductor's domain metrics.

Traces and metrics are two independent SDK pipelines. Traces go to Jaeger (OTLP/gRPC,
on by default in Jaeger v2); metrics go to the OTel collector, which Prometheus scrapes.
Everything degrades to a safe no-op when OTEL_ENABLED=false or a backend is unreachable,
so a run never fails because telemetry is down (FR-8).

Prometheus name-mangling (dots->_, unit suffix, histogram _bucket/_sum/_count):
  conductor.tool.latency   (s) -> conductor_tool_latency_seconds_*
  conductor.llm.latency    (s) -> conductor_llm_latency_seconds_*
  conductor.llm.tokens         -> conductor_llm_tokens_total
  conductor.llm.cost_usd       -> conductor_llm_cost_usd_total
  conductor.submissions        -> conductor_submissions_total
  conductor.runs               -> conductor_runs_total

Every instrument here has a caller; a metric nothing records is worse than no metric, because
a dashboard panel over it reads as "healthy and idle". Pending-review depth deliberately isn't
a metric: it lives in Temporal (GET /pending lists + queries it), and a per-process up/down
counter would drift negative the first time a worker died while a run was parked.
"""

from __future__ import annotations

import logging

from ..settings import get_settings

log = logging.getLogger(__name__)

_LAT_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10]
_LLM_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60]


class Telemetry:
    """Holds metric instruments; every record method is a no-op until init() succeeds."""

    def __init__(self) -> None:
        self.enabled = False
        self.tool_latency = None
        self.llm_latency = None
        self.tokens = None
        self.cost = None
        self.submissions = None
        self.runs = None

    def init(self, service_name: str | None = None) -> None:
        s = get_settings()
        if not s.otel_enabled:
            log.info("OTel disabled (OTEL_ENABLED=false); metrics are no-ops")
            return
        try:
            self._init(service_name or s.otel_service_name, s.otel_traces_endpoint, s.otel_exporter_otlp_endpoint)
            self.enabled = True
            log.info("OTel initialized -> traces %s / metrics %s", s.otel_traces_endpoint,
                     s.otel_exporter_otlp_endpoint)
        except Exception as exc:  # never let telemetry break the service
            log.warning("OTel init failed (%s); continuing without telemetry", exc)

    def _init(self, service_name: str, traces_endpoint: str, metrics_endpoint: str) -> None:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": service_name})

        tp = TracerProvider(resource=resource)
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint, insecure=True)))
        trace.set_tracer_provider(tp)

        def view(name: str, buckets: list) -> View:
            return View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(buckets))

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint, insecure=True), export_interval_millis=10_000
        )
        mp = MeterProvider(
            resource=resource, metric_readers=[reader],
            views=[
                view("conductor.tool.latency", _LAT_BUCKETS),
                view("conductor.llm.latency", _LLM_BUCKETS),
            ],
        )
        metrics.set_meter_provider(mp)
        m = mp.get_meter("conductor")

        self.tool_latency = m.create_histogram("conductor.tool.latency", unit="s")
        self.llm_latency = m.create_histogram("conductor.llm.latency", unit="s")
        self.tokens = m.create_counter("conductor.llm.tokens")
        self.cost = m.create_counter("conductor.llm.cost_usd")
        self.submissions = m.create_counter("conductor.submissions")
        self.runs = m.create_counter("conductor.runs")

    # ---- record helpers (all safe when disabled) ----
    def record_tool(self, tool: str, seconds: float) -> None:
        if self.tool_latency:
            self.tool_latency.record(seconds, {"tool": tool})

    def record_llm(self, seconds: float, tokens_in: int, tokens_out: int, cost_usd: float, model: str) -> None:
        attrs = {"model": model}
        if self.llm_latency:
            self.llm_latency.record(seconds, attrs)
        if self.tokens:
            self.tokens.add(tokens_in, {**attrs, "direction": "in"})
            self.tokens.add(tokens_out, {**attrs, "direction": "out"})
        if self.cost and cost_usd:
            self.cost.add(cost_usd, attrs)

    def count_submission(self) -> None:
        """Accepted at the edge. Separate instrument from `count_run` on purpose: mixing an
        arrival count into `conductor.runs` would make "runs by outcome" count every run twice
        (once on submit, once on its terminal outcome) and its sum meaningless."""
        if self.submissions:
            self.submissions.add(1)

    def count_run(self, outcome: str) -> None:
        """One increment per run, at its TERMINAL outcome only (see `count_submission`)."""
        if self.runs:
            self.runs.add(1, {"outcome": outcome})


T = Telemetry()


def instrument_fastapi(app) -> None:
    """Auto-emit request spans for the control API. MUST run at import time: Starlette
    caches the middleware stack on the app's first call (the lifespan scope), so
    instrumenting inside lifespan is a silent no-op. Gate on settings, not T.enabled."""
    if not get_settings().otel_enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    except Exception as exc:  # noqa: BLE001
        log.warning("FastAPI instrumentation failed: %s", exc)
