"""Read-only investigation views, authorized against every frozen evidence dependency."""

from sqlalchemy import select

from agent_py.context import ContextBundle, ContextCompiler
from agent_py.db import InvestigationRound, Reservation
from agent_py.domain import InvestigationDecision
from agent_py.investigation_loop import MAX_ROUNDS, normalized


def authorized_rounds(db, principal, task):
    with db.session(principal.tenant_id) as s:
        rounds = s.scalars(
            select(InvestigationRound)
            .where(
                InvestigationRound.tenant_id == principal.tenant_id,
                InvestigationRound.task_id == task.id,
            )
            .order_by(InvestigationRound.ordinal)
        ).all()
    compiler = ContextCompiler(db)
    for row in rounds:
        compiler.validate(
            principal,
            task.contract["project"],
            task.contract["environment"],
            ContextBundle(**row.context),
            task_id=task.id,
        )
    return rounds


def investigation_details(service, principal, task_id):
    task = service.get_task(principal, task_id)
    if task.contract.get("workflow") != "investigation_loop":
        return None
    rounds = authorized_rounds(service.db, principal, task)
    with service.db.session(principal.tenant_id) as s:
        reservations = {
            r.call_key: r
            for r in s.scalars(
                select(Reservation).where(
                    Reservation.tenant_id == principal.tenant_id,
                    Reservation.task_id == task_id,
                    Reservation.call_key.like("investigation-loop:v1:%"),
                )
            )
        }
    items, queries, digests = [], [], []
    stop_reason = None
    for row in rounds:
        reservation = reservations.get(f"investigation-loop:v1:{row.ordinal}")
        decision = (
            InvestigationDecision.model_validate(reservation.decision)
            if (reservation is not None and reservation.decision is not None)
            else None
        )
        if not row.context["documents"]:
            state = stop_reason = "NO_EVIDENCE"
        elif row.context["digest"] in digests:
            state = stop_reason = "NO_PROGRESS"
        elif decision is not None:
            state = "DECIDED"
            if decision.stop:
                stop_reason = "MODEL_STOP"
            elif normalized(decision.next_query) in queries + [normalized(row.query)]:
                stop_reason = "REPEATED_QUERY"
            elif row.ordinal == MAX_ROUNDS:
                stop_reason = "ROUND_LIMIT"
        elif reservation is not None and reservation.actual is not None:
            # Settled without a validated decision: do not offer blind retries.
            state = "RESPONSE_UNKNOWN"
        elif reservation is not None and reservation.dispatched:
            state = "IN_FLIGHT"
        else:
            state = "READY"
        queries.append(normalized(row.query))
        digests.append(row.context["digest"])
        items.append(
            {
                "ordinal": row.ordinal,
                "query": row.query,
                "state": state,
                "context_digest": row.context["digest"],
                "decision": decision.model_dump() if decision else None,
                "evidence": [
                    {
                        key: doc[key]
                        for key in (
                            "id",
                            "source",
                            "version",
                            "chunk_id",
                            "start_line",
                            "end_line",
                            "symbol",
                        )
                        if key in doc
                    }
                    for doc in row.context["documents"]
                ],
                "spent_micro_usd": reservation.actual
                if reservation and reservation.actual is not None
                else 0,
                "reserved_micro_usd": reservation.maximum
                if reservation and reservation.actual is None
                else 0,
            }
        )
    # Authorize again after materializing the view; this endpoint never advances the task.
    service.get_task(principal, task_id)
    return {
        "max_rounds": MAX_ROUNDS,
        "rounds": items,
        "stop_reason": stop_reason,
        "status": task.status,
        "waiting_reason": task.waiting_reason,
        "executed": False,
        "requires_human_review": True,
    }
