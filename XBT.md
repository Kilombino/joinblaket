# JoinMarket-NG for Bitcoin BLAKE2b (XBT)

This branch (`xbt`) is a small, auditable patch on top of upstream
JoinMarket-NG **0.41.0** (tag `0.41.0`, commit
`66d9eea388e3f7a7ac0203cbcfcc5a44a91559e8`) that makes it safe to run
CoinJoins on the Bitcoin BLAKE2b chain ("XBT", the fork of Bitcoin at height
961640 whose node is Bitcoin Knots with BLAKE2b proof of work).

Upstream works against a Knots-BLAKE2b node unchanged, and that is the
problem: it would happily join the real-Bitcoin (SHA-256) JoinMarket, sign
replayable transactions and trust whatever node it is pointed at. This build
closes those holes and is **XBT-only**: it cannot be used on the SHA-256 chain.

## What changes and why

| Area | Upstream 0.41.0 | This build | Why |
|---|---|---|---|
| Wire network | Handshakes send `"network": "mainnet"` | Send and require `"xbt"` (`"xbt-regtest"`, `"xbt-testnet"`, `"xbt-signet"` on test networks). Directories, peers (directory-relayed and direct onion connections) and directory replies with any other value are refused. | The handshake network is the only thing separating markets. Without this an XBT wallet joins the Bitcoin market (and vice versa). |
| Default directories | 8 hardcoded mainnet onions, 3 signet | None. Maker and taker refuse to start: *"No directory servers configured… set directory_servers to XBT directories"*. | The defaults are real-Bitcoin directories. |
| Network identity | `network` (protocol) and `bitcoin_network` (addresses) may differ; `jm-wallet` derives from the former, maker/taker from the latter | They must be equal (checked in `WalletConfig` and CLI backend resolution), so every component derives the same keys. Addresses stay mainnet style: `bc1…`, `m/84'/0'/…`. | Consistent wallets across `jm-wallet`, `jm-maker`, `jm-taker`; XBT shares Bitcoin's address formats. |
| Chain check | None | Before its first RPC call the descriptor-wallet backend requires `difficulty_blake2b` in `getblockchaininfo` and `header_version >= 2` in `getblockheader(tip)`. Anything else raises `XBTChainError` on every call (fail closed). | Pointing the wallet at a SHA-256 node (or a BLAKE2b node before the fork) must not work. |
| Backends | `descriptor_wallet`, `neutrino` | `descriptor_wallet` only | Neutrino cannot check chain identity, cannot look up foreign inputs, and its fee sources are SHA-256 explorers. |
| Fee sources | Neutrino fee estimates from mempool.space / blockstream onions + clearnet | No default external fee sources. Fees come from the node's `estimatesmartfee`; when it has no estimate the 1 sat/vB fallback is logged explicitly. | SHA-256 mempool fees say nothing about the XBT mempool, and the requests leak. |
| Signatures | BIP-143 `SIGHASH_ALL` (0x01) | Every input this code signs (taker, maker, `jm-wallet send`, PSBT signing, hot fidelity-bond spends) uses the **unified sighash 0x21** (`SIGHASH_UNIFIED \| SIGHASH_ALL`). Verification of counterparties' signatures accepts **only** 0x21; a plain 0x01 signature is rejected, so no replayable input can enter the CoinJoin. | A 0x21 signature is valid only on the BLAKE2b chain, so the transaction cannot be replayed on the SHA-256 chain. |
| Maker input lookup | Looking up other participants' inputs is best effort (only for the fee-floor check) | The maker looks up the amount and scriptPubKey of **every** input (own wallet, then `gettxout` on its own node) and refuses to sign if any lookup fails. | The unified sighash commits to all inputs' amounts and scripts; signing over values not checked on our own node would be signing blind. |

### Unified sighash

Implemented in `jmwallet/src/jmwallet/wallet/signing.py`
(`unified_sighash_message`, `compute_sighash_unified`), ported from Knots'
`doc/unified-sighash.md` (v29.4.1) via Kilowallet's `TxBuilder.unifiedMessage`.
Script type 1 (segwit v0) is used for both P2WPKH (scriptCode = P2PKH script of
the key hash) and P2WSH (scriptCode = witness script; fidelity bonds). The
message commits to every input's outpoint, amount, scriptPubKey and sequence
and ends in the BIP-340 tagged hash `"UnifiedSighash"`.

