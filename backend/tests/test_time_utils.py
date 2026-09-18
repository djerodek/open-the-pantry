import os
import sys

from app.time_utils import (  # noqa: E402
    ddhhmm_to_minutes,
    minutes_to_ddhhmm,
    minutes_to_label,
    available_time_buckets,
)


def test_ddhhmm_round_trip():
    assert ddhhmm_to_minutes("00:02:38") == 2 * 60 + 38
    assert minutes_to_ddhhmm(158) == "00:02:38"


def test_ddhhmm_blank_is_none():
    assert ddhhmm_to_minutes("") is None
    assert ddhhmm_to_minutes(None) is None
    assert ddhhmm_to_minutes("   ") is None


def test_ddhhmm_invalid_format():
    assert ddhhmm_to_minutes("not a time") is None


def test_ddhhmm_with_days():
    assert ddhhmm_to_minutes("01:02:03") == 24 * 60 + 2 * 60 + 3


def test_minutes_to_label():
    assert minutes_to_label(20) == "20m"
    assert minutes_to_label(60) == "1h"
    assert minutes_to_label(80) == "1h 20m"


def test_available_time_buckets_empty():
    assert available_time_buckets([]) == []


def test_available_time_buckets_caps_at_data():
    """Only buckets up to the smallest 20-min multiple covering the max
    logged time should appear -- not endless buckets beyond what any
    recipe actually needs."""
    buckets = available_time_buckets([5, 158])  # 158 min = 2h38m
    minutes_list = [b["minutes"] for b in buckets]
    assert minutes_list[0] == 20
    assert minutes_list[-1] == 160  # next 20-min multiple >= 158
    assert 180 not in minutes_list  # no bucket beyond what's needed


def test_available_time_buckets_short_recipe():
    buckets = available_time_buckets([5])
    assert [b["minutes"] for b in buckets] == [20]


# --- edge cases: zero, negative, non-string, float inputs ---

def test_ddhhmm_rejects_non_string_types():
    for bad in [123, 12.5, [], {}, True]:
        assert ddhhmm_to_minutes(bad) is None, f"{bad!r} should not parse"


def test_ddhhmm_rejects_negative_and_malformed():
    for bad in ["-01:00:00", "00:-1:00", "1:2:3:4", "::", "abc:def:ghi"]:
        assert ddhhmm_to_minutes(bad) is None, f"{bad!r} should not parse"


def test_ddhhmm_zero():
    assert ddhhmm_to_minutes("00:00:00") == 0
    assert minutes_to_ddhhmm(0) == "00:00:00"


def test_minutes_to_ddhhmm_none_passthrough():
    assert minutes_to_ddhhmm(None) is None


def test_minutes_to_label_zero():
    assert minutes_to_label(0) == "0m"


def test_available_time_buckets_ignores_zero_and_negative():
    """A zero or negative logged duration shouldn't produce a nonsensical
    bucket list or crash the filter."""
    assert available_time_buckets([0]) == [{"minutes": 20, "label": "20m", "hour_group": 0}]
    buckets = available_time_buckets([0, 45])
    assert [b["minutes"] for b in buckets] == [20, 40, 60]


def test_available_time_buckets_boundary_exact_multiple():
    """A duration exactly on a 20-minute boundary shouldn't add a
    redundant bucket beyond it."""
    assert [b["minutes"] for b in available_time_buckets([60])] == [20, 40, 60]
