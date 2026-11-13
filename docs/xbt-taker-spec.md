# JoinBlaket (XBT) taker protocol: implementation spec

Status: describes `v0.43.0-xbt4` (branch `xbt2-work`, commit `f071a376`) as the code
behaves, for a byte-compatible reimplementation (Kilowallet, Kotlin). Every rule cites
`file:line` in this repository. Where the code is ambiguous or surprising this is said
explicitly ("**Note**").

Companion files:

* `docs/xbt-taker-vectors.json`: deterministic vectors produced by the Python code
  (`scripts/xbt/taker_vectors.py`; checked by `tests/test_xbt_taker_vectors.py`).
* `docs/xbt-taker-transcript.md`: an annotated wire transcript of a real regtest CoinJoin
  (1 bonded maker, nick authentication on).

Abbreviations: `jmcore/` = `jmcore/src/jmcore/`, `taker/` = `taker/src/taker/`,
`maker/` = `maker/src/maker/`, `jmwallet/` = `jmwallet/src/jmwallet/`,
`dirsrv/` = `directory_server/src/directory_server/`.

Scope: a taker with a full set of P2WPKH (BIP84, `bc1q`) UTXOs that only uses makers
with a fidelity bond (`bondless_makers_allowance = 0`, as the Docker/Jam image ships),
never offers an onion service itself, and never spends fidelity bonds in a CoinJoin.

---

## 0. What the XBT fork changes (summary)

| Rule | Where |
|---|---|
| Handshake `network` is `"xbt"` on BTC-BLAKE2b mainnet (`"xbt-testnet"`, `"xbt-signet"`, `"xbt-regtest"`). Addresses stay mainnet (`bc1`, `m/84'/0'`). | `jmcore/models.py:110-130`, sent at `jmcore/protocol.py:562` |
| A directory answering any other network is refused. | `jmcore/directory_client.py:651-658` |
| Direct (onion) peers whose reciprocal handshake carries another network are refused. | `jmcore/network.py:691-697` |
| No default directories; the directory list must be configured. | `jmcore/models.py:48-70`, `taker/config_builder.py:160-161` |
| Every signature the taker makes uses the unified sighash `0x21`; maker signatures are accepted only with `0x21`. | `jmwallet/wallet/signing.py:192-213`, `taker/coinjoin_session.py:2003-2010,2101-2103,2291` |
| `bondless_makers_allowance = 0` removes every offer without a verified bond before selection (no bondless fallback). | `taker/orderbook.py:624-641,851` |
| `minimum_makers` defaults to 1; asking for N makers goes on with fewer offers down to `minimum_makers`. | `taker/config.py:266-276`, `taker/taker.py:1275-1294` |
| Only the `descriptor_wallet` (Bitcoin Knots BLAKE2b RPC) backend; it refuses non-BLAKE2b nodes. | `XBT.md` "What changes and why" |

Everything else is upstream JoinMarket-NG 0.43.0 (protocol v5, compatible in format with
the reference `joinmarket-clientserver` onion message channel).

---

## 1. Transport

### 1.1 Connection

* The taker connects **outbound only**. Directory endpoints are `host:port` strings from
  config (`network_config.directory_servers`). For `.onion` hosts it opens a SOCKS5
  connection through Tor with remote DNS (`rdns=True`), `jmcore/network.py:168-237`,
  called from `jmcore/directory_client.py:557-569`. Non-onion endpoints are refused
  except on regtest or with `allow_clearnet_connections`, `jmcore/directory_client.py:537-556`.
* SOCKS5 username/password may be set for Tor stream isolation (`stream_isolation`,
  default true, `jmcore/config.py:310-313`); directories and peers get different
  credentials (`jmcore/directory_pool.py:140-152`). Purely a Tor circuit-separation
  device, the values themselves are not part of the protocol.
* Connect timeout: `connection_timeout`, default 120 s (`jmcore/config.py:314-320`); it
  covers SOCKS negotiation and circuit building.
* The taker announces `location-string = "NOT-SERVING-ONION"`
  (`taker/multi_directory.py:123`, `jmcore/directory_client.py:251`). It never needs to
  accept connections. Makers always answer through directories anyway
  (`maker/maker_session.py:788-803`, `maker/protocol_handlers.py:1519-1531`).

### 1.2 Framing

* A stream of UTF-8 lines. Sender appends `\r\n` (`jmcore/network.py:71`); receiver reads
  up to `\n` and strips trailing `\r\n` (`jmcore/network.py:89-90`).
* Each line is one JSON object `{"type": <int>, "line": <string>}`
  (`jmcore/directory_client.py:627-632,1634-1637`, `jmcore/models.py:184-188`). Python
  serializes with `json.dumps` default separators (`", "`, `": "`); peers parse JSON, so
  whitespace does not matter **except** that the nick-auth hash covers the exact
  `line` string of the HANDSHAKE (section 2.3).
* Size limits: client stream reader limit 2 MiB (`jmcore/network.py:52`); the directory
  rejects lines longer than `max_line_length` = 64 KiB (`jmcore/models.py:213`,
  `jmcore/settings.py:1398-1402`) and JSON nested deeper than 10. Makers refuse an
  unsigned tx larger than 1,000,000 bytes (`maker/maker_session.py:55-56`). A `!tx` line
  is ~ `16/9 * tx_size` + 200 bytes, so the 64 KiB directory limit caps the unsigned
  transaction at about 36 KB (far above `max_maker_utxos` x makers in practice).
* There is **no chunking** of JoinMarket messages on the onion channel (the IRC-era
  `;`/`~` chunking does not exist here). Only PEERLIST replies are split into several
  frames of up to 20 entries (`dirsrv/message_router.py:479-552`).

### 1.3 Message types (`jmcore/protocol.py:484-502`)

| type | name | direction (taker view) | `line` |
|---|---|---|---|
| 685 | PRIVMSG | both | `<from_nick>!<to_nick>!<command> <args...> <pubkey_hex> <sig_b64>` |
| 687 | PUBMSG | both | `<from_nick>!PUBLIC!<command> <args...>` (unsigned) |
| 789 | PEERLIST | in | comma-separated `nick;location[;D][;F:feat1+feat2]` |
| 791 | GETPEERLIST | out | `""` |
| 793 | HANDSHAKE | out (also to/from direct peers) | JSON string |
| 795 | DN_HANDSHAKE | in (directory reply) | JSON string |
| 798 / 799 | PING / PONG | in / out | `""` |
| 803 / 805 / 807 | NICK_AUTH_CHALLENGE / PROOF / RESULT | in / out / in | compact JSON string |

