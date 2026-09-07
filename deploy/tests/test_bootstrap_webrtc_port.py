"""The pinned WebRTC-Direct port on the bootstrap tier (V2-1169).

Bootstraps are the one node tier whose browser-facing address must not move: a
WASM/browser client seeds itself from a bootstrap multiaddr. Left unset,
ant-node *derives* the WebRTC port from the QUIC port, so it changes whenever
the QUIC port does. Non-bootstrap nodes deliberately keep the derived port.

Every test here comes in an omitted/emitted pair, because the load-bearing half
is the omission: no existing deployment's rendered unit or emitted argv may
change by so much as a byte.
"""

from saorsa_deploy.config.models import Bootstraps
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.provisioning.bootstrap import (
    AntBootstrapNodeProvisioner,
    AntBootstrapNodesProvisioner,
    _build_exec_start,
)

from .test_orchestrator_testnet import _minimal


class TestExecStart:
    def test_omitted_when_unset(self):
        assert "--webrtc-port" not in _build_exec_start(bootstrap_index=1)

    def test_emitted_when_set(self):
        result = _build_exec_start(bootstrap_index=1, webrtc_port=42768)
        assert "--webrtc-port 42768" in result

    def test_emitted_for_additional_bootstraps(self):
        result = _build_exec_start(
            bootstrap_index=3, bootstrap_peers=["1.2.3.4:10000"], webrtc_port=42768
        )
        assert "--webrtc-port 42768" in result

    def test_sits_directly_after_the_quic_port(self):
        # The two ports are a pair; keeping them adjacent is what makes the
        # rendered unit readable when someone is debugging a browser dial.
        result = _build_exec_start(bootstrap_index=1, port=10000, webrtc_port=42768)
        assert "--port 10000 --webrtc-port 42768" in result

    def test_zero_is_treated_as_unset(self):
        # ant-node accepts --webrtc-port 0 to mean "derive automatically", which
        # is exactly what omitting the flag already does. Emitting it would be a
        # pointless divergence from the pre-feature unit.
        assert "--webrtc-port" not in _build_exec_start(bootstrap_index=1, webrtc_port=0)


class TestProvisionersAcceptIt:
    def test_single_host_provisioner_defaults_to_none(self):
        p = AntBootstrapNodeProvisioner(ip="1.2.3.4", bootstrap_index=1)
        assert p.webrtc_port is None

    def test_single_host_provisioner_stores_it(self):
        p = AntBootstrapNodeProvisioner(ip="1.2.3.4", bootstrap_index=1, webrtc_port=42768)
        assert p.webrtc_port == 42768

    def test_parallel_provisioner_defaults_to_none(self):
        p = AntBootstrapNodesProvisioner(
            nodes=[{"ip": "1.2.3.4", "bootstrap_index": 2}], bootstrap_peers=["5.6.7.8:10000"]
        )
        assert p.webrtc_port is None

    def test_parallel_provisioner_renders_the_flag(self):
        p = AntBootstrapNodesProvisioner(
            nodes=[{"ip": "1.2.3.4", "bootstrap_index": 2}],
            bootstrap_peers=["5.6.7.8:10000"],
            webrtc_port=42768,
        )
        commands = p._build_config_commands(2)
        assert any("--webrtc-port 42768" in c for c in commands)


class TestSchema:
    def test_defaults_to_none(self):
        assert Bootstraps().webrtc_port is None

    def test_accepts_a_port(self):
        assert Bootstraps(webrtc_port=42768).webrtc_port == 42768


class TestOrchestrator:
    def _steps(self, **bootstraps):
        spec = {"count": 3, "port": 10000, **bootstraps}
        return build_deploy_steps(_minimal(bootstraps=spec))

    def _step(self, steps, head):
        return next(s for s in steps if s.argv[: len(head)] == head)

    def test_omitted_from_genesis_when_unset(self):
        step = self._step(self._steps(), ["nodes", "provision-genesis"])
        assert "--webrtc-port" not in step.argv

    def test_omitted_from_bootstraps_when_unset(self):
        step = self._step(self._steps(), ["bootstraps", "provision"])
        assert "--webrtc-port" not in step.argv

    def test_emitted_on_genesis_when_set(self):
        step = self._step(self._steps(webrtc_port=42768), ["nodes", "provision-genesis"])
        assert step.argv[step.argv.index("--webrtc-port") + 1] == "42768"

    def test_emitted_on_bootstraps_when_set(self):
        step = self._step(self._steps(webrtc_port=42768), ["bootstraps", "provision"])
        assert step.argv[step.argv.index("--webrtc-port") + 1] == "42768"

    def test_not_emitted_on_the_node_tier(self):
        # Regular nodes keep the derived/random port on purpose: their address
        # changes propagate over the DHT exactly as QUIC's do, so there is
        # nothing to pin, and pinning would collide across services on one VM.
        step = self._step(self._steps(webrtc_port=42768), ["nodes", "provision"])
        assert "--webrtc-port" not in step.argv
