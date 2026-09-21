"""Explicit metadata-only stage boundaries; never capture arguments or return values."""

from contextlib import contextmanager

from opentelemetry.trace import INVALID_SPAN


@contextmanager
def stage(telemetry, name, tenant, task_id, **attributes):
    if telemetry is None or task_id is None:
        yield INVALID_SPAN
        return
    with telemetry.span(name, **{"tenant.id": tenant, "task.id": task_id, **attributes}) as span:
        try:
            yield span
        except Exception:
            span.set_attribute("stage.outcome", "error")
            raise
        else:
            span.set_attribute("stage.outcome", "completed")


def operation_attributes(service, operation):
    return {
        "operation.id": operation.id,
        "stage.tool": operation.tool,
        "stage.attempt": operation.attempts,
        "stage.status": operation.status,
        "stage.execution_mode": service.settings.execution_mode,
    }
