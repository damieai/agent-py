"""Stable task-level export selection, independent of OTel trace IDs and business state."""

import hashlib
import hmac
import json


def pseudonym(settings, tenant, category, identifier):
    message = json.dumps([settings.environment, tenant, category, identifier]).encode()
    return hmac.new(
        settings.langfuse_pseudonym_key.get_secret_value().encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


def selected(settings, tenant, task_id):
    if not settings.langfuse_enabled or tenant != settings.langfuse_tenant:
        return False
    rate = settings.langfuse_sample_rate
    if rate == 0:
        return False
    if rate == 1:
        return True
    # Integer comparison avoids rounding a near-maximum hash up to 1.0.
    threshold = int(rate * (1 << 64))
    return int(pseudonym(settings, tenant, "sampling-v1", task_id)[:16], 16) < threshold
