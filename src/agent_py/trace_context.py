"""Server-originated links only. Never ingest caller baggage or mutate business versions."""

import re

from opentelemetry import trace
from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState
from sqlalchemy import select

from agent_py.db import Task, TaskTrace, tenant_get

PATTERN = re.compile(r"00-([a-f0-9]{32})-([a-f0-9]{16})-(00|01)")
ROLES = frozenset({"origin", "dispatch", "previous"})


def enabled(service, tenant):
    return service.settings.langfuse_enabled and service.settings.langfuse_tenant == tenant


def pack(context=None):
    context = context or trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags) & 1:02x}"


def unpack(value):
    match = PATTERN.fullmatch(value) if isinstance(value, str) else None
    if not match or not int(match[1], 16) or not int(match[2], 16):
        return None
    return SpanContext(
        int(match[1], 16),
        int(match[2], 16),
        is_remote=True,
        trace_flags=TraceFlags(int(match[3], 16)),
        trace_state=TraceState(),
    )


def link(value, role):
    context = unpack(value)
    return Link(context, {"agent.link": role}) if context and role in ROLES else None


def row_for(s, tenant, task_id):
    return s.scalar(
        select(TaskTrace).where(TaskTrace.tenant_id == tenant, TaskTrace.task_id == task_id)
    )


def record_origin(service, s, task):
    if enabled(service, task.tenant_id):
        s.add(TaskTrace(tenant_id=task.tenant_id, task_id=task.id, origin=pack()))


def read_links(service, tenant, task_id, carrier=None):
    if not enabled(service, tenant):
        return []
    try:
        with service.db.session(tenant) as s:
            tenant_get(s, Task, task_id, tenant)
            row = row_for(s, tenant, task_id)
            if not row:
                return []
            values = [(row.origin, "origin"), (row.latest, "previous")]
            # The durable dispatch token binds the supplied context to this tenant/task.
            # A different task's valid traceparent, or arbitrary extras, cannot be linked.
            if (
                type(carrier) is dict
                and set(carrier) == {"traceparent"}
                and carrier["traceparent"] == row.dispatch
            ):
                values.append((row.dispatch, "dispatch"))
            return [item for value, role in values if (item := link(value, role))]
    except Exception:
        service.telemetry.langfuse_events.labels("correlation_failed").inc()
        return []


def record_segment(service, tenant, task_id, context, *, dispatch=False):
    if not enabled(service, tenant) or not pack(context):
        return None
    try:
        with service.db.session(tenant) as s:
            task = tenant_get(s, Task, task_id, tenant, True)
            if not dispatch:
                from agent_py.scheduling import check_execution_lease, execution_lease

                if execution_lease.get() is None:
                    return None  # A control-only tick has no owner lease to advance the pointer.
                check_execution_lease(s, task)
            row = row_for(s, tenant, task_id)
            if row is None:
                row = TaskTrace(tenant_id=tenant, task_id=task_id)
                s.add(row)
            if dispatch:
                if row.dispatch is None:
                    row.dispatch = pack(context)
                return {"traceparent": row.dispatch} if unpack(row.dispatch) else None
            row.latest = pack(context)
    except Exception:
        # Observation persistence never authorizes a retry or overrides a task result.
        service.telemetry.langfuse_events.labels("correlation_failed").inc()
    return None
