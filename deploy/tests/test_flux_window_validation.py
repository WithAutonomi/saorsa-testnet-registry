"""`--window` must not silently mean "the future" (found during V2-1061).

`--window` is interpolated straight into Flux `range(start: ...)`. A bare `6h`
is *now + 6h*, so the query returns nothing and `record-analysis` reports
"No host metric points found ... Nothing to record" and exits 0 -- which reads
as "this run collected no metrics", the opposite of the truth. On DEV-01 the
run's whole host-metric archival was nearly lost to it.
"""

import pytest

from saorsa_deploy.influx import validate_flux_window


@pytest.mark.parametrize("w", ["0", "", "  "])
def test_zero_and_empty_mean_whole_run(w):
    assert validate_flux_window(w) == "0"


@pytest.mark.parametrize("w", ["-6h", "-24h", "-7d", "-90m", "-1mo"])
def test_negative_durations_pass_through(w):
    assert validate_flux_window(w) == w


@pytest.mark.parametrize("w", ["6h", "24h", "7d", "90m", "1y"])
def test_positive_duration_is_refused(w):
    with pytest.raises(ValueError) as e:
        validate_flux_window(w)
    msg = str(e.value)
    assert "future" in msg
    assert f"-{w}" in msg, "the message must name the corrected value"


def test_rfc3339_instant_is_allowed():
    assert validate_flux_window("2026-08-30T13:34:16Z") == "2026-08-30T13:34:16Z"


def test_garbage_is_refused():
    with pytest.raises(ValueError):
        validate_flux_window("banana")