`801` DISCONNECT exists but must not be sent (`jmcore/directory_client.py:1655-1656`).

### 1.4 Directory handshake

Taker sends (`jmcore/directory_client.py:582-635`, payload built at
`jmcore/protocol.py:525-563`):

```json
{"type": 793, "line": "{\"app-name\": \"joinmarket\", \"directory\": false,
 \"location-string\": \"NOT-SERVING-ONION\", \"proto-ver\": 5,
 \"features\": {\"neutrino_compat\": true, \"nick_auth\": true,
 \"peerlist_features\": true, \"ping\": true}, \"nick\": \"J5...\", \"network\": \"xbt\"}"}
```

* Key order: `app-name, directory, location-string, proto-ver, features, nick, network`.
  `features` is a dict with sorted keys, only `true` values (`jmcore/protocol.py:224-227`).
* Feature set: always `peerlist_features` and `ping` (`jmcore/directory_client.py:607`);
  `neutrino_compat` because the descriptor-wallet backend "can provide" extended UTXO
  metadata (`taker/taker.py:209,222`, `jmwallet/backends/base.py:545`); `nick_auth` when
  the mode is not `disabled` **and** an expected directory id is configured for this
  endpoint (`jmcore/directory_client.py:610-614`).

Directory replies with type 795 whose `line` is
`create_handshake_response(...)` (`jmcore/protocol.py:566-605`):
`app-name, directory:true, proto-ver-min, proto-ver-max, features, accepted, nick,
network, motd`. Taker checks, in order (`jmcore/directory_client.py:637-701`): type is 793
or 795; `accepted` true; `network == "xbt"` (XBT); `proto-ver-min <= 5 <= proto-ver-max`
(or single `proto-ver`); if `require_verified` the directory must advertise `nick_auth`;
then runs nick authentication when both sides advertise it (section 2.3).

Timeouts: reply wait = `connection_timeout` (`jmcore/directory_client.py:638`). The
directory itself drops a connection that sends no handshake in 30 s
(`dirsrv/server.py:283`).

### 1.5 Keepalive and peerlist

* The directory PINGs ping-capable peers idle for 600 s and evicts those that do not PONG
  within 30 s; anything silent for 1500 s is evicted (`dirsrv/heartbeat.py:1-55`). The
  taker answers every PING with `{"type": 799, "line": ""}` (`jmcore/directory_client.py:1322-1325,1662-1671`).
* GETPEERLIST `{"type": 791, "line": ""}` (`jmcore/directory_client.py:913`), at most once
  per 60 s (`jmcore/directory_client.py:357,887`). Replies: one or more PEERLIST frames;
  the response is complete after 5 s without a new chunk (`jmcore/directory_client.py:359-361,926`).
  Entry grammar: `jmcore/protocol.py:697-731`. The directory also pushes unsolicited
  PEERLIST frames: a peer's location when it relays that peer's privmsg
  (`dirsrv/message_router.py:555-578`) and `nick;location;D` when a peer leaves.
* Peerlist is used for (a) the peer's onion location for optional direct connections and
  (b) per-peer features. A mobile taker that never connects directly can ignore it except
  for nick presence.

### 1.6 Routing taker <-> maker

* Default path: PRIVMSG via the directory. The directory relays a PRIVMSG only if
  `from_nick` equals the nick the sender handshaked with (`dirsrv/message_router.py:407-412`)
  and the target is connected on the same network; PUBMSG likewise (`dirsrv/message_router.py:140-144`).
  So `from_nick` in a relayed frame is directory-attested; the signature (section 2.2)
  additionally binds the content to the nick key.
* Optional direct path: the taker may open an outbound onion connection to a maker whose
  peerlist location is an onion (`taker/multi_directory.py:245-296,501-547`) and send
  the same PRIVMSG frames over it (`jmcore/network.py:785-818`). The peer handshake is a
  793 frame from us; a reciprocal 793 reply within `min(timeout, 2 s)` is checked
  (app-name, proto-ver 5, not a directory, network `xbt`), silence is accepted
  (`jmcore/network.py:629-707`). **Note:** with a silent peer no network check happens on
  the direct channel; this is upstream behaviour and the maker was found via an XBT
  directory anyway.
* Channel choice per maker is pinned at `!fill` (`taker/multi_directory.py:310-369`,
  `taker/coinjoin_session.py:520-537`) and may only be upgraded directory -> direct for
  later messages (`taker/multi_directory.py:371-409`). Makers accept channel switches
  (`maker/coinjoin.py:156-199`). A mobile taker can send everything through one
  directory; this is fully supported.
* With several directories: a privmsg goes to exactly one directory
  (`taker/multi_directory.py:750-795`); responses may arrive several times (maker fans out
  to all its directories) and are de-duplicated (`taker/multi_directory.py:902-944`).
* Response collection (`taker/multi_directory.py:797-1031`): only PRIVMSG frames, with
  `to_nick == our nick`, `from_nick` among the expected makers and a valid signature
  (section 2.2) are considered. A `!error` from a maker ends that maker's wait (first
  response wins). `!sig` responses accumulate until each maker sent as many as it has
  inputs (`taker/coinjoin_session.py:1971-1985`).
* Per-phase timeout: `maker_timeout_sec`, default 60 s (`taker/config.py:205-207`).

---

## 2. Nick identity, signatures, nick authentication

### 2.1 Nick

* One random secp256k1 key per taker process (`taker/taker.py:198`). Python draws it as
  `sha256(16 random bytes)` (`jmcore/crypto.py:286-288`); any uniformly random valid
  scalar is equivalent. A mobile taker should use a fresh key per CoinJoin (privacy).
