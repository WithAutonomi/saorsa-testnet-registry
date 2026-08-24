"""Run-time measurement tooling for testnet runs (`saorsa-deploy analysis ...`).

These commands exist because the numbers a testnet run gates on are mostly not
in the registry's standard aggregates:

* per-flow facts (connection lifetimes, keep-alive cadence, whether a peer was
  told a connection closed) come from packet capture, since the lifecycle events
  are `debug!` in saorsa-core and a fleet at debug is not viable;
* per-phase UDP counters must be read from BOTH network namespaces over SSH,
  because the NAT-simulated VMs run their nodes inside `ant_nat` where telegraf
  cannot see them;
* phrase counts and cumulative-counter deltas over an arbitrary window come
  from the run's Elasticsearch/InfluxDB, which die with the testnet.

Every command takes the deployment name and derives hosts, the NAT tier and the
SSH key from the deployment's inventory (see :mod:`saorsa_deploy.analysis.hosts`).
The remote-side shell payloads are kept verbatim from the keep-alive A/B runs
(V2-996/V2-988) and the backstop run (V2-1059), where they were proven on live
fleets; only the local drivers were rewritten.
"""
