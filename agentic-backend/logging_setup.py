"""Structured JSON logging for Cloud Run / Cloud Logging.

Cloud Run forwards container stdout to Cloud Logging. When each line is
valid JSON containing a top-level "severity" field, Cloud Logging promotes
it to a structured log entry: the severity is indexed, every other field
becomes a searchable jsonPayload attribute, and the entry is queryable
with log filters like:

    jsonPayload.tool="create_gmail_draft"
    jsonPayload.workflow="/meeting"
    jsonPayload.session_id="abc123def456"

Log ↔ trace correlation: when an OTel span is active (set up by
``tracing_setup.configure_tracing``), the formatter automatically adds
``logging.googleapis.com/trace`` and ``logging.googleapis.com/spanId``
to each log line. Cloud Logging uses these to render a "View trace" link
inline on every log entry, and Cloud Trace shows the log entries alongside
the flame chart.

Usage in other modules::

    log = logging.getLogger(__name__)
    log.info("adk.tool_call", extra={"json_fields": {"tool": "...", "args": {...}}})

Call ``configure_logging()`` once at process start (in main.py) before
any module-level log statements fire.
"""

from __future__ import annotations

import datetime
import json
import logging
import os


_LEVEL_TO_SEVERITY: dict[int, str] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

# Third-party loggers that are too chatty to leave at INFO.
_QUIET_LOGGERS: list[str] = [
    "uvicorn.access",
    "uvicorn.error",
    "google.auth",
    "google.auth.transport",
    "httpx",
    "httpcore",
]


class _CloudRunFormatter(logging.Formatter):
    """Emit one newline-delimited JSON object per log record.

    Cloud Logging parses the top-level "severity" and "message" fields;
    everything else lands in jsonPayload and is fully queryable.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "severity": _LEVEL_TO_SEVERITY.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.timezone.utc
            ).isoformat(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "json_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)

        # Attach Cloud Trace context so Cloud Logging can link this line to
        # its trace. The OTel API is always importable (it's a dependency);
        # if no tracer provider is configured it returns a no-op span whose
        # context is not valid, so the block below is a clean no-op locally.
        try:
            from opentelemetry import trace as otel_trace
            ctx = otel_trace.get_current_span().get_span_context()
            if ctx.is_valid:
                project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
                payload["logging.googleapis.com/trace"] = (
                    f"projects/{project}/traces/{ctx.trace_id:032x}"
                )
                payload["logging.googleapis.com/spanId"] = f"{ctx.span_id:016x}"
                payload["logging.googleapis.com/trace_sampled"] = bool(
                    ctx.trace_flags.sampled
                )
        except Exception:  # noqa: BLE001 — never let tracing break logging
            pass

        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Replace the root logger's handlers with the Cloud Run JSON formatter.

    Call this once, early in ``main.py``, before importing any application
    module that calls ``logging.getLogger`` at module level.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(_CloudRunFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