* `nick = "J5" + base58(sha256(hex(compressed_pubkey).encode("ascii"))[:10])`, padded on
  the right with `"O"` to 14 characters after `J5` (16 total). Note the hash input is the
  **ASCII hex string** of the 33-byte pubkey, not the bytes (`jmcore/crypto.py:295-302`,
  `jmcore/crypto.py:389-395`). Base58 is plain (no checksum), Bitcoin alphabet
  (`jmcore/crypto.py:80-86`). Valid nick regex: `J[0-9][1-9A-HJ-NP-Za-km-zO]{14}`
  (`jmcore/protocol.py:78`). Vector: `nick`.

### 2.2 Privmsg signature

* Taker sends `"<from>!<to>!<command> <data> <pubkey_hex> <sig_b64>"`
  (`jmcore/directory_client.py:1600-1639`).
* `sig_b64 = base64(DER(ECDSA_sign(nick_key, H)))` with
  `H = sha256d("\x18Bitcoin Signed Message:\n" || compactsize(len(m)) || m)`,
  `m = utf8(data + "onion-network")` (`jmcore/crypto.py:160-186,314-337`,
  `jmcore/network.py:22`). The command is **not** signed; the hostid is always the fixed
  string `onion-network`, whichever directory or direct channel carries it
  (`jmcore/directory_client.py:1619-1626`).
* Signing: libsecp256k1 RFC 6979 deterministic nonce **without** low-R grinding
  (`_ecdsa_sig_grind_low_r=False`), low-S, DER. Vectors reproduce byte for byte only with
  the same nonce rule (`jmcore/crypto.py:331-332`).
* Verifying a received privmsg (`jmcore/crypto.py:398-422`): split `rest` on single
  spaces; `command = tokens[0]`, `pubkey = tokens[-2]`, `sig = tokens[-1]`,
  `data = " ".join(tokens[1:-2])`; require `nick_from_pubkey(pubkey) == from_nick` and a
  strict-DER, low-S, valid signature over `data + "onion-network"`
  (`jmcore/crypto.py:35-77,242-263`).
* Vector: `nick` (signature over a sample `!fill` data string, the full line, and the
  verification result).

### 2.3 Nick authentication (JMP-0005)

Negotiated when the client advertises `nick_auth` and the directory advertises it
(`jmcore/directory_client.py:689-697`, `dirsrv/handshake_handler.py:61-67`). Exchange,
right after the 795 reply (`jmcore/directory_client.py:715-785`, `dirsrv/server.py:421-486`):

1. Directory -> `{"type": 803, "line": "{\"challenge\":\"<64 lowercase hex>\",\"directory-id\":\"<id>\"}"}`.
   The taker requires `directory-id` to equal the id configured for this endpoint.
2. Taker -> `{"type": 805, "line": "{\"pubkey\":\"<33-byte hex, lowercase>\",\"signature\":\"<base64 DER>\"}"}`.
   Signed message bytes (`jmcore/nick_auth.py:247-262`):
   `ascii("nick-auth|<challenge>|<directory-id>|<sha256_hex(handshake_line)>|<nick>|<pubkey_hex>") || b"nick-auth-v1"`,
   hashed with the Bitcoin signed-message hash over **bytes** (`jmcore/crypto.py:189-218`),
   signed by the nick key (`jmcore/crypto.py:339-343`, `jmcore/nick_auth.py:264-283`).
   `handshake_line` is the exact `line` string the taker put in its 793 frame
   (`jmcore/nick_auth.py:222-225`, `jmcore/directory_client.py:626,698`). The proof JSON is
   compact (`separators=(",", ":")`, `jmcore/nick_auth.py:168-169`); the directory parses it
   strictly (no duplicate keys, no extra fields, canonical base64, strict DER, low-S,
   `jmcore/nick_auth.py:47-61,131-142,286-317`).
3. Directory -> `{"type": 807, "line": "{\"code\":\"ok\",\"verified\":true}"}`; any other
   code (`malformed`, `expired`, `invalid`, `policy`) means the directory closes.
   Timeout per step: `min(connection_timeout, 30 s)` (`jmcore/directory_client.py:723`).

Directory identity: there is no automatic derivation in the client; it is looked up in
`nick_auth_directory_ids["host:port"]` (`jmcore/directory_pool.py:149`). Convention for an
onion directory: the id is the lowercase `host:port` itself (`jmcore/nick_auth.py:228-244`
implements that rule but is not called by the client). The Jam/Docker image sets
`require_verified` with id `f3tfzgk5...tvad.onion:5222` for that directory
(`xbt-docker/Dockerfile:157-158`). **Note:** the production directory runs the server
default `prefer_verified` (it accepts unauthenticated takers but authenticates those that
offer `nick_auth`; `dirsrv/handshake_handler.py:149-152`); `require_verified` is a
client-side policy. Recommended for Kilowallet: implement it and require it.

Vector: `handshake` (frames sent/received with a scripted directory, the signed message,
its hash and the proof; `authenticated: true` comes from the real `DirectoryClient`).

---

## 3. Orderbook

### 3.1 Request and responses

* GETPEERLIST first (to learn features), then PUBMSG
  `{"type": 687, "line": "<nick>!PUBLIC!orderbook"}` (`jmcore/directory_client.py:1386-1452`).
* Listen adaptively: at least `orderbook_min_wait` (30 s), stop after
  `orderbook_quiet_period` (15 s) without a new offer, never more than `order_wait_time`
  (120 s) (`jmcore/directory_client.py:1407-1492`, `taker/config.py:217-245`).
* Each maker answers with one PRIVMSG per offer (`maker/protocol_handlers.py:323-410`):

  `<maker>!<taker>!sw0reloffer <oid> <minsize> <maxsize> <txfee> <cjfee>!tbond <proof_b64> <pubkey_hex> <sig_b64>`

  (`sw0absoffer` for absolute fees). The data signed is everything between the command
  and the pubkey, i.e. including `!tbond <proof>`. Bond proofs are only ever sent by
  privmsg because they are bound to the taker's nick (`maker/protocol_handlers.py:384-395`).
