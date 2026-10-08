"""Filesystem occupancy capture (V2-758).

`[[inputs.disk]]` was collected by Telegraf from the start but never queried, so
"how much data is resident" existed nowhere in the registry and the
"disk consumed per GB stored" gate could not be assessed. These tests cover the
capture path: the Flux query, the gauge normalisation keyed by mountpoint, the
rollup's `disk_usage_by_mount` (including the endpoints that make growth
computable), and the bucket writer's node-storage restriction.
"""

from datetime import datetime, timezone

from saorsa_deploy.influx import (
    FLUX_FAR_FUTURE,
    HOST_FLUX_DISK,
    NODE_STORAGE_ROLE,
    aggregate_host_metrics,
    bucket_host_metric_samples,
    node_storage_mount,
    normalise_host_metric_points,
)

UTC = timezone.utc
NODE_MOUNT = "/mnt/ant-node-storage"


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _disk_point(
    host: str,
    ts: str,
    path: str,
    used: int,
    volume_role: str | None = None,
) -> dict:
    point = {
        "_time": _ts(ts),
        "host": host,
        "_measurement": "disk",
        "path": path,
        "_field": "used",
        "_value": str(used),
    }
    if volume_role is not None:
        point["volume_role"] = volume_role
    return point


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


def test_disk_query_reads_used_from_the_disk_measurement():
    flux = HOST_FLUX_DISK.format(bucket="testnet-metrics", start="0", stop="s")
    assert 'r._measurement == "disk"' in flux
    assert 'r._field == "used"' in flux


def test_disk_query_carries_an_explicit_stop():
    # Same regression guard as every other query in this module: Flux defaults
    # `stop` to now(), which silently drops forward clock-skewed hosts.
    flux = HOST_FLUX_DISK.format(bucket="b", start="0", stop=FLUX_FAR_FUTURE)
    assert f"stop: {FLUX_FAR_FUTURE}" in flux


# ---------------------------------------------------------------------------
# Normalise: occupancy is a gauge, keyed by mountpoint
# ---------------------------------------------------------------------------


def test_occupancy_passes_through_without_a_delta_stage():
    # Unlike net/diskio counters, `used` is already the quantity of interest.
    normalised = normalise_host_metric_points(
        {
            "disk": [
                _disk_point("h1", "2026-07-01T00:00:00", NODE_MOUNT, 1_000),
                _disk_point("h1", "2026-07-01T00:00:10", NODE_MOUNT, 3_000),
            ]
        }
    )
    assert [s["value"] for s in normalised.samples] == [1_000.0, 3_000.0]
    assert {s["metric"] for s in normalised.samples} == {"disk_used_bytes"}


def test_occupancy_is_keyed_by_mountpoint_not_device():
    # `disk` also carries a `device` tag, but its value is the fstab-style
    # source ("mapper/vg-lv"), which does not match the kernel name diskio
    # reports. The mountpoint is the only key that lines up across both.
    normalised = normalise_host_metric_points(
        {"disk": [_disk_point("h1", "2026-07-01T00:00:00", NODE_MOUNT, 1)]}
    )
    assert normalised.samples[0]["device"] == NODE_MOUNT


def test_only_the_exact_node_storage_marker_counts():
    normalised = normalise_host_metric_points(
        {
            "disk": [
                _disk_point("h1", "2026-07-01T00:00:00", "/", 1, volume_role="root"),
                _disk_point(
                    "h1",
                    "2026-07-01T00:00:00",
                    NODE_MOUNT,
                    2,
                    volume_role=NODE_STORAGE_ROLE,
                ),
            ]
        }
    )
    assert normalised.mounts["h1"] == {"/": None, NODE_MOUNT: NODE_STORAGE_ROLE}


def test_untagged_mountpoints_are_still_recorded():
    # A client VM, or a host provisioned before the tag existed. The rollup
    # keeps every mountpoint; only the bucket writer narrows.
    normalised = normalise_host_metric_points(
        {"disk": [_disk_point("h1", "2026-07-01T00:00:00", "/", 5)]}
    )
    assert normalised.mounts["h1"] == {"/": None}


def test_occupancy_contributes_to_the_host_observation_window():
    normalised = normalise_host_metric_points(
        {"disk": [_disk_point("h1", "2026-07-01T00:00:00", NODE_MOUNT, 1)]}
    )
    assert normalised.time_bounds["h1"] == [
        _ts("2026-07-01T00:00:00"),
        _ts("2026-07-01T00:00:00"),
    ]


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


def _growth_run() -> list[dict]:
    return [
        _disk_point(
            "h1",
            "2026-07-01T00:00:00",
            NODE_MOUNT,
            1_000,
            volume_role=NODE_STORAGE_ROLE,
        ),
        _disk_point(
            "h1",
            "2026-07-01T00:05:00",
            NODE_MOUNT,
            5_000,
            volume_role=NODE_STORAGE_ROLE,
        ),
        _disk_point(
            "h1",
            "2026-07-01T00:10:00",
            NODE_MOUNT,
            9_000,
            volume_role=NODE_STORAGE_ROLE,
        ),
    ]


