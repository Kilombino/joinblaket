<p align="center">
  <img src="media/joinblaket-logo.jpg" alt="JoinBlaket" width="260"/>
</p>

<h1 align="center">JoinBlaket</h1>

<p align="center"><strong>CoinJoin on Bitcoin (BLAKE2b)</strong> · <code>0.43.0-xbt2</code> · with the <a href="https://github.com/Kilombino/jam">Jam</a> web wallet</p>

JoinBlaket is JoinMarket-NG adapted to the Bitcoin (BLAKE2b) chain: a small, auditable patch on top of upstream (branch `xbt`). It only works on BLAKE2b and refuses to start against a node on the SHA-256 chain (Spamchain).

- **Network `"xbt"`** on the wire: SHA-256 (`"mainnet"`) directories and peers are rejected.
- **Unified sighash `0x21`** on every signature, and only `0x21` accepted from others: nothing you sign can be replayed on the SHA-256 chain.
- **Node check:** requires `difficulty_blake2b` and v2 headers before the first call (fails closed).
- **One BLAKE2b directory**, `f3tfzgk5ilwfapx427yx5ckeh7ptuvskfxnfnicfo747jyq5mgzdtvad.onion:5222`, with verified nick authentication. None built into the code.
- **Fees** only from your node's `estimatesmartfee`. **Makers** only with a fidelity bond, at least 4. **Backend** `descriptor_wallet` only (no Neutrino).

Install: StartOS (registry `https://registry.kilombino.com`), Umbrel (store `https://github.com/Kilombino/umbrel-store`), Docker image `ghcr.io/kilombino/jam-xbt:0.43.0-xbt2`, or native. Guide (Spanish): **[kilombino.com/jam](https://kilombino.com/jam/)** · details in [XBT.md](XBT.md) · tutorials [Linux](TUTORIAL-LINUX-ES.md), [StartOS](TUTORIAL-STARTOS-ES.md), [Umbrel](TUTORIAL-UMBREL-ES.md).

The code, the packages and the image still use the names `joinmarket-ng` and `jam-xbt`. Use small amounts while the market is young.

Based on [joinmarket-ng/joinmarket-ng](https://github.com/joinmarket-ng/joinmarket-ng); its documentation is in [docs/](docs/). Licensed under MIT (see [LICENSE](LICENSE)).
