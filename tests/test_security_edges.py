import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from agent_py.db import Inbox
from agent_py.domain import DomainError
from agent_py.patches import FileEdit, apply_patch
from agent_py.webhooks import consume, receive


def test_webhook_signed_idempotent_notice_does_not_fabricate_success(env, task):
    from test_execution import proposed

    service = env[0]
    op = proposed(env, task)
    service.settings.webhook_secret = SecretStr("s" * 40)
    service.settings.webhook_tenant = "t1"
    body = json.dumps({"event_id": "event", "operation_id": op.id}).encode()
    ts = str(int(time.time()))
    signature = hmac.new(b"s" * 40, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    with pytest.raises(DomainError, match="signature"):
        receive(service.db, service.settings, body, ts, "bad")
    receive(service.db, service.settings, body, ts, signature)
    receive(service.db, service.settings, body, ts, signature)
    with service.db.session("t1") as s:
        assert s.scalar(select(func.count()).select_from(Inbox)) == 1
    consume(service, "t1")
    assert service.execute("t1", op.id).status == "SUCCEEDED"
    assert service.remote.snapshot("t1", op.resource)["effect_count"] == 1


def test_patch_validates_all_files_before_writing(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    source = root / "src" / "app.py"
    source.write_text("x = 1\n")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    valid = FileEdit(path="src/app.py", original_sha256=checksum, content="x = 2\n")
    forbidden = FileEdit(path="../outside.py", original_sha256=checksum, content="x = 3\n")
    with pytest.raises(DomainError):
        apply_patch(root, [valid, forbidden])
    assert source.read_text() == "x = 1\n"
    assert apply_patch(root, [valid])["verification"] == "REQUIRED"
    with pytest.raises(DomainError, match="changed"):
        apply_patch(root, [valid])


def test_symlink_patch_denied(tmp_path):
    (tmp_path / "src").mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("private")
    (tmp_path / "src/app.py").symlink_to(outside)
    with pytest.raises(DomainError, match="Symlinks"):
        apply_patch(
            tmp_path,
            [
                FileEdit(
                    path="src/app.py",
                    original_sha256=hashlib.sha256(b"private").hexdigest(),
                    content="changed",
                )
            ],
        )


def test_no_success_without_verification(env, task):
    with pytest.raises(DomainError, match="verification"):
        env[0].finish("t1", task.id)


def test_creator_can_cancel_without_operator_role(env, task):
    service, p, _ = env
    service.stop(p.model_copy(update={"roles": ["developer"]}), task.id)
    assert service.get_task(p, task.id).cancelled


def test_model_reservation_can_only_dispatch_once(env, task):
    service = env[0]
    reservation = service.reserve("t1", task.id, "model", 1000)
    service.claim_inference("t1", reservation.id)
    with pytest.raises(DomainError, match="possibly billed"):
        service.claim_inference("t1", reservation.id)


def test_uncertified_live_write_is_rejected_before_dispatch(env, task):
    from test_execution import proposed

    from agent_py.adapters.simulation import DisabledLiveExecutor
    from agent_py.db import Operation

    service = env[0]
    op = proposed(env, task)
    service.remote = DisabledLiveExecutor()
    with pytest.raises(DomainError, match="certified"):
        service.execute("t1", op.id)
    with service.db.session("t1") as s:
        assert s.get(Operation, op.id).status == "NOT_SUBMITTED"


def test_unconfirmed_receipt_cannot_report_success(env, task):
    from test_execution import proposed

    service = env[0]
    op = proposed(env, task)
    recorded = service._record("t1", op.id, "SUCCEEDED", {"external_id": "untrusted"})
    assert recorded.status == "UNKNOWN"
    assert recorded.error == "UNVERIFIED_RECEIPT"


def test_concurrent_settlement_only_charges_once(env, task):
    from agent_py.db import DailyBudget

    service, p, _ = env
    reservation = service.reserve("t1", task.id, "concurrent", 1000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: service.settle("t1", reservation.id, 600), range(8)))
    current = service.get_task(p, task.id)
    assert (current.spent, current.reserved) == (600, 0)
    with service.db.session("t1") as s:
        daily = s.scalar(select(DailyBudget))
        assert (daily.spent, daily.reserved) == (600, 0)


def test_opposing_concurrent_approvals_have_one_winner(env, task):
    from test_execution import proposed

    from agent_py.db import Approval

    service, _, reviewer = env
    op = proposed(env, task, tool="deploy")
    with service.db.session("t1") as s:
        approval = s.scalar(select(Approval).where(Approval.operation_id == op.id))

    def decide(decision):
        try:
            return service.decide(reviewer, approval.id, decision, approval.payload_digest).status
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ["approve", "reject"]))
    assert results.count("APPROVAL_CLOSED") == 1
    assert len(set(results) & {"APPROVED", "REJECTED"}) == 1