def test_rollup_records_growth_between_the_endpoints():
    entry = aggregate_host_metrics({"disk": _growth_run()})[0]
    usage = entry["disk_usage_by_mount"][NODE_MOUNT]["used_bytes"]
    assert usage["first"] == 1_000
    assert usage["last"] == 9_000
    assert usage["growth_bytes"] == 8_000


def test_growth_uses_time_order_not_arrival_order():
    points = list(reversed(_growth_run()))
    entry = aggregate_host_metrics({"disk": points})[0]
    usage = entry["disk_usage_by_mount"][NODE_MOUNT]["used_bytes"]
    assert (usage["first"], usage["last"]) == (1_000, 9_000)


def test_growth_is_last_minus_first_not_max_minus_min():
    # A run that filled the volume and was then pruned must not be reported as
    # if nothing had been reclaimed. min/max still expose that it happened.
    points = [
        _disk_point("h1", "2026-07-01T00:00:00", NODE_MOUNT, 1_000),
        _disk_point("h1", "2026-07-01T00:05:00", NODE_MOUNT, 9_000),
        _disk_point("h1", "2026-07-01T00:10:00", NODE_MOUNT, 2_000),
    ]
    usage = aggregate_host_metrics({"disk": points})[0]["disk_usage_by_mount"][NODE_MOUNT][
        "used_bytes"
    ]
    assert usage["growth_bytes"] == 1_000
    assert (usage["min"], usage["max"]) == (1_000, 9_000)


def test_rollup_keeps_every_mountpoint():
    points = [
        _disk_point("h1", "2026-07-01T00:00:00", "/", 100),
        _disk_point(
            "h1",
            "2026-07-01T00:00:00",
            NODE_MOUNT,
            200,
            volume_role=NODE_STORAGE_ROLE,
        ),
    ]
    entry = aggregate_host_metrics({"disk": points})[0]
    assert sorted(entry["disk_usage_by_mount"]) == ["/", NODE_MOUNT]


def test_node_storage_mount_finds_the_tagged_mountpoint():
    entry = aggregate_host_metrics(
        {
            "disk": [
                _disk_point("h1", "2026-07-01T00:00:00", "/", 100),
                _disk_point(
                    "h1",
                    "2026-07-01T00:00:00",
                    NODE_MOUNT,
                    200,
                    volume_role=NODE_STORAGE_ROLE,
                ),
            ]
        }
    )[0]
    found = node_storage_mount(entry)
    assert found is not None
    assert found[0] == NODE_MOUNT


def test_node_storage_mount_is_none_without_the_tag():
    # Client VMs, and every run recorded before this capture existed.
    entry = aggregate_host_metrics({"disk": [_disk_point("h1", "2026-07-01T00:00:00", "/", 100)]})[
        0
    ]
    assert node_storage_mount(entry) is None


def test_hosts_without_disk_points_get_an_empty_usage_map():
    entry = aggregate_host_metrics(
        {
            "cpu": [
                {
                    "_time": _ts("2026-07-01T00:00:00"),
                    "host": "h1",
                    "_measurement": "cpu",
                    "cpu": "cpu-total",
                    "_field": "usage_idle",
                    "_value": "90",
                }
            ]
        }
    )[0]
    assert entry["disk_usage_by_mount"] == {}


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


def test_buckets_restrict_occupancy_to_the_node_storage_mount():
    # The root filesystem's occupancy is dominated by the OS and says nothing
    # about stored chunks; storing it per bucket is rows for no signal.
    points = [
        _disk_point("h1", "2026-07-01T00:00:00", "/", 100),
        _disk_point(
            "h1",
            "2026-07-01T00:00:00",
            NODE_MOUNT,
            200,
            volume_role=NODE_STORAGE_ROLE,
        ),
    ]
    rows = bucket_host_metric_samples(
        normalise_host_metric_points({"disk": points}), bucket_secs=300
    )
    assert [(r["device"], r["avg"]) for r in rows] == [(NODE_MOUNT, 200.0)]
    assert rows[0]["volume_role"] == NODE_STORAGE_ROLE


def test_buckets_can_keep_every_mountpoint_when_asked():
    points = [
        _disk_point("h1", "2026-07-01T00:00:00", "/", 100),
        _disk_point(
            "h1",
            "2026-07-01T00:00:00",
            NODE_MOUNT,
            200,
            volume_role=NODE_STORAGE_ROLE,
        ),
    ]
    rows = bucket_host_metric_samples(
        normalise_host_metric_points({"disk": points}),
        bucket_secs=300,
        node_storage_only=False,
    )
    assert sorted(r["device"] for r in rows) == ["/", NODE_MOUNT]


def test_buckets_keep_each_window_endpoint_recoverable():
    # Growth over a re-derived sub-window needs the first and last bucket, so
    # each bucket must stand alone rather than being pooled across the run.
    points = [
        _disk_point(
            "h1",
            f"2026-07-01T00:{minute:02d}:00",
            NODE_MOUNT,
            used,
            volume_role=NODE_STORAGE_ROLE,
        )
        for minute, used in ((0, 1_000), (5, 5_000), (10, 9_000))
    ]
    rows = bucket_host_metric_samples(
        normalise_host_metric_points({"disk": points}), bucket_secs=300
    )
    assert [r["avg"] for r in rows] == [1_000.0, 5_000.0, 9_000.0]