It is verified:

* byte for byte against all 142 script-type-0/1 vectors of Knots'
  `unified_sighash.json` (`jmwallet/tests/test_unified_sighash.py`,
  `jmwallet/tests/data/unified_sighash_type01.tsv`);
* against a Knots-BLAKE2b regtest node (`scripts/xbt/regtest_unified_check.py`):
  a P2WPKH + P2WSH (CLTV freeze script) spend signed with 0x21 is accepted by
  `testmempoolaccept` and mined; the same signature with the byte changed to
  0x01, or a signature committing to a wrong amount for the *other* input, is
  rejected.

## Configuration for XBT

`~/.joinmarket-ng/config.toml` (or `$JOINMARKET_DATA_DIR/config.toml`):

```toml
[bitcoin]
backend_type = "descriptor_wallet"          # the only supported backend
rpc_url = "http://127.0.0.1:8332"           # your Bitcoin Knots BLAKE2b node
rpc_user = "youruser"
rpc_password = "yourpassword"

[network_config]
network = "mainnet"                         # = XBT; announces "xbt" on the wire
# bitcoin_network, if set, must also be "mainnet"
directory_servers = [                       # REQUIRED: XBT directories only
  "yourxbtdirectory56charsonionaddressxxxxxxxxxxxxxxxxxxxxx.onion:5222",
]

[tor]
socks_host = "127.0.0.1"
socks_port = 9050
# control_enabled = true                    # makers: Tor control for the onion service
# control_port = 9051

[taker]
# fee_rate = 2.0                            # optional: fixed sat/vB instead of estimatesmartfee
```

Running an XBT directory: `jm-directory-server` with `network = "mainnet"`
answers only `"xbt"` handshakes. There is no built-in list in the code; the
Docker image (`xbt-docker/`) defaults to the XBT directory
`f3tfzgk5ilwfapx427yx5ckeh7ptuvskfxnfnicfo747jyq5mgzdtvad.onion:5222`
(nick-auth directory id: the same `host:port`). The image also makes takers use only makers with a
fidelity bond: `bondless_makers_allowance = 0`, which in this build removes
every bondless offer before selection (no fallback; the CoinJoin fails with
"Not enough makers with a fidelity bond"), plus
`bondless_require_zero_fee = true` and `minimum_makers = 4`.

Regtest/local testing (no Tor) uses `network = "regtest"`,
`allow_clearnet_connections = true`, `directory_servers = ["127.0.0.1:PORT"]`
and `[tor] control_enabled = false`; see "How this was tested".

## Known limitations

* **Interoperability**: by design this build cannot talk to upstream
  JoinMarket-NG, JoinMarket-clientserver/JAM or joinmarket-rs, and they cannot
  talk to it. Both sides must run an XBT build.
* **Neutrino** is unsupported (refused at startup); tests for it are skipped.
* **Cold-storage fidelity bonds** are out of scope: `jm-wallet spend-bond`,
  `scripts/sign_bond_psbt.py`, `scripts/sign_bond_mnemonic.py` and
  `scripts/finalize_bond_psbt.py` still build/verify BIP-143 0x01 signatures for
  hardware wallets. Such a spend is replayable on the SHA-256 chain if the bond
  UTXO predates the fork. Hot-wallet bond spends through `sign_input` (and
  `jm-wallet sign-psbt`) use 0x21. A PSBT that requests sighash 0x01 is refused
  by `jm-wallet sign-psbt`; one with no sighash field is signed with 0x21.
* **jmwalletd + Jam** (web UI, Jam `v2.0.0-beta.4` with a small XBT patch)
  were exercised end to end on regtest: wallet creation, receive, a 4-party
  CoinJoin started from Jam (every input signed 0x21,
  `xbt-test-logs/jam-ui-coinjoin-verify.log`) and starting/stopping the maker.
  `jmwalletd --data-dir X` now reads `X/config.toml` (upstream read
  `~/.joinmarket-ng/config.toml`). The jmwalletd unit-test fixtures were
  adapted (equal networks, explicit test directory); the suite passes.
  With the image defaults (bonded makers only) a Jam CoinJoin against 4
  makers holding wallet-derived bonds (P2WSH, locktime 2026-11) succeeded
  with every input 0x21, the same against 4 bondless makers was refused
  (`xbt-test-logs/docker-jam-bonds-only.log`), and a bond created from Jam
  (`timelock/new` + direct-send) was recognized and advertised.
  The tumbler was not exercised. Packaging: `PACKAGING-XBT.md`, image in
  `xbt-docker/`, user tutorial (Spanish) in `TUTORIAL-LINUX-ES.md`.
