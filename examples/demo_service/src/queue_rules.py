"""Deliberately defective fixture. An item is overdue at the threshold, inclusive."""


def overdue(age_seconds: int, threshold_seconds: int) -> bool:
    return age_seconds > threshold_seconds
