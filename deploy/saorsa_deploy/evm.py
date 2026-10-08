"""EVM network constants sourced from evmlib.

Values mirror the public constants in evmlib (`Network::ArbitrumOne`,
`Network::ArbitrumSepoliaTest`) so that a devnet-manifest.json generated here
can be consumed by ant-node without further configuration.

Addresses are written in lowercase to match the `format!("{:?}", addr)`
representation used by ant-devnet when it emits its own manifest.
"""

ARBITRUM_SEPOLIA = {
    "rpc_url": "https://sepolia-rollup.arbitrum.io/rpc",
    "payment_token_address": "0x4bc1ace0e66170375462cb4e6af42ad4d5ec689c",
    "payment_vault_address": "0x7f0842a78f7d4085d975ba91d630d680f91b1295",
}

ARBITRUM_ONE = {
    "rpc_url": "https://arb1.arbitrum.io/rpc",
    "payment_token_address": "0xa78d8321b20c4ef90ecd72f2588aa985a4bdb684",
    "payment_vault_address": "0xb1b5219f8aaa18037a2506626dd0406a46f70bcc",
}

_NETWORKS = {
    "arbitrum-sepolia": ARBITRUM_SEPOLIA,
    "arbitrum-one": ARBITRUM_ONE,
}


def get_evm_network_info(name: str) -> dict:
    """Return the EVM network constants dict for ``name``.

    Raises ValueError if the name is not a known network.
    """
    try:
        return dict(_NETWORKS[name])
    except KeyError:
        known = ", ".join(sorted(_NETWORKS))
        raise ValueError(f"Unknown EVM network '{name}'. Known networks: {known}")