* Parsing (`jmcore/directory_client.py:1994-2107`): accept `sw0absoffer`, `sw0reloffer`
  (and legacy `swabsoffer`/`swreloffer`, filtered out later by type,
  `taker/orderbook.py:257-258`); split on `!`; first part `type oid minsize maxsize txfee
  cjfee`; flags `neutrino` and `tbond <proof> ...` (only the first token after `tbond` is
  the proof). Absolute `cjfee` -> `str(int(x))`; relative -> validated Decimal (non-negative,
  <= 32 digits, exponent in [-32, 32]) formatted fixed-point (`jmcore/directory_client.py:216-230`).
  For a PRIVMSG the bond proof is verified with `taker_nick = to_nick` (us); for a PUBMSG
  with `taker_nick = maker_nick` (`jmcore/directory_client.py:2045-2053`).
* **Note:** the orderbook path does not verify the privmsg signature of offers; it relies
  on the directory's nick binding (1.6). For a bonded-only taker the bond proof's nick
  signature binds the offer to `(taker_nick, maker_nick)` anyway. Verifying the privmsg
  signature in Kotlin is harmless and recommended.
* Across directories offers are keyed by `(counterparty, oid)`; the copy with the larger
  `cert_expiry` wins (`taker/multi_directory.py:629-660`).
* Offer fields and limits: `jmcore/models.py:325-340` (`minsize`, `maxsize`, `txfee` in
  `[0, 21e14]`).

### 3.2 Fidelity bond proof (252 bytes, base64)

Layout `struct "<72s72s33sH33s32sII"` (`jmcore/crypto.py:532-635`,
producer `maker/fidelity.py:345-479`):

| offset | size | field |
|---|---|---|
| 0 | 72 | nick_sig: DER, left-padded with `0xff` to 72 |
| 72 | 72 | cert_sig: DER, left-padded with `0xff` |
| 144 | 33 | cert_pub |
| 177 | 2 | cert_expiry (uint16 LE), in 2016-block periods |
| 179 | 33 | utxo_pub |
| 212 | 32 | txid in **display (big-endian) order** (`maker/fidelity.py:454-458`). **Note:** the inline comment at `jmcore/crypto.py:587` says "little-endian"; the code treats it as display hex. |
| 244 | 4 | vout LE |
| 248 | 4 | locktime LE (Unix time) |

Verification (`jmcore/crypto.py:568-635`):

1. Strip padding: take each signature from its first `0x30` byte.
2. `nick_sig` verifies with `cert_pub` over the Bitcoin signed-message hash of
   `ascii(taker_nick + "|" + maker_nick)`.
3. `cert_sig` verifies with `utxo_pub` over the signed-message hash of either
   `b"fidelity-bond-cert|" + cert_pub(33 raw bytes) + b"|" + ascii(str(cert_expiry))` or the
   same with `hex(cert_pub)` (`jmcore/crypto.py:471-507,615-621`).
   (Here the strict-DER/low-S checks of `verify_strict_ecdsa` apply after stripping,
   `jmcore/crypto.py:448-468`.)
4. Expiry height = `cert_expiry * 2016`. Makers set it to
   `((height + 2) // 2016) + 1` (`maker/fidelity.py:24-26,434-436`).

Vector: `offer` (deterministic proof with pinned cert key, every field, the full privmsg
line, parse result, verification with the right and a wrong taker nick).

### 3.3 Bond verification against the chain and value

`taker/taker.py:817-961`, backend `jmwallet/backends/base.py:596-683`:

1. Skip bonds whose certificate expired: `current_height > cert_expiry*2016` (`taker/taker.py:866-871`).
2. Expected script: `witness_script = <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP <utxo_pub> OP_CHECKSIG`
   (minimal CScriptNum push of the locktime, `jmcore/btc_script.py:22-41`);
   `scriptPubKey = OP_0 <sha256(witness_script)>` (`jmcore/btc_script.py:136-147,162-204`).
3. Look up `txid:vout`: must exist unspent (`gettxout`, mempool included), have
   `confirmations > 0` and exactly that scriptPubKey.
4. `confirmation_time` = header `time` of block `current_height - confirmations + 1`
   (`jmwallet/backends/base.py:659-661`, `jmwallet/backends/descriptor_wallet.py:2271-2275`).
5. Value (`jmcore/bond_calc.py:14-52`), with `r = 0.015`, `year = 31556952 s`, floats:

   ```
   a = max(0, min(1, exp(r*(locktime - conf_time)/year) - 1)
              - min(1, exp(r*max(0, now/year - locktime/year)) - 1))
   value = int((utxo_value * a) ** 1.3)
   ```

   **Note:** the configurable `bond_value_exponent` is never passed (`taker/taker.py:943-948`);
   the exponent is always 1.3. Float results may differ in the last ulp between libms;
   the value is only used as a selection weight.
6. Offers that share the same `(txid, vout, scriptPubKey)` claim get the same value.

Vector: `offer.bond_value_inputs` / `offer.bond_value`, `offer.freeze_script`,
`offer.bond_p2wsh_scriptpubkey`, `offer.bond_address`.

### 3.4 Selection rules relevant to a bonded-only taker

`taker/orderbook.py:791-906` via `OrderbookManager.select_makers`
(`taker/orderbook.py:1199-1335`):

1. `filter_offers` (`taker/orderbook.py:224-361`): drop ignored/own nicks; only
   `sw0reloffer`/`sw0absoffer`; **if `require_quantized_cj_fees` (default true,
   `taker/config.py:105-108`) the advertised fee must be exactly on the grid**
   `QUANT_REL = {0.00002, 0.00005, 0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1}`,
   `QUANT_ABS = {0, 100, 200, 500, 1000, 2000, 5000, 10000}` (`jmcore/fee_quantization.py:20-41`,
   `taker/orderbook.py:108-112`); `minsize <= cj_amount <= maxsize`; fee within
   `max_cj_fee` (`abs_fee` 500 sats for absolute offers, `rel_fee` 0.001 for relative ones,
   each checked against its own type only, `taker/orderbook.py:194-221`, `taker/config.py:60-70`).
2. `exclude_bondless_offers`: keep only `fidelity_bond_value > 0` (`taker/orderbook.py:624-641,851`).
3. Keep the cheapest offer per maker nick (`taker/orderbook.py:364-397`), then the
   cheapest per bond outpoint (sybil protection, `taker/orderbook.py:422-478`).
4. Pick `n` by bond-weighted random choice (`taker/orderbook.py:644-788`): with allowance 0
   every slot is weighted by `fidelity_bond_value` (`taker/orderbook.py:776-788`). If the
   pool has `<= n` offers, all are taken.
