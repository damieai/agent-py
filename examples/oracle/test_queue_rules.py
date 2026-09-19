"""Operator-owned oracle. Mount outside candidate source during sandbox verification."""

from queue_rules import overdue


def test_threshold_is_inclusive():
    assert overdue(60, 60) is True


def test_before_threshold_is_not_overdue():
    assert overdue(59, 60) is False


def test_after_threshold_is_overdue():
    assert overdue(61, 60) is True
