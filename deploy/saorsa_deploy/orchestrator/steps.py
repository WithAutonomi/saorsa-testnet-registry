"""Step primitives shared by the per-kind orchestrators.

A :class:`Step` is one primitive ``saorsa-deploy`` invocation expressed as an
argv list (no ``saorsa-deploy`` prefix). The orchestrator turns a manifest into
an ordered ``list[Step]``; ``plan`` prints them and ``up``/``down`` execute them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from saorsa_deploy.config.models import Binary


@dataclass(frozen=True)
class Step:
    argv: list[str]  # e.g. ["bootstraps", "infra", "--name", "STG-01", ...]
    label: str  # human-readable description for `plan` output
    best_effort: bool = False  # if True, a non-zero exit is warned about, not fatal
    # Parallel-group markers. Steps sharing the same ``group`` (and appearing
    # contiguously in the plan) are collapsed by :func:`iter_units` into one
    # :class:`StepGroup` that executes its lanes concurrently. Within a group,
    # steps sharing the same ``lane`` run sequentially (e.g. bootstraps→nodes,
    # which share state); different lanes run in parallel. Both default to None,
    # so an unmarked step is its own sequential unit and the byte-for-byte argv
    # output of the builders (and the journal contract) is unchanged.
    group: str | None = None
    lane: str | None = None

    def render(self) -> str:
        """Render as a copy-pasteable ``saorsa-deploy`` command line."""
        parts = ["saorsa-deploy", *self.argv]
        return " ".join(_quote(p) for p in parts)


@dataclass(frozen=True)
class StepGroup:
    """A contiguous run of plan steps that execute concurrently as one unit.

    ``lanes`` is a list of step-lanes; the steps inside a lane run sequentially,
    and the lanes themselves run in parallel. The whole group is treated as a
    single resumable unit in the step journal: it is recorded as done only once
    every lane has succeeded, so a resumed run re-executes the entire group
    (each underlying command is idempotent) rather than half of it.
    """

    name: str
    lanes: list[list[Step]]
    label: str

    @property
    def steps(self) -> list[Step]:
        return [s for lane in self.lanes for s in lane]

    def render(self) -> str:
        """One-line summary of the parallel commands, for divergence output."""
        return "  |  ".join(
            " && ".join(s.render() for s in lane) for lane in self.lanes
        )


def _group_label(group_id: str) -> str:
    if group_id == "infra":
        return "Create infrastructure (parallel)"
    if group_id == "build":
        return "Build binaries (parallel)"
    return f"{group_id} (parallel)"


def iter_units(steps: list[Step]) -> list[Step | StepGroup]:
    """Collapse a flat plan into execution units.

    A run of consecutive steps carrying the same non-None ``group`` id becomes a
    single :class:`StepGroup`; everything else stays a bare :class:`Step`. So an
    ungrouped plan yields one unit per step (units 1:1 with steps), which keeps
    the journal/resume index space and all existing behaviour unchanged.
    """
    units: list[Step | StepGroup] = []
    i = 0
    n = len(steps)
    while i < n:
        step = steps[i]
        if step.group is None:
            units.append(step)
            i += 1
            continue
        gid = step.group
        run: list[Step] = []
        while i < n and steps[i].group == gid:
            run.append(steps[i])
            i += 1
        lane_order: list[object] = []
        lane_map: dict[object, list[Step]] = {}
        for idx, s in enumerate(run):
            # A step with no explicit lane gets its own lane (keyed uniquely so
            # it never merges with another); explicitly-laned steps merge by name.
            key: object = s.lane if s.lane is not None else (idx, id(s))
            if key not in lane_map:
                lane_map[key] = []
                lane_order.append(key)
            lane_map[key].append(s)
        units.append(
            StepGroup(name=gid, lanes=[lane_map[k] for k in lane_order], label=_group_label(gid))
        )
    return units


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
    # The two branch builds are independent (~5 min each), so when both are
    # needed run them concurrently: tag each as its own lane of a single
    # ``build`` parallel group (collapsed by :func:`iter_units`, executed by
    # ``parallel_infra.run_parallel_group`` with a live lane table). A lone
    # build (or none) is left as a plain sequential step so prod kinds and
    # single-binary testnets keep their byte-for-byte argv and journal contract.
    if len(steps) == 2:
        steps = [replace(s, group="build", lane=s.argv[1]) for s in steps]
    return steps