5. `n` is the requested counterparty count (random 8..10 when unset,
   `taker/config.py:23-31`); XBT: if fewer offers exist, `n` drops to the number of offers
   as long as it is `>= minimum_makers` (`taker/taker.py:1275-1294`); fewer selected makers
   than `minimum_makers` fails with "Not enough makers with a fidelity bond"
   (`taker/taker.py:1028-1037,1412-1417`).

---

## 4. CoinJoin session, message by message

State per maker: `taker/models.py` (`MakerSession`). Phases: `_phase_fill`
(`taker/coinjoin_session.py:435`), `_phase_auth` (`:665`), `_phase_build_tx` (`:1186`),
`_phase_collect_signatures` (`:1892`), `_phase_broadcast` (`:2409`); orchestration with
replacements in `taker/taker.py:1093-1719,1826-2298`.

Before `!fill` (`taker/taker.py:1398-1627`): choose inputs that include at least one
PoDLE-capable UTXO, reserve them, check fee headroom (`taker/taker.py:963-1026`),
confirm with the user, generate a fresh commitment (section 5).

Encoding conventions: hex is lowercase; "NaCl" means `base64(nonce24 || crypto_box(m))`
(`jmcore/encryption.py:109-143`).

### 4.1 `!fill` (taker -> maker, plaintext)

`fill <oid> <cj_amount> <taker_nacl_pubkey_hex> P<commitment_hex>` (+ pubkey, sig)
(`taker/coinjoin_session.py:539-545`).

* `taker_nacl_pubkey`: X25519 public key (64 hex). **One keypair per round**, shared by all
  makers of the round; rotated only when every maker is (re)filled
  (`taker/coinjoin_session.py:452-455`, reused at `:426-431`).
* Commitment: `"P" + hex(sha256(P2))` (`jmcore/podle.py:133-141`).
* The maker checks `minsize <= amount <= maxsize`, commitment `P` + 64 hex, not
  blacklisted (`maker/protocol_handlers.py:567-660`, `maker/coinjoin.py:205-268`).
  A blacklisted commitment is answered with silence by reference makers or with an
  `!error` mentioning "blacklist" (`taker/coinjoin_session.py:558-628`).

### 4.2 `!pubkey` (maker -> taker, plaintext)

`pubkey <maker_nacl_pubkey_hex> [features=<comma-separated>]` (+ pubkey, sig)
(`maker/protocol_handlers.py:1519-1525`). This build's makers always send
`features=neutrino_compat` (`maker/coinjoin.py:262`). Taker: first token is the maker NaCl
key; build the box `crypto_box(taker_sk, maker_pk)` (`taker/coinjoin_session.py:383-433`).
The maker's NaCl key is authenticated later by `btc_sig` (4.4).

### 4.3 `!auth` (taker -> maker, NaCl)

