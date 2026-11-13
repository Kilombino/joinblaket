# Annotated XBT taker transcript (regtest)

Real CoinJoin run on 2026-11-13 in the regtest lab `/mnt/datos2/jm-043-regtest`
(Bitcoin Knots BLAKE2b v29.5, BLAKE2b active, JoinBlaket `v0.43.0-xbt4`): one directory,
one maker with a fidelity bond, one taker (`jm-taker coinjoin -a 30000000 -d INTERNAL -n 1`).
Compared with the lab's own script, nick authentication was switched on (directory
`prefer_verified` with id `127.0.0.1:15322`, taker `require_verified`) and a recording
proxy sat between taker and directory, so every frame the taker sent or received is below.
Result: CoinJoin `5f338701f48d8bab828ee012efe6a6c6dfbe492bfdc3b88ce880a77b9517b9f3`,
2 inputs (taker + maker), 4 outputs (2 x 30,000,000 sat), nLockTime 277 (the 1-in-10
"height minus random" branch), nSequence 0xfffffffe, both witnesses signed with 0x21.

Anonymization: nicks replaced by `<TAKER>`, `<MAKER>`, `<DIR>`, `<HP2NICK>`; pubkeys cut to
10 hex digits, 32-byte hex values to 12, base64 blobs to 20 characters plus their length.
Lengths are real. Each frame is one line on the TCP stream terminated by `\r\n`.
Scripts used: `/mnt/datos3/reconstruible/jm-transcript/{run.sh,proxy.py}` (not part of the
repository; the lab configuration was not modified, overrides were environment variables).

Timing: the taker spends ~35 s in the orderbook phase (5 s peerlist + 30 s minimum listen)
and ~3 s for the whole maker exchange on regtest.

## F1 (t = 0.00 s, taker->dir)

```
{"type": 793, "line": "{\"app-name\": \"joinmarket\", \"directory\": false, \"location-string\": \"NOT-SERVING-ONION\", \"proto-ver\": 5, \"features\": {\"neutrino_compat\": true, \"nick_auth\": true, \"peerlist_features\": true, \"ping\": true}, \"nick\": \"<TAKER>\", \"network\": \"xbt-regtest\"}"}
```

Taker HANDSHAKE (793). `line` is a JSON string; its exact bytes are hashed for nick auth. Network is `xbt-regtest` (would be `xbt` on mainnet). Location NOT-SERVING-ONION: the taker never listens. Features: `nick_auth` because a directory id is configured (env `NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS`), `neutrino_compat` because the descriptor-wallet backend can provide extended UTXO metadata.

## F2 (t = 0.00 s, dir->taker)

```
{"type": 795, "line": "{\"app-name\": \"joinmarket\", \"directory\": true, \"proto-ver-min\": 5, \"proto-ver-max\": 5, \"features\": {\"nick_auth\": true, \"peerlist_features\": true, \"ping\": true}, \"accepted\": true, \"nick\": \"<DIR>\", \"network\": \"xbt-regtest\", \"motd\": \"JoinMarket NG Directory Server https://github.com/joinmarket-ng/joinmarket-ng/\\nJoinMarket NG version: 0.43.0\"}"}
```

Directory reply DN_HANDSHAKE (795): `accepted`, same XBT network (checked by the taker), protocol 5..5, advertises `nick_auth`.

## F3 (t = 0.00 s, dir->taker)

```
{"type": 803, "line": "{\"challenge\":\"1a1b38b9ac07…\",\"directory-id\":\"127.0.0.1:15322\"}"}
```

NICK_AUTH_CHALLENGE (803): 32 random bytes hex + directory id. The taker checks the id equals its configured one (here `127.0.0.1:15322`; on mainnet the onion `host:port`).

## F4 (t = 0.00 s, taker->dir)

```
{"type": 805, "line": "{\"pubkey\":\"02c746d512…\",\"signature\":\"MEUCIQD5nkrJS2tBug50…[96 chars]\"}"}
```

NICK_AUTH_PROOF (805): nick pubkey + base64 DER signature over `nick-auth|challenge|directory-id|sha256(F1.line)|nick|pubkey` + `nick-auth-v1` (Bitcoin signed-message hash).

## F5 (t = 0.00 s, dir->taker)

```
{"type": 807, "line": "{\"code\":\"ok\",\"verified\":true}"}
```

NICK_AUTH_RESULT (807) ok. From here on the directory knows the nick is owned by that key.

## F6 (t = 0.01 s, taker->dir)

```
{"type": 791, "line": ""}
```

GETPEERLIST (791).

## F7 (t = 0.01 s, dir->taker)

```
{"type": 789, "line": "<MAKER>;NOT-SERVING-ONION;F:neutrino_compat+peerlist_features+ping,<TAKER>;NOT-SERVING-ONION;F:neutrino_compat+nick_auth+peerlist_features+ping"}
```

PEERLIST (789): `nick;location;F:features`, comma separated. Both peers are NOT-SERVING-ONION in this lab (maker without Tor), so no direct connection is attempted and everything below is relayed by the directory.

## F8 (t = 5.01 s, taker->dir)

```
{"type": 687, "line": "<TAKER>!PUBLIC!orderbook"}
```