* **Fees**: with no external sources, a node without fee history falls back to
  1 sat/vB (logged as a warning). Set `fee_rate` explicitly if that matters.
* The chain check identifies a *BLAKE2b node past the fork*; it does not tell
  XBT mainnet from another BLAKE2b network (the usual `network`/`chain` setup
  applies, and the wire identifiers differ per network).
* Upstream docs, README and help texts still describe Bitcoin defaults.

## Reproducible image

`xbt-docker/build.sh` builds `ghcr.io/kilombino/jam-xbt:0.41.0-xbt1` from
`git archive` of this repository and of the Jam fork (committed files only),
with every input pinned (base image digest, Debian snapshot, Node.js sha256,
`pip --require-hashes`, `npm ci` integrity), `SOURCE_DATE_EPOCH` = the time
of the joinmarket-ng commit, file times clamped (`rewrite-timestamp=true`)
and no provenance/SBOM attestations. Two builds of the same commits give the
same digest:

```sh
cd joinmarket-ng                      # branch xbt; ../jam-xbt on branch xbt
OCI=/tmp/a.tar NO_CACHE=1 xbt-docker/build.sh ../jam-xbt
OCI=/tmp/b.tar NO_CACHE=1 xbt-docker/build.sh ../jam-xbt
# both end with the same "image digest: sha256:…"
# both architectures at once (needs QEMU/binfmt for arm64), as published:
PLATFORM=linux/amd64,linux/arm64 OCI=/tmp/multi.tar xbt-docker/build.sh ../jam-xbt
# compare with the registry (the digest of the tag's index):
docker buildx imagetools inspect ghcr.io/kilombino/jam-xbt:0.41.0-xbt1
```

Without `OCI` the image is loaded into Docker and `build.sh` prints its id
(the config digest); compare it with
`docker image inspect --format '{{.Id}}'` of the pulled image for the same
platform.

The container runs on an ordinary bridge network: only jmwalletd (28183,
API + Jam) listens on the container interface; Tor (SOCKS 28185, control
28186), the orderbook watcher (28184) and the maker's onion listener stay on
127.0.0.1 inside it. Publish `-p 127.0.0.1:28183:28183` and never use
`--network host`. Nick authentication defaults to `require_verified`.

## Verifying the build

```sh
git clone https://github.com/joinmarket-ng/joinmarket-ng.git && cd joinmarket-ng
git -c advice.detachedHead=false checkout 0.41.0
git rev-parse HEAD   # must print 66d9eea388e3f7a7ac0203cbcfcc5a44a91559e8
# (optionally verify the upstream release signatures as in install.sh)
git diff 0.41.0..xbt # in this repository: the complete XBT patch
git log --oneline 0.41.0..xbt
```

The patch only touches the files listed by `git diff --stat 0.41.0..xbt`;
everything else is upstream byte for byte.

### How this was tested

* Unit tests: `pytest jmcore directory_server maker taker jmwallet -m "not docker"`
  (Python 3.12, dependencies installed with `pip --require-hashes` from the
  components' `requirements.txt`).
* Regtest end to end against Bitcoin Knots BLAKE2b (v29.4.2.knots20260508,
  BLAKE2b active): one `jm-directory-server`, three `jm-maker` yield
  generators and one `jm-taker coinjoin` (4 parties, 0.5 regtest coins each),
  all native, without Tor, each with a fresh wallet. The resulting transaction
  was mined; `scripts/xbt/verify_coinjoin_tx.py` checks that every input's
  witness signature ends in 0x21, the equal-amount outputs, and confirmation.
  A live handshake probe confirmed the directory refuses `"mainnet"`,
  `"regtest"` and `"xbt"` (wrong network) and accepts only `"xbt-regtest"`.
