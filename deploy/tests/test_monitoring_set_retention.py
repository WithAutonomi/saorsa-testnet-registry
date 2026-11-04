"""`monitoring set-retention` (INFRA-484).

`--influx-retention-days` applies at first provision only -- it is baked into the
bucket-creation calls in `influxdb_setup.py`, which exits early on a stack that
already has a token -- so there was no way to change retention on a running
deployment. The four permanent production transfer deployments run at 7 days,
and that window is the whole reason their download history read as lost.

Two properties matter, and both are about being CHECKABLE: the command reads
each bucket back rather than reporting the value it asked for, and it covers
every bucket the stack creates, because they expire independently.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from saorsa_deploy.cmd.monitoring_set_retention import (
    STACK_BUCKETS,
    _fmt,
    _retention_seconds,
    cmd_monitoring_set_retention,
)
from saorsa_deploy.provisioning.monitoring import (
    INFLUXDB_BUCKET,
    INFLUXDB_CLIENT_BUCKET,
    INFLUXDB_WASM_BUCKET,
)

MODULE = "saorsa_deploy.cmd.monitoring_set_retention"

STATE = {
    "influxdb_url": "http://10.0.0.1:8086",
    "influxdb_token": "tok",
    "influxdb_org": "ant",
}


class _FakeInflux:
    """Stands in for the InfluxDB the provisioner installs — **version 2.7.10**.

    Two of its behaviours are load-bearing and the previous fake modelled
    neither, which is how a `--days 0` that could never work passed its tests
    (review findings 1 and 4). Both were confirmed against 2.7.10 in a
    container:

    * ``PATCH {"retentionRules": []}`` returns 200 and changes NOTHING. An
      empty list means "no retention update" on update, where at create time it
      means "no retention" — so infinite has to be an explicit
      ``everySeconds: 0`` rule.
    * a ``name`` filter matching nothing returns **404** with
      ``bucket "<name>" not found``, not 200 and an empty list.
    """

    def __init__(self, buckets=None, patch_error=None, missing=()):
        rules = [{"type": "expire", "everySeconds": 604800}]
        self.buckets = {
            name: {"id": f"id-{i}", "name": name, "retentionRules": list(rules)}
            for i, name in enumerate(buckets if buckets is not None else STACK_BUCKETS)
            if name not in missing
        }
        self.patch_error = patch_error
        self.patches: list[tuple[str, list]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        name = (params or {}).get("name")
        if name not in self.buckets:
            # 2.7.10's real answer, not an empty list.
            return SimpleNamespace(
                status_code=404,
                ok=False,
                text=f'{{"code":"not found","message":"bucket "{name}" not found"}}',
                json=lambda: {"code": "not found"},
                raise_for_status=lambda: (_ for _ in ()).throw(
                    requests.HTTPError("404 Client Error")
                ),
            )
        return SimpleNamespace(
            status_code=200,
            ok=True,
            raise_for_status=lambda: None,
            json=lambda: {"buckets": [self.buckets[name]]},
        )

    def patch(self, url, json=None, headers=None, timeout=None):
        if self.patch_error is not None:
            raise self.patch_error
        bucket_id = url.rsplit("/", 1)[-1]
        for bucket in self.buckets.values():
            if bucket["id"] != bucket_id:
                continue
            rules = list(json["retentionRules"])
            # The behaviour that hid the P1: an empty list is not an update.
            if rules:
                bucket["retentionRules"] = rules
            self.patches.append((bucket["name"], rules))
            return SimpleNamespace(
                status_code=200, raise_for_status=lambda: None, json=lambda: bucket
            )
        raise AssertionError(f"unknown bucket id {bucket_id}")


def _run(days, influx, state=STATE):
    with (
        patch(f"{MODULE}.load_monitoring_state", return_value=dict(state)),
        patch(f"{MODULE}.requests.get", influx.get),
        patch(f"{MODULE}.requests.patch", influx.patch),
    ):
        try:
            cmd_monitoring_set_retention(SimpleNamespace(name="PROD-DL-01", days=days))
            return 0
        except SystemExit as exc:
            return exc.code


class TestEveryBucketTheStackCreates:
    def test_the_set_is_the_three_the_provisioner_creates(self):
        assert set(STACK_BUCKETS) == {
            INFLUXDB_BUCKET,
            INFLUXDB_CLIENT_BUCKET,
            INFLUXDB_WASM_BUCKET,
        }

    def test_all_three_are_patched(self):
        """They expire independently; leaving one behind keeps half expiring."""
        influx = _FakeInflux()
        assert _run(0, influx) == 0
        assert {name for name, _ in influx.patches} == set(STACK_BUCKETS)

    def test_zero_days_sends_an_explicit_zero_duration_rule(self):
        """Infinite is `everySeconds: 0`, NOT an empty list (finding 1).

        On InfluxDB 2.7.10 an empty `retentionRules` is "no retention update"
        on PATCH -- it returns 200 and changes nothing -- although at create
        time it means "no retention", which is why `influxdb_setub.py` can send
        it. Confirmed against 2.7.10 in a container: the PATCH succeeded, the
        read-back still said seven days, and `--days 0` could never complete.
        """
        influx = _FakeInflux()
        assert _run(0, influx) == 0
        for name in STACK_BUCKETS:
            assert influx.buckets[name]["retentionRules"] == [{"type": "expire", "everySeconds": 0}]
            # And that is what the reader calls infinite.
            assert _retention_seconds(influx.buckets[name]) == 0

    def test_an_empty_rule_list_is_never_sent(self):
        """The shape that silently did nothing must not come back."""
        influx = _FakeInflux()
        _run(0, influx)
        assert influx.patches
        assert all(rules for _name, rules in influx.patches)

    def test_an_absent_rule_list_also_reads_as_infinite(self):
        """What `influxdb_setup.py` leaves behind at create time."""
        assert _retention_seconds({"retentionRules": []}) == 0

    def test_a_positive_day_count_installs_one(self):
        influx = _FakeInflux()
        assert _run(14, influx) == 0
        for name in STACK_BUCKETS:
            assert influx.buckets[name]["retentionRules"] == [
                {"type": "expire", "everySeconds": 14 * 86400}
            ]

    def test_an_absent_bucket_is_not_an_error(self):
        """A stack provisioned before the WASM tier existed has no such bucket."""
        influx = _FakeInflux(missing={INFLUXDB_WASM_BUCKET})
        assert _run(0, influx) == 0
        assert {name for name, _ in influx.patches} == set(STACK_BUCKETS) - {INFLUXDB_WASM_BUCKET}


class TestItIsCheckable:
    def test_a_bucket_that_did_not_take_the_change_fails_the_run(self):
        """The read-back is the point: "the PATCH returned 200" is not success."""
        influx = _FakeInflux()
        real_patch = influx.patch

        def _patch_that_does_nothing(url, json=None, headers=None, timeout=None):
            if url.endswith("id-1"):
                bucket = next(b for b in influx.buckets.values() if b["id"] == "id-1")
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: bucket)
            return real_patch(url, json=json, headers=headers, timeout=timeout)

        influx.patch = _patch_that_does_nothing
        assert _run(0, influx) == 1

    def test_a_refused_patch_fails_the_run(self):
        error = requests.HTTPError()
        error.response = SimpleNamespace(status_code=403)
        assert _run(0, _FakeInflux(patch_error=error)) == 1

    def test_a_negative_day_count_is_refused_before_anything_is_touched(self):
        influx = _FakeInflux()
        assert _run(-1, influx) == 1
        assert influx.patches == []

    def test_a_stack_with_no_influx_in_state_is_refused(self):
        influx = _FakeInflux()
        assert _run(0, influx, state={"influxdb_org": "ant"}) == 1
        assert influx.patches == []

    def test_a_missing_monitoring_stack_is_refused(self):
        with patch(f"{MODULE}.load_monitoring_state", side_effect=RuntimeError("no state")):
            with pytest.raises(SystemExit) as exc:
                cmd_monitoring_set_retention(SimpleNamespace(name="NOPE", days=0))
        assert exc.value.code == 1


class TestFormatting:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "infinite"),
            (604800, "7d (604800s)"),
            (86400, "1d (86400s)"),
            (43200, "0.50d (43200s)"),
        ],
    )
    def test_fmt(self, seconds, expected):
        assert _fmt(seconds) == expected

    def test_an_absent_expire_rule_reads_as_infinite_not_unknown(self):
        assert _retention_seconds({"retentionRules": []}) == 0
        assert _retention_seconds({}) == 0
