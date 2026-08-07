"""Offline tests for the RemoteCLIP loader guard.

Exercise the pure key-count guard only — no model load, no network.
"""

import pytest

from app.services.embeddings import _check_state_dict_applied


def test_guard_passes_on_clean_load():
    _check_state_dict_applied(0, 302)  # healthy RemoteCLIP load: 0 missing


def test_guard_tolerates_a_few_non_weight_keys():
    _check_state_dict_applied(3, 302)  # e.g. logit_scale / position_ids


def test_guard_raises_on_total_mismatch():
    # Wrong checkpoint/model: nothing applied -> would serve random weights.
    with pytest.raises(RuntimeError, match="mismatch"):
        _check_state_dict_applied(302, 302)


def test_guard_raises_above_threshold():
    with pytest.raises(RuntimeError):
        _check_state_dict_applied(20, 302)  # ~6.6% missing > 5% ceiling


def test_guard_noop_when_total_unknown():
    _check_state_dict_applied(0, 0)  # defensive: no total -> never raises
