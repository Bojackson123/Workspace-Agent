"""Cloud Trace setup via OpenTelemetry.

ADK uses OpenTelemetry internally; setting a global TracerProvider before
the runner starts causes ADK's agent and tool spans to export to Cloud
Trace automatically alongside the spans we create manually.

The key span is the one in ``chat._run_agent`` — it wraps the full pipeline
run so Cloud Trace shows end-to-end latency per workflow invocation. ADK's
sub-agent and tool-call spans nest under it.

Log ↔ trace correlation: ``logging_setup._CloudRunFormatter`` reads the
active span context via the OTel API and writes the
``logging.googleapis.com/trace`` and ``spanId`` fields into each JSON log
line. Cloud Logging picks these up and adds a "View trace" link inline on
every log entry that was emitted inside a span.

This module is a no-op when ``GOOGLE_CLOUD_PROJECT`` is not set so local
development needs no extra configuration.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def configure_tracing() -> None:
    """Configure the global OTel tracer to export to Cloud Trace.

    Uses ``BatchSpanProcessor`` — appropriate here because the Cloud Run
    service is deployed with ``--no-cpu-throttling``, so the background
    flush thread always has CPU available.

    Safe to call multiple times; subsequent calls after the provider is
    already set are silently ignored by the OTel SDK.
    """
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        log.info("GOOGLE_CLOUD_PROJECT not set — Cloud Trace disabled (local mode)")
        return

    from opentelemetry import trace
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(CloudTraceSpanExporter(project_id=project))
    )
    trace.set_tracer_provider(provider)
    log.info("Cloud Trace enabled", extra={"json_fields": {"project": project}})
