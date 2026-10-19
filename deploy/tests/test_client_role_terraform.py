"""Every role Python knows is accepted by every client Terraform module.

The `role` variable is statically validated inside each provider's module, so a
role the Python half is complete for but a module's allow-list omits fails at
`terraform apply` — after the lane has printed the role back with its options,
which reads as a provider fault rather than as a missing value. That is exactly
how `browser-download` shipped in 1.4.0 and 1.4.1: the feature commit touched no
`resources/*-client/variables.tf`, and every `clients infra` carrying the role
died on all four providers.

`ALL_CLIENT_ROLES` is the authoritative enumeration and every other consumer
derives its list from it; the Terraform allow-list is the one that cannot, so it
is pinned here instead — in both directions, because a stale value left in a
module is as wrong as a missing one. This supersedes the pointer-only assertion
that used to live in `test_pointer_client_plumbing.py`.
"""

import re
from pathlib import Path

import pytest

from saorsa_deploy.client_roles import ALL_CLIENT_ROLES

RESOURCES = Path(__file__).parent.parent / "saorsa_deploy" / "resources"

PROVIDERS = ("digitalocean", "hetzner", "ovh", "vultr")

_CONDITION = re.compile(r"condition\s*=\s*contains\(\s*\[(?P<values>[^\]]*)\]\s*,\s*var\.role\s*\)")


def _role_block(provider: str) -> str:
    """The `variable "role"` block of one provider's module."""
    variables = (RESOURCES / f"{provider}-client" / "variables.tf").read_text()
    match = re.search(r'^variable "role" \{\n(?:.*\n)*?^\}\n', variables, flags=re.MULTILINE)
    assert match, f'{provider}-client/variables.tf has no `variable "role"` block'
    return match.group(0)


def _allowed_roles(provider: str) -> set[str]:
    block = _role_block(provider)
    match = _CONDITION.search(block)
    assert match, f"{provider}-client: no `contains([...], var.role)` condition"
    return set(re.findall(r'"([^"]+)"', match.group("values")))


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_module_allow_list_is_exactly_the_python_roles(provider):
    """Set equality, not containment: a role missing from the module fails every
    apply for that provider, and a value the module still accepts that Python has
    dropped would be a role nothing provisions."""
    assert _allowed_roles(provider) == set(ALL_CLIENT_ROLES)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_error_message_names_every_role(provider):
    """The `error_message` is the whole of what an operator sees when the apply
    refuses, so a list that disagrees with the condition sends them looking for a
    role that is in fact accepted."""
    block = _role_block(provider)
    message = re.search(r"error_message\s*=\s*\"(?P<text>[^\"]*)\"", block)
    assert message, f"{provider}-client: no `error_message` on the role validation"
    for role in ALL_CLIENT_ROLES:
        assert f"'{role}'" in message.group("text"), (provider, role)


def test_the_role_blocks_are_identical_across_the_providers():
    """Nothing in any module branches on the role beyond the VM's name, so the
    four blocks have no reason to differ — and when they drift, the role works on
    some providers and not others, which is the hardest shape to diagnose."""
    blocks = {provider: _role_block(provider) for provider in PROVIDERS}
    reference = blocks[PROVIDERS[0]]
    for provider, block in blocks.items():
        assert block == reference, provider
