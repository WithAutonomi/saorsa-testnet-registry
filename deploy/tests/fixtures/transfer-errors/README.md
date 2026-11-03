# Transfer failure fixtures (INFRA-484)

What `ant` actually says when a transfer fails, and what
`saorsa_deploy.transfer_errors.classify_transfer_error` must make of it.

Every case in `cases.json` carries a `provenance` field with one of three
values, and the distinction is the point of this directory. This repository has
an explicit position on hand-written fixtures, recorded for the browser trace
fixture: a hand-written one encoding the assumed shape passed every test while
two of three derived fields would have been zero on every live round. So nothing
here is invented silently.

## `provenance: "captured"` — produced on demand, committed verbatim

`ant file download <64 random hex>` against the production network is free and
read-only, and it produces a real `not found: DataMap chunk not found at <hex>`
through the whole stderr path. Both committed `.stderr` / `.stdout` pairs were
produced on **2026-11-03** like this:

```bash
# ant 0.3.12, the release the four permanent deployments run (PROD-WDL-01 is on
# 0.3.11). Downloaded from the ant-cli-v0.3.12 GitHub release:
#   gh api repos/WithAutonomi/ant-client/releases/tags/ant-cli-v0.3.12 \
#     --jq '.assets[] | select(.name=="ant-0.3.12-x86_64-unknown-linux-musl.tar.gz") | .browser_download_url'
ADDR=$(python3 -c "import secrets; print(secrets.token_hex(32))")
BOOT=$(python3 - <<'PY'
# the `quic` list out of the bootstrap_peers.toml that ships in the tarball
print(",".join([
    "/ip4/207.148.94.42/udp/10000/quic", "/ip4/45.77.50.10/udp/10000/quic",
    "/ip4/66.135.23.83/udp/10000/quic", "/ip4/149.248.9.2/udp/10000/quic",
    "/ip4/49.12.119.240/udp/10000/quic", "/ip4/5.161.25.133/udp/10000/quic",
    "/ip4/18.228.202.183/udp/10000/quic",
]))
PY
)

./ant -b "$BOOT" file download "$ADDR" -o out.bin \
  > native-download-not-found.stdout 2> native-download-not-found.stderr
# exit 1

./ant --json -b "$BOOT" file download "$ADDR" -o out2.bin \
  > native-download-not-found-json.stdout 2> native-download-not-found-json.stderr
# exit 1
```

Two things the capture establishes that no reading of the source proves on its
own:

* **stdout is EMPTY on a failed download, `--json` included.** Both `.stdout`
  files are zero bytes. The only `println!` of `DownloadJsonResult` sits after
  the `?` that aborts, so there is no result document to read and the stderr
  line is the whole of the evidence. This is why the native read path is
  classified from text at all.
* **The wrap is `Failed to fetch public DataMap:`, not `Download failed:`.**
  Both prefixes exist in `ant-cli/src/commands/data/file.rs`; the data-map fetch
  fails earlier than the download the other one wraps. The classifier therefore
  matches ant-core's `Display` text as a SUBSTRING and never as a prefix.

## `provenance: "observed"` — real rows, read out of the registry

The seven `observed-*` cases are the `reason` column of every non-success row of
`deployment_prod_upload_results` for deployment ids **672 (PROD-UL-01)** and
**673 (PROD-WUL-01)**, read from the live registry on **2026-11-03**. They are
verbatim and already clipped to 500 characters — by `describeError` in the WASM
client, which is the cap `clip_reason` mirrors.

`observed-672-...` is `ant exit 1`: the empty signal this whole issue exists to
replace. It is kept as a case precisely because it must classify as `other` —
there is nothing in it to classify. That row is **not** rewritten; see the
issue.

## `provenance: "derived"` — from the source, and said so

The chunk-exhaustion sentence cannot be provoked on demand: it needs a chunk
whose entire close group stops answering part-way through a download. The same
goes for the local-fault variants and for the browser-path size refusal. Those
cases are **derived from the `#[error(...)]` literals** in
`ant-core/src/data/error.rs` and the format strings around them, at the ref
named in `cases.json`'s `ant_client_ref`. Each case's `source` names the file.

They are **not observed**, and a reader should treat the exact payload text in
them as illustrative while the prefix — which is all the classifier matches on —
is exact.

## The pinned ref

`cases.json` carries `ant_client_ref: "ant-cli-v0.3.11"` and
`deploy/tests/test_transfer_error_classifier.py` asserts that every marker the
classifier matches on still appears in the ant-core source at that tag, when the
`ant-client` checkout is present. So a client upgrade that moves these strings
fails loudly instead of quietly reclassifying every failure as `other`.

The test SKIPS when `~/dev/github.com/WithAutonomi/ant-client` is absent, which
is CI: the assertion is a developer tripwire, not a build dependency on a second
repository. The cases themselves run everywhere.
