"""Step primitives shared by the per-kind orchestrators.

A :class:`Step` is one primitive ``saorsa-deploy`` invocation expressed as an
argv list (no ``saorsa-deploy`` prefix). The orchestrator turns a manifest into
an ordered ``list[Step]``; ``plan`` prints them and ``up``/``down`` execute them.
"""

from __future__ import annotations

from dataclasses import dataclass

from saorsa_deploy.config.models import Binary


@dataclass(frozen=True)
class Step:
    argv: list[str]  # e.g. ["bootstraps", "infra", "--name", "STG-01", ...]
    label: str  # human-readable description for `plan` output
    best_effort: bool = False  # if True, a non-zero exit is warned about, not fatal

    def render(self) -> str:
        """Render as a copy-pasteable ``saorsa-deploy`` command line."""
        parts = ["saorsa-deploy", *self.argv]
        return " ".join(_quote(p) for p in parts)


def _quote(token: str) -> str:
    if token and all(c not in token for c in " '\"\t"):
        return token
    return "'" + token.replace("'", "'\\''") + "'"


def node_binary_flags(b: Binary) -> list[str]:
    """Binary-source flags for node-side commands (genesis/bootstraps/nodes provision)."""
    if b.is_branch:
        return ["--branch-name", b.branch, "--repo-owner", b.repo_owner or "maidsafe"]
    return ["--node-version", b.version]


def ant_binary_flags(b: Binary) -> list[str]:
    """Binary-source flags for client-side commands (clients provision, prod kinds)."""
    if b.is_branch:
        return ["--branch-name", b.branch, "--repo-owner", b.repo_owner or "maidsafe"]
    return ["--ant-version", b.version]


def ant_build_steps(name: str, ssh_key: str, ant: Binary | None) -> list[Step]:
    """`build ant` step, only when the client binary is sourced from a branch."""
    if ant is None or not ant.is_branch:
        return []
    argv = [
        "build",
        "ant",
        "--name",
        name,
        "--branch-name",
        ant.branch,
        "--repo-owner",
        ant.repo_owner or "maidsafe",
        "--ssh-key-path",
        ssh_key,
    ]
    if ant.validation:
        argv += ["--validation-string", ant.validation]
    if ant.debug:
        argv.append("--debug")
    return [Step(argv, "Build ant client from branch")]


def build_steps_for_branches(
    name: str, ssh_key: str, ant_node: Binary, ant: Binary | None
) -> list[Step]:
    """`build ant-node` / `build ant` steps, only for binaries sourced from a branch."""
    steps: list[Step] = []
    if ant_node.is_branch:
        argv = [
            "build",
            "ant-node",
            "--name",
            name,
            "--branch-name",
            ant_node.branch,
            "--repo-owner",
            ant_node.repo_owner or "maidsafe",
            "--ssh-key-path",
            ssh_key,
        ]
        if ant_node.validation:
            argv += ["--validation-string", ant_node.validation]
        if ant_node.debug:
            argv.append("--debug")
        steps.append(Step(argv, "Build ant-node from branch"))
    steps += ant_build_steps(name, ssh_key, ant)
    return steps
