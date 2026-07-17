"""Offline unit tests for the registry phase tracker."""

from app.services import registry_state as rs


def test_tracker_phase_transitions():
    rs.mark_warming()
    assert rs.phase() == "warming"

    rs.mark_ready()
    assert rs.phase() == "ready"
    assert rs._tracker.finished_at is not None

    rs.mark_error("boom")
    assert rs.phase() == "error"
    assert rs._tracker.error == "boom"
