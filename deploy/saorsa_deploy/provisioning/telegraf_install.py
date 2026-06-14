"""Shared Telegraf package-install operation definition.

Both the Elasticsearch log-forwarder (:mod:`telegraf`) and the InfluxDB
host-metrics forwarder (:mod:`telegraf_metrics`) install the exact same Telegraf
deb. The op name and command live here so the two provisioners stay byte-identical
and a single combined pass (see :mod:`telegraf_combined`) can install the package
exactly once per host. Each provisioner adds the op through its own ``add_op`` so
the install stays visible to module-level mocks.
"""

#: The Telegraf release installed on every provisioned VM.
TELEGRAF_DEB_URL = (
    "https://dl.influxdata.com/telegraf/releases/telegraf_1.33.0-1_amd64.deb"
)

#: pyinfra op name for the install step.
TELEGRAF_INSTALL_OP_NAME = "Install Telegraf"

#: Idempotent install command. No-op on hosts that already have the package
#: (``dpkg -s`` short-circuits), so it is safe to run on a re-provision.
TELEGRAF_INSTALL_COMMANDS = [
    "dpkg -s telegraf >/dev/null 2>&1 && echo 'TELEGRAF:SKIP' || "
    "(rm -f /tmp/telegraf.deb && "
    f"wget -q {TELEGRAF_DEB_URL} -O /tmp/telegraf.deb && "
    "dpkg -i /tmp/telegraf.deb && "
    "rm -f /tmp/telegraf.deb && "
    "echo 'TELEGRAF:INSTALLED')",
]