Plaintext (`taker/coinjoin_session.py:744-757`):
`<utxo>|<P hex>|<P2 hex>|<s hex>|<e hex>` where `utxo` is `txid:vout` or, when we have
the metadata and the maker advertised `neutrino_compat` (always with this build's makers),
the extended `txid:vout:scriptpubkey_hex:blockheight` (`taker/coinjoin_session.py:698-743`,
`taker/podle.py:93-105`). Encrypted with the maker's box and sent on the pinned channel
(`taker/coinjoin_session.py:771-778`).

Maker side, for reference (`maker/coinjoin.py:272-596`): PoDLE verifies for an index in
`range(taker_utxo_retries)` (default 3); the UTXO exists on its node; `P` controls it
(P2WPKH, P2PKH or P2SH-P2WPKH, `jmcore/podle.py:431-492`); confirmations
`>= taker_utxo_age` (5); value `>= int(cj_amount * taker_utxo_amtpercent / 100)` (20 %).

### 4.4 `!ioauth` (maker -> taker, NaCl)

Plaintext: `<utxo_list> <auth_pub_hex> <cj_addr> <change_addr> <btc_sig_b64>`
(`maker/maker_session.py:767-777`), `utxo_list` = comma-separated `txid:vout` or
`txid:vout:spk:height` (`jmcore/protocol.py:444-481`). Taker validation, all fatal for that
maker (`taker/coinjoin_session.py:795-999`):

1. Decrypt with the maker's box; at least 5 space-separated parts.
2. `btc_sig` = Bitcoin-signed-message ECDSA by `auth_pub` over the **maker's NaCl pubkey
   hex string** (`maker/coinjoin.py:571-575`); verified with `ecdsa_verify`
   (`taker/coinjoin_session.py:863-874`).
3. At most `max_maker_utxos` inputs (default 15) (`taker/coinjoin_session.py:882-896`).
4. Each UTXO: no duplicate; exists unspent on **our** chain view with
   `confirmations > 0`; value and scriptPubKey taken from our lookup, never from the
   maker (`taker/coinjoin_session.py:1070-1158`). Lookup failure (backend unavailable) is
   distinguished from invalid data.
5. No outpoint already used by us or another maker (`taker/coinjoin_session.py:914-935`).
6. Maker change `sum(inputs) - cj_amount - offer.txfee + cjfee >= 27300`
   (`taker/coinjoin_session.py:937-957`, `jmcore/constants.py:28`).
7. `P2WPKH(auth_pub)` equals the scriptPubKey of one declared UTXO (`:959-974`).
8. Both addresses decode for our network (`:976-983,1178-1184`).

### 4.5 `!tx` (taker -> maker, NaCl)

Plaintext = `base64(unsigned_tx_bytes)` (no witness, section 6), encrypted per maker
(`taker/coinjoin_session.py:1906-1968`). Before sending: miner-fee policy check
(`taker/coinjoin_session.py:1740-1766,1899-1904`) and a history record (address reuse
protection).

Maker side (`maker/tx_verification.py:45-150`, `maker/coinjoin.py:598-700`): all its
inputs present; exactly one output to its cj address with value `>= cj_amount`; exactly one
to its change address with value `>= inputs - cj_amount - txfee + cjfee(advertised)`;
`cjfee - txfee >= 0`; version 1..3. XBT maker additionally looks up the amount and
scriptPubKey of **every** input on its own node and refuses to sign otherwise
(`maker/coinjoin.py:684-698`), so the taker's inputs must be visible to the maker's node
(confirmed or in its mempool).

### 4.6 `!sig` (maker -> taker, NaCl), one per maker input

Plaintext = `base64(u8(len(sig)) || sig || u8(len(pub)) || pub)` with `sig = DER || 0x21`
(`maker/coinjoin.py:1080-1092`). The taker decodes (adding missing `=` padding),
then for each of the maker's inputs (in tx order, not yet matched): requires
`P2WPKH(pub) == utxo scriptPubKey` and a valid **0x21** signature over the unified sighash
(`taker/coinjoin_session.py:2056-2116`). Every maker input must end up with exactly one
verified signature, otherwise the maker is dropped and, since its inputs are already in
the tx, the round fails (`taker/coinjoin_session.py:2128-2177`). Witness stored as
`[sig, pub]`.

Then the taker signs its own inputs (0x21, section 6.3,
`taker/coinjoin_session.py:2221-2317`) and assembles the witnesses in input order
(`taker/tx_builder.py:152-226`).

### 4.7 Broadcast, `!push`

* `!push <base64(signed_tx)>` is **plaintext** (`taker/coinjoin_session.py:2452,2766-2774`);
  the `push_encrypted` feature exists in `jmcore/protocol.py:16-17` but the taker does not
  use it. Makers broadcast only a tx they signed for that taker
  (`maker/protocol_handlers.py:1115-1200`).
* Policies (`taker/coinjoin_session.py:2409-2575`): `self`, `random-peer` (try makers in
  random order, verify the txid appears, fall back to self), `multiple-peers` (send to up
  to 3 makers, no verification wait), `not-self`. Verification polls our node for the
  txid every 2 s up to `broadcast_timeout_sec` (30 s) (`taker/coinjoin_session.py:2739-2888`).
  Before any broadcast all inputs are re-checked unspent and confirmed
  (`taker/coinjoin_session.py:2369-2407`). **Note:** the default differs by entry point:
  `TakerConfig.tx_broadcast = multiple-peers` (`taker/config.py:246-249`) but the settings
  file default used by the CLI/Jam path is `random-peer` (`jmcore/settings.py:1265-1268`);
  the regtest lab logged `random-peer`.

### 4.8 `!error` (maker -> taker, plaintext)

`error <free text>` (+ pubkey, sig) (`maker/maker_session.py:783-784`). It ends the wait
for that maker in the current phase (`taker/multi_directory.py:902-914`). In the fill
phase a text containing "blacklist" marks a blacklisted commitment (section 7). In the
auth phase the text is not trusted for policy (`taker/coinjoin_session.py:807-822`). In the
signing phase a parsed low-fee message is recorded (`taker/coinjoin_session.py:1840-1857`).

### 4.9 `!hp2`

After a commitment is used, makers broadcast `!hp2 <commitment>` on PUBLIC from a
throw-away nick (seen in the transcript). The taker ignores these.

Vector: `session` (exact PRIVMSG frames of fill, pubkey, auth, ioauth, tx, sig, push, error
with fixed NaCl keys and nonces, plus plaintexts).

---

## 5. PoDLE

### 5.1 Math (`jmcore/podle.py`)

* `J_i` (NUMS, `jmcore/podle.py:149-191`): for `G_ser` in `[compressed G, uncompressed G]`,
  for `ctr` in 0..255: candidate `0x02 || sha256(G_ser || u8(i) || u8(ctr))`; first valid
  point. Indices 0..255; reference points in `jmcore/podle.py:63-70` and vector
  `podle.nums_points` (0..9).
* For UTXO key `x`: `P = xG`, `P2 = xJ_i`, commitment `C = sha256(P2_compressed)`
  (`jmcore/podle.py:351-363`).
* Proof: nonce `k`; `KG = kG`, `KJ = kJ_i`; `e = sha256(KG || KJ || P || P2)` (all 33-byte
  compressed); `s = k + e*x mod n` (`jmcore/podle.py:369-381,295-317`). If `e mod n == 0`
  or `s == 0`, take the next nonce candidate.
* Nonce (`jmcore/podle.py:247-292`): RFC 6979 HMAC-SHA256 with key `x` and message hash
  `sha256("JoinMarket/PoDLE/nonce/v1" || be32(len(utxo_utf8)) || utxo_utf8 || u8(i) || P || P2)`,
  where `utxo` is the **legacy** `txid:vout` string. Verification does not depend on the
  nonce; matching it is only needed to reproduce the vectors.
* Verification (`jmcore/podle.py:495-578`): `sha256(P2) == C`; `0 < s < n`; for each
  allowed index `KG' = sG - eP`, `KJ' = sJ_i - eP2`, accept if `sha256(KG'||KJ'||P||P2) == e`.

Wire: commitment `P<64 hex>` in `!fill`; revelation `utxo|P|P2|s|e` in `!auth`
(hex fields of 66, 66, 64, 64 chars, `jmcore/podle.py:79-104`).

### 5.2 Which UTXOs qualify (`taker/podle.py:116-148`)

`confirmations >= taker_utxo_age` (5), `value >= int(cj_amount * taker_utxo_amtpercent / 100)`
(20 %), not a fidelity bond / P2WSH; sorted by `(confirmations, value)` descending. The
PoDLE UTXO must be one of the inputs actually spent (`taker/taker.py:1611-1620`,
`taker/taker.py:730-788`). Its key type must be P2WPKH (maker binding, 4.3).

### 5.3 Index usage and history (`taker/podle_manager.py`)

* For each eligible UTXO try indices `0..taker_utxo_retries-1` (3); skip commitments in
  `used` or in the local blacklist; the first fresh one is **added to `used` and persisted
  before it is sent** (`taker/podle_manager.py:183-239,267-331`).
* History file `<data_dir>/cmtdata/commitments.json` (`jmcore/paths.py:58-75`):
  `{"used": [<commitment hex>...], "external": {}}`, optionally
  `"generated_wallets_only": true` (`jmcore/paths.py:81-101`). A fresh wallet starts with
  `{"used": [], "external": {}}`. The Python taker refuses to run without the file or for a
  restored wallet whose history it cannot trust (`taker/podle_manager.py:113-140`): a
  restored wallet does not know which indices were already burned. A mobile wallet should
  persist `used` alongside the wallet and back it up; after a restore it should expect to
  meet blacklisted commitments and rotate.
* Local blacklist `<data_dir>/cmtdata/commitmentlist` (one hex per line,
  `jmcore/commitment_blacklist.py:65-140`); the taker adds a commitment there only when a
  majority of makers rejected it as blacklisted (`taker/taker.py:2104-2120`).

Vector: `podle` (NUMS 0..9, three proofs for indices 0..2 with nonce, KG, KJ, e, s;
legacy and extended revelation strings; binding check).

---

## 6. Transaction construction

### 6.1 Amounts and fees

* Maker fee for an offer (`taker/orderbook.py:115-144`, `jmcore/bitcoin.py:136-182`):
  absolute = `int(cjfee)`; relative = `int((Decimal(cjfee) * Decimal(cj_amount)).quantize(Decimal(1)))`,
  i.e. **round half to even**. With `round_up_cj_fees` (default false) the advertised fee is
  first raised to the next grid value. With `equalize_cj_fees` (default false) every maker
  is paid the largest fee of the set (`taker/orderbook.py:147-164`).
* Fee rate: `fee_rate` from config, else node `estimatesmartfee(3)` (XBT build: no external
  sources; 1 sat/vB fallback), never below the minimum (`min_fee_rate_sat_vb` 1.0 and the
  10-block estimate) nor above `max_fee_rate_sat_vb` (100) (`taker/coinjoin_session.py:1629-1738`).
  Then randomized once per round: uniform in `[rate, min(rate*(1+tx_fee_factor), max)]`,
  `tx_fee_factor` 0.2 (`taker/coinjoin_session.py:1768-1806`).
* `vsize_estimate = 68*inputs + 31*outputs + 11` (`jmcore/fee_policy.py:54-56`);
  `tx_fee = ceil(vsize_estimate * randomized_rate)` (`taker/coinjoin_session.py:1595-1627`).
  Normal mode outputs = `1 + makers + 1 + makers` (`taker/coinjoin_session.py:1244-1255`).
* `offer.txfee` is the maker's absolute contribution to the mining fee, deducted from its
  change (`taker/tx_builder.py:373-378`).
* Taker change = `taker_inputs - cj_amount - sum(maker_fees) - tx_fee`; no change output if
  `<= 2730` (it goes to the miner) (`taker/tx_builder.py:332-351`, `jmcore/constants.py:24`).
* Maker change = `maker_inputs - cj_amount - txfee + cjfee`; negative fails, `< 27300` would
  omit the output (but such makers were dropped in 4.4) (`taker/tx_builder.py:373-402`).
* Actual mining fee = `tx_fee + sum(offer.txfee) + dust residue`. Before `!tx` the taker
  checks `fee <= max_fee_rate * vsize_estimate` and `fee >= min_rate * vsize_estimate`
  (`taker/coinjoin_session.py:1740-1766`, `jmcore/fee_policy.py:59-61`).
* Sweep (amount 0) uses a fixed fee budget from selection time and
  `calculate_sweep_amount` (`jmcore/bitcoin.py:185-263`, `taker/coinjoin_session.py:1259-1400`);
  out of scope for a first mobile version.

Vector: `fees` (half-even cases, per-offer fees, quantization, fee plans, vsize/fee for
three shapes, a sweep amount).

### 6.2 Layout (`taker/tx_builder.py`)

* Inputs: taker inputs then maker inputs; outputs: taker cj, maker cj outputs, taker
  change (if any), maker change outputs; **then both lists are shuffled with a CSPRNG**
  (`taker/tx_builder.py:89-115`). The vectors disable the shuffle; a port must shuffle.
* `version = 2`; unsigned serialization **without** segwit marker/flag/witness
  (`taker/tx_builder.py:133-150`, `jmcore/bitcoin.py:1098-1144`). This exact byte string is
  what `!tx` carries and what makers parse.
* `nLockTime` = anti-fee-sniping height: `0` if height is 0; otherwise with probability
  1/10 `max(1, height - randint(0, 99))`, else `height` (`jmcore/transaction_policy.py:20-32`,
  `taker/coinjoin_session.py:1537-1539`).
* `nSequence = 0xfffffffe` for every input when `nLockTime != 0`, else `0xffffffff`
  (`taker/tx_builder.py:139-143`, `jmcore/transaction_policy.py:11`). No RBF.
* Final: segwit serialization with witness `[sig, compressed_pubkey]` per input
  (`taker/tx_builder.py:184-226`); txid = sha256d of the non-witness serialization
  (`taker/tx_builder.py:228-241`).

### 6.3 Unified sighash 0x21 (`jmwallet/wallet/signing.py`)

Hash type `0x21 = SIGHASH_UNIFIED (0x20) | SIGHASH_ALL (0x01)`. Message for input `i`
of a segwit v0 input (`jmwallet/wallet/signing.py:80-179`), with `sha256` single:

```
0x00                                   epoch
0x21                                   hash type (1 byte)
version          LE 4
nLockTime        LE 5  (zero-extended to 5 bytes)
sha256(concat(txid_LE || vout_LE4  for every input))
sha256(concat(amount_LE8           for every input))      <- new vs BIP143
sha256(concat(compactsize(len(spk)) || spk for every input)) <- new vs BIP143
sha256(concat(nSequence_LE4        for every input))
sha256(concat(value_LE8 || compactsize(len(spk)) || spk for every output))
0x01                                   script type (segwit v0; P2WPKH and P2WSH)
input index      LE 4
compactsize(len(scriptCode)) || scriptCode
sighash = TaggedHash("UnifiedSighash", message)
        = sha256(sha256("UnifiedSighash") || sha256("UnifiedSighash") || message)
```

For P2WPKH `scriptCode = 76 a9 14 <hash160(pub)> 88 ac` (`jmcore/bitcoin.py:1195-1215`).
Differences from BIP143: single SHA-256 aggregates, the two extra aggregates (amounts and
scriptPubKeys of **all** spent outputs), 5-byte locktime, epoch and script-type bytes, the
input index instead of outpoint/amount/sequence of the signed input, and a tagged hash
instead of sha256d. For P2WPKH this is 202 bytes before hashing (vector
`transaction.inputs[*].unified_preimage`).

Signing: `DER(ECDSA(key, sighash)) || 0x21`, RFC 6979, no low-R grinding
(`jmwallet/wallet/signing.py:276-304`). Verification extracts the last byte as hash type
and accepts **only** 0x21 (`jmwallet/wallet/signing.py:192-213,307-343`); signature must be
strict DER low-S (`jmcore/crypto.py:35-77`).

Spent outputs: amounts and scriptPubKeys of every input in tx order; ours from the wallet,
makers' from our own chain lookup in 4.4 (`taker/coinjoin_session.py:2195-2219`). Kilowallet
already implements this message (`TxBuilder.unifiedMessage`, the origin of the Python port).

Vector: `transaction` (inputs, offers, fee, unsigned tx hex/base64, metadata, per input:
script code, unified preimage, unified sighash, BIP143 0x01 sighash for contrast, 0x21
signature, `!sig` wire payload, verification results incl. rejection of the same DER with
0x01; the signed tx and txid). The test also checks that each maker's own
`verify_unsigned_transaction` accepts the unsigned tx.

---

## 7. Failure handling a minimal taker needs

* **Maker silent or invalid in fill/auth**: drop it, ignore it for the rest of the run,
  and try up to `max_maker_replacement_attempts` (3) replacement selections excluding
  current, failed and bond-sibling nicks (`taker/taker.py:2059-2298,1826-1948`,
  `taker/orderbook.py:1372-1375`). Proceed with fewer than requested if at least
  `minimum_makers` remain (`taker/taker.py:2232-2247,2264-2270,1933-1939`).
* **Replacement after `!auth` was revealed**: the commitment is burned for those makers;
  a new commitment (next index / next UTXO) is generated before filling replacements, and
  this is only allowed when every remaining maker already answered `!ioauth`
  (`taker/taker.py:1913-1974`).
* **Blacklisted commitment** (fill phase): if at least half of the filled makers reported a
  blacklist error (silent makers count as blacklist once one maker said so,
  `taker/coinjoin_session.py:612-628`), store it in the local blacklist, generate the next
  fresh commitment, refill the same offers; at most `taker_utxo_retries` times; may add
  more UTXOs of the same mixdepth to find a fresh one (`taker/taker.py:2104-2219`).
  A minority blacklist just drops those makers.
* **Signature phase**: every maker whose inputs are in the tx must sign; any missing or
  invalid signature fails the round, makers that failed are ignored next time
  (`taker/coinjoin_session.py:2151-2177`, `taker/taker.py:1670-1691`). There is no
  rebuild with the remaining makers.
* **Broadcast**: revalidate inputs; policy fallbacks as in 4.7.
* **Upfront failures**: fewer offers than `minimum_makers` (`taker/taker.py:1275-1288`),
  fewer bonded makers than `minimum_makers` (`taker/taker.py:1412-1417`), no fresh PoDLE
  (`taker/taker.py:1622-1627`), fee headroom (`taker/taker.py:963-1026`).
* Inputs reserved for the round are released on failure before the taker signs
  (`taker/taker.py:1711-1719`).

---

## 8. What a mobile taker needs from its environment

* **Tor client, outbound only** (SOCKS to onions). No hidden service, no inbound
  connections. Connections last ~1-3 minutes per round (30-120 s orderbook + 3 phases of
  up to 60 s each), so the round should run in a foreground service.
* **Crypto**: secp256k1 ECDSA with RFC 6979 (no low-R grinding) and DER/low-S checks;
  EC point multiplication and addition with arbitrary points (PoDLE, `KG' = sG - eP`);
  NaCl `crypto_box` (X25519, XSalsa20-Poly1305); SHA-256, HMAC-SHA256, RIPEMD-160,
  base58 (no checksum), bech32. Nothing heavy.
* **Chain lookups**: the taker must look up arbitrary outpoints it does not own:
  maker inputs (exists, unspent, confirmed, value, scriptPubKey), bond outpoints (same plus
  the confirming block's header time), all inputs again before broadcast, and the txid
  after a maker `!push`. The Python taker does this with `gettxout`/`getrawtransaction` on
  its own Knots node. Without a node, the extended UTXO format gives the scriptPubKey and
  height of every maker input and the bond proof determines the bond scriptPubKey, so an
  Electrum server can answer by script hash (listunspent / get_history + header). That
  backend has **no Python reference**: upstream's light-client path is Neutrino, which this
  fork disables. The unified sighash makes these values security-critical: signing with a
  wrong amount for any input yields an invalid tx, not a theft, but verifying makers'
  signatures needs the same values.
* **Fee estimate**: from the node/Electrum server; 1 sat/vB fallback.
* **State**: `commitments.json`-equivalent (used PoDLE commitments), ignored makers, and
  input reservations across rounds.

---

## 9. Vector index (`docs/xbt-taker-vectors.json`)

| key | contents |
|---|---|
| `nick` | nick key, pubkey, nick derivation, signed `!fill` data, message hash, signature, full privmsg line, verification |
| `handshake` | handshake frame/line and its SHA-256, directory DN_HANDSHAKE/challenge/result frames, nick-auth signed message, hash and proof frame |
| `encryption` | NaCl keys, shared key, sample message with fixed nonce both directions |
| `podle` | NUMS points 0..9, proofs for indices 0..2 (P, P2, C, nonce, KG, KJ, e, s), revelation strings, binding |
| `fees` | relative-fee rounding cases, per-offer fees, grid, fee plans, vsize/fee shapes, sweep amount |
| `transaction` | 4 inputs (1 taker, 3 from 2 makers), unsigned tx (shuffle disabled), unified preimages/sighashes, 0x21 signatures, signed tx, txid, locktime cases |
| `offer` | deterministic bond proof and its fields, signed offer privmsg, parsed offer and bond, freeze script, P2WSH, address, bond value |
| `session` | exact PRIVMSG frames for fill/pubkey/auth/ioauth/tx/sig/push/error with plaintexts and fixed nonces |

Regenerate: `PYTHONPATH=$(ls -d */src | paste -sd:) python scripts/xbt/taker_vectors.py > docs/xbt-taker-vectors.json`;
check: `pytest tests/test_xbt_taker_vectors.py`.