`!orderbook` request, PUBMSG (687), unsigned. Sent ~5 s after F6 because the peerlist fetch waits 5 s for further chunks.

## F9 (t = 5.06 s, dir->taker)

```
{"type": 685, "line": "<MAKER>!<TAKER>!sw0reloffer 0 27300 39703276 0 0.001!tbond /zBFAiEAp3F9csJUIn62…[336 chars] 02a397f402… MEQCIDJD4faWgBrimhF5…[96 chars]"}
```

One PRIVMSG per offer: `sw0reloffer oid minsize maxsize txfee cjfee`, then `!tbond <252-byte proof, base64 336 chars>` bound to (taker nick, maker nick), then the maker's nick pubkey and signature over everything between `sw0reloffer ` and the pubkey (+ `onion-network`). The taker verified the bond on its node (unspent P2WSH freeze script, confirmed) before selecting. 0.001 is on the fee grid.

## F10 (t = 35.06 s, taker->dir)

```
{"type": 685, "line": "<TAKER>!<MAKER>!fill 0 30000000 a1fbf2e74bdf… Pac8d866c0c28… 02c746d512… MEUCIQDcs38rlHQjwGch…[96 chars]"}
```

`!fill oid amount taker_nacl_pubkey P<commitment>` (plaintext, signed). ~30 s after F8: the orderbook listen lasts at least `orderbook_min_wait` = 30 s.

## F11 (t = 35.09 s, dir->taker)

```
{"type": 685, "line": "<MAKER>!<TAKER>!pubkey 56a52b9927c1… features=neutrino_compat 02a397f402… MEQCIHj6KTUYUhlCHNXc…[96 chars]"}
```

`!pubkey maker_nacl_pubkey features=neutrino_compat` (plaintext, signed).

## F12 (t = 36.06 s, taker->dir)

```
{"type": 685, "line": "<TAKER>!<MAKER>!auth n9C+mb7xPys2N2G/1+fm…[560 chars] 02c746d512… MEQCIGVZ8i5VAsp/F4Db…[96 chars]"}
```

`!auth <NaCl box>`: 560 base64 chars = 420 bytes = 24 nonce + 16 MAC + 380 plaintext, i.e. the extended revelation `txid:vout:spk:height|P|P2|s|e` (the maker advertised neutrino_compat).

## F13 (t = 36.11 s, dir->taker)

```
{"type": 685, "line": "<MAKER>!<TAKER>!ioauth qW1d6fKz5qw8aBFtVRwU…[548 chars] 02a397f402… MEQCIAM6mmkeYJY0K75M…[96 chars]"}
```

`!ioauth <NaCl box>`: `utxo_list auth_pub cj_addr change_addr btc_sig` (extended utxo format since the taker sent extended). The taker verified btc_sig (auth key over the maker's NaCl pubkey), looked each UTXO up on its node, checked the change >= 27300 and that auth_pub owns one input.

## F14 (t = 36.17 s, dir->taker)

```
{"type": 687, "line": "<HP2NICK>!PUBLIC!hp2 ac8d866c0c28…"}
```

Broadcast by the maker side from a throw-away nick: `!hp2 <commitment>` = the commitment of F10, so other makers blacklist it. Takers ignore it.

## F15 (t = 36.17 s, dir->taker)

```
{"type": 789, "line": "<HP2NICK>;NOT-SERVING-ONION;D"}
```

Unsolicited PEERLIST: the throw-away nick left (`;D`).

## F16 (t = 37.08 s, taker->dir)

```
{"type": 685, "line": "<TAKER>!<MAKER>!tx vz59s+ezcyQujKiCBlGd…[440 chars] 02c746d512… MEUCIQDfeuwALdTPq4d0…[96 chars]"}
```

`!tx <NaCl box>`: plaintext is base64 of the 216-byte unsigned tx (2 inputs, 4 outputs, version 2, no witness).

## F17 (t = 37.10 s, dir->taker)

```
{"type": 685, "line": "<MAKER>!<TAKER>!sig BpjTU9ILAvMWnhahuHXO…[248 chars] 02a397f402… MEUCIQCP4tw8ltJdOYRA…[96 chars]"}
```

`!sig <NaCl box>`, one per maker input: plaintext base64 of `len||DER sig||0x21` + `len||pubkey` (~108 bytes). The taker matched it to the maker's input and verified it under the unified sighash (0x21 only).

## F18 (t = 38.10 s, taker->dir)

```
{"type": 685, "line": "<TAKER>!<MAKER>!push AgAAAAABAvgFH/Imy3Z8…[580 chars] 02c746d512… MEQCICOp4OB+NsjCQMQD…[96 chars]"}
```

`!push <base64 signed tx>` in plaintext (434 bytes; both witnesses end in 0x21). Policy was random-peer: the taker then saw txid `5f338701…b9f3` in its node's mempool within ~2 s and stopped. Mined in the next block.

## Not shown

* PING/PONG: none in this short session (the directory pings only peers idle for 10 min).
* `!error`: no maker error occurred; see `docs/xbt-taker-vectors.json` `session.error`.
* Direct (onion-to-onion) frames: none, both bots were NOT-SERVING-ONION. Over a direct
  connection the PRIVMSG frames are identical; the taker first sends its 793 handshake.
