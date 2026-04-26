from saorsa_deploy.influx import _parse_prod_upload_csv

PROD_UPLOAD_CSV = (
    ",result,table,_start,_stop,_time,host,region,_measurement,address,"
    "chunks_failed,chunks_stored,file_name,file_size,source_url,start_ns,"
    "status,store_concurrency,total_chunks,total_ms,upload_secs\n"
    ",_result,0,2026-04-01T00:00:00Z,2026-04-26T00:00:00Z,"
    "2026-04-19T16:19:36.708970856Z,prodhost-01,lon1,prod_upload,addr-abc,"
    "0,42,file.bin,1048576,https://example.com/file.bin,"
    "1776615435704524798,0,8,42,12345,3.5\n"
    ",_result,0,2026-04-01T00:00:00Z,2026-04-26T00:00:00Z,"
    "2026-04-19T16:30:00.000000000Z,prodhost-01,lon1,prod_upload,,"
    "2,40,partial.bin,2097152,https://example.com/partial.bin,"
    "1776616100000000000,1,8,42,67890,7.2\n"
)


def test_parses_success_and_partial_rows():
    points = _parse_prod_upload_csv(PROD_UPLOAD_CSV)
    assert len(points) == 2

    success = points[0]
    assert success["host"] == "prodhost-01"
    assert success["region"] == "lon1"
    assert success["status"] == 0
    assert success["chunks_stored"] == 42
    assert success["chunks_failed"] == 0
    assert success["total_chunks"] == 42
    assert success["address"] == "addr-abc"
    assert success["file_name"] == "file.bin"
    assert success["source_url"] == "https://example.com/file.bin"
    assert success["upload_secs"] == 3.5
    assert success["total_ms"] == 12345
    assert success["file_size"] == 1048576
    assert success["store_concurrency"] == 8
    assert success["_time"].tzinfo is not None

    partial = points[1]
    assert partial["status"] == 1
    assert partial["chunks_failed"] == 2
    assert partial["address"] == ""


def test_skips_other_measurements():
    body = (
        ",result,table,_time,host,_measurement,_value,_field\n"
        ",_result,0,2026-04-19T16:19:36Z,hostA,upload,1,status\n"
    )
    assert _parse_prod_upload_csv(body) == []


def test_skips_rows_without_host():
    body = (
        ",result,table,_time,host,_measurement,status,total_ms\n"
        ",_result,0,2026-04-19T16:19:36Z,,prod_upload,0,100\n"
    )
    assert _parse_prod_upload_csv(body) == []
