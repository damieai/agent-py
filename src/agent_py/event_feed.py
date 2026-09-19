"""Bounded, currently authorized pages for SSE consumers."""

from sqlalchemy import select

from agent_py.db import Task, TaskEvent, tenant_get
from agent_py.domain import DomainError
from agent_py.security import authenticate, authorize


def event_page(service, token, task_id, cursor):
    principal = authenticate(service.settings, token)
    with service.db.snapshot(principal.tenant_id) as s:
        task = tenant_get(s, Task, task_id, principal.tenant_id)
        authorize(s, principal, task)
        if cursor > task.next_sequence:
            raise DomainError("CURSOR_AHEAD", "Event cursor is ahead of task history", 409)
        rows = s.scalars(
            select(TaskEvent)
            .where(
                TaskEvent.tenant_id == principal.tenant_id,
                TaskEvent.task_id == task_id,
                TaskEvent.sequence > cursor,
            )
            .order_by(TaskEvent.sequence)
            .limit(200)
        ).all()
        entries = [
            {"sequence": row.sequence, "type": row.event_type, "payload": row.payload}
            for row in rows
        ]
        if entries and entries[0]["sequence"] != cursor + 1:
            raise DomainError("EVENT_GAP", "Event history contains a gap", 409)
        if any(b["sequence"] != a["sequence"] + 1 for a, b in zip(entries, entries[1:])):
            raise DomainError("EVENT_GAP", "Event history contains a gap", 409)
        if not entries and cursor < task.next_sequence:
            raise DomainError("EVENT_GAP", "Event history is incomplete", 409)
        terminal = (
            task.status == "TERMINATED"
            and (entries[-1]["sequence"] if entries else cursor) == task.next_sequence
        )
    return entries, terminal
