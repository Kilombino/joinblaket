"""Container preflight: refuse to start unless the node is Bitcoin BLAKE2b (XBT).

Uses the same check as the wallet backend (``difficulty_blake2b`` in
getblockchaininfo and ``header_version >= 2`` at the tip), plus the node's
chain must match the configured network (mainnet -> "main").

Exit codes: 0 ok, 2 wrong node/chain (do not retry), 3 node unreachable.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from jmcore.settings import get_settings
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend, XBTChainError

EXPECTED_CHAIN = {"mainnet": "main", "testnet": "test", "signet": "signet", "regtest": "regtest"}


async def check_once(backend: DescriptorWalletBackend, network: str) -> str:
    await backend._verify_xbt_chain()  # raises XBTChainError on a non-BLAKE2b node
    info = await backend._rpc_call("getblockchaininfo", use_wallet=False)
    chain = info.get("chain")
    expected = EXPECTED_CHAIN.get(network)
    if chain != expected:
        raise XBTChainError(
            f"Node chain is {chain!r} but network is {network!r} (expected {expected!r})"
        )
    return f"chain={chain} height={info.get('blocks')} ibd={info.get('initialblockdownload')}"


async def main() -> int:
    settings = get_settings()
    network = settings.network_config.network.value
    btc = settings.bitcoin
    if btc.backend_type != "descriptor_wallet":
        print("preflight: only backend_type=descriptor_wallet is supported", file=sys.stderr)
        return 2
    deadline = time.monotonic() + float(os.environ.get("JM_XBT_PREFLIGHT_TIMEOUT", "600"))
    while True:
        backend = DescriptorWalletBackend(
            rpc_url=btc.rpc_url,
            rpc_user=btc.rpc_user,
            rpc_password=btc.rpc_password.get_secret_value(),
        )
        try:
            summary = await check_once(backend, network)
            print(f"preflight: XBT node OK ({summary})", flush=True)
            return 0
        except XBTChainError as exc:
            print(f"preflight: REFUSING TO START: {exc}", file=sys.stderr, flush=True)
            return 2
        except Exception as exc:  # node down, auth error, still starting
            if time.monotonic() > deadline:
                print(f"preflight: node not reachable at {btc.rpc_url}: {exc}", file=sys.stderr)
                return 3
            print(f"preflight: waiting for node at {btc.rpc_url} ({exc!r})", flush=True)
            await asyncio.sleep(10)
        finally:
            await backend.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
