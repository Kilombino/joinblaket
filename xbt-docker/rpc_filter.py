#!/usr/bin/env python3
"""JoinBlaket RPC filter: a small JSON-RPC proxy between JoinBlaket and the node.

Why: Jam, jmwalletd, the maker, the taker and the orderbook watcher talk to the
Internet (Tor, the directory, counterparties). If any of them were compromised,
the node's own RPC credentials would give full control of the node: every RPC,
every wallet of every other app (send, dump keys, stop the node...). This proxy
holds the node's credentials instead; JoinBlaket gets only the proxy's own
random credentials and can call only the RPCs it really uses, and only on its
own wallets.

Rules (enforce mode, the default):
  * clients authenticate with HTTP Basic, the proxy's own user/password;
  * NODE_METHODS are allowed on "/" and on an allowed wallet path;
  * WALLET_METHODS are allowed only on "/wallet/<name>" with <name> a
    JoinBlaket wallet: jm_<8 hex>_<network> (generate_wallet_name in
    jmwallet/backends/descriptor_wallet.py). Never on "/": Bitcoin Core would
    then use the node's default wallet, which may belong to another app;
  * createwallet/loadwallet/unloadwallet only with a JoinBlaket wallet name;
  * listwallets/listwalletdir answer with JoinBlaket wallets only;
  * anything else gets JSON-RPC error -32601 "method not allowed"; in a batch,
    each denied call gets its own error and the allowed ones still run;
  * denied calls are logged with the method name only, never the params.

The list was taken from the traffic of every JoinBlaket flow in a regtest lab
(record mode, see XBT.md "RPC filter"), then checked against the code. Two
things it does on purpose: the orderbook watcher's bond check first tries
listunspent on a wallet named "jm_descriptor_wallet" (the backend's default
name; JoinBlaket never creates it) and falls back to gettxout: that first call
is denied, so it can never read a wallet of that name that someone else made.

Record mode (JM_RPC_FILTER_MODE=record) forwards everything and writes each
(method, scope) once to JM_RPC_FILTER_RECORD_FILE: it is for building the list,
never for production.

Configuration, all from the environment:
  JM_RPC_FILTER_LISTEN        host:port to listen on (default 127.0.0.1:28187)
  JM_RPC_FILTER_NODE_URL      the node, e.g. http://bitcoind:8332 (required)
  JM_RPC_FILTER_NODE_COOKIE   node cookie file (user:password), re-read when it
                              changes or the node answers 401; or
  JM_RPC_FILTER_NODE_USER / JM_RPC_FILTER_NODE_PASS
  JM_RPC_FILTER_CLIENT_USER / JM_RPC_FILTER_CLIENT_PASS   the proxy's own
                              credentials; or
  JM_RPC_FILTER_CLIENT_COOKIE file with user:password for clients; created
                              (random, mode 0600) when missing
  JM_RPC_FILTER_CLIENT_COOKIE_OWNER  uid:gid to chown a created cookie to
  JM_RPC_FILTER_TIMEOUT       seconds to wait for the node (default 21600)
  JM_RPC_FILTER_MODE          enforce (default) or record
  JM_RPC_FILTER_RECORD_FILE   where record mode writes (default stderr only)

`rpc_filter.py --check` makes one authenticated call through a running proxy
(the health check): exit 0 when the node answers through it.

Python standard library only.
"""

from __future__ import annotations

import base64
import hmac
import http.client
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --- the whitelist ----------------------------------------------------------

# Recorded in the regtest lab through every flow Jam/jmwalletd offers (wallet
# create/recover/lock/unlock, rescan, receive, freeze, direct send, sweep, bond
# creation and spend, maker, CoinJoin, tumbler, orderbook watcher): 21 methods.
# Kept although no flow reached them, each for a code path in the image:
#   getaddressinfo        wallet sync, addresses beyond the descriptor range (batch)
#   gettxspendingprevout  send spending an input already spent in the mempool
#   testmempoolaccept     `jm-wallet send` policy check (CLI in the image)
#   unloadwallet, listwalletdir   wallet deletion (`jm-wallet` CLI)
# Dropped from the code's own list: getbalances, getnewaddress (no caller).

# Node-level RPCs: chain data, fee estimation, mempool checks and broadcast.
NODE_METHODS = frozenset(
    {
        "getblockchaininfo",
        "getblockhash",
        "getblockheader",
        "getdescriptorinfo",
        "getmempoolinfo",
        "getrawtransaction",
        "gettxout",
        "gettxspendingprevout",
        "estimatesmartfee",
        "testmempoolaccept",
        "sendrawtransaction",
    }
)

# Wallet RPCs: only on /wallet/<JoinBlaket wallet>. JoinBlaket's node wallets
# are watch-only descriptor wallets (no private keys on the node).
WALLET_METHODS = frozenset(
    {
        "getwalletinfo",
        "importdescriptors",
        "listdescriptors",
        "getaddressinfo",
        "listunspent",
        "listlockunspent",
        "listsinceblock",
        "gettransaction",
        "getreceivedbyaddress",
        "rescanblockchain",
    }
)

# Wallet management: on "/" with a JoinBlaket wallet name as first parameter
# (unloadwallet may also come without a name on its own wallet path).
WALLET_ADMIN_METHODS = frozenset({"createwallet", "loadwallet", "unloadwallet"})
# Answered with JoinBlaket wallets only.
WALLET_LIST_METHODS = frozenset({"listwallets", "listwalletdir"})

ALLOWED_METHODS = (
    NODE_METHODS | WALLET_METHODS | WALLET_ADMIN_METHODS | WALLET_LIST_METHODS
)

# generate_wallet_name(): f"jm_{fingerprint}_{network}", fingerprint = 4 bytes hex.
WALLET_NAME_RE = re.compile(r"\Ajm_[0-9a-f]{8}_(mainnet|testnet|signet|regtest)\Z")

# Named-parameter keys holding the wallet name (Bitcoin Core's names).
_WALLET_NAME_KEYS = {
    "createwallet": "wallet_name",
    "loadwallet": "filename",
    "unloadwallet": "wallet_name",
}

MAX_BODY = 32 * 1024 * 1024
MAX_CONCURRENT = 32
ERR_NOT_ALLOWED = -32601
ERR_INVALID_REQUEST = -32600
ERR_PARSE = -32700
ERR_INTERNAL = -32603


def log(msg: str) -> None:
    print(f"[rpc-filter] {msg}", file=sys.stderr, flush=True)


def is_jm_wallet(name: Any) -> bool:
    return isinstance(name, str) and WALLET_NAME_RE.match(name) is not None


def parse_path(path: str) -> tuple[bool, str | None]:
    """("/" or "/wallet/<name>") -> (valid, wallet name or None)."""
    path = path.split("?", 1)[0]
    if path in ("", "/"):
        return True, None
    if path.startswith("/wallet/"):
        name = urllib.parse.unquote(path[len("/wallet/") :])
        return True, name
    return False, None


def _first_param(params: Any, key: str) -> Any:
    if isinstance(params, list):
        return params[0] if params else None
    if isinstance(params, dict):
        return params.get(key)
    return None


def check_call(call: Any, wallet: str | None) -> str | None:
    """None if the call may go to the node, else the reason it may not."""
    if not isinstance(call, dict) or not isinstance(call.get("method"), str):
        return "invalid request"
    method = call["method"]
    params = call.get("params", [])
    if params is not None and not isinstance(params, (list, dict)):
        return "invalid request"
    if method not in ALLOWED_METHODS:
        return "method not allowed"
    if wallet is not None and not is_jm_wallet(wallet):
        return "wallet not allowed"
    if method in WALLET_METHODS:
        return (
            None
            if wallet is not None
            else "wallet method needs a JoinBlaket wallet path"
        )
    if method in WALLET_ADMIN_METHODS:
        name = _first_param(params, _WALLET_NAME_KEYS[method])
        if name is None and method == "unloadwallet" and wallet is not None:
            return None
        return None if is_jm_wallet(name) else "wallet not allowed"
    return None


def scope_of(wallet: str | None) -> str:
    if wallet is None:
        return "node"
    return "wallet:jm" if is_jm_wallet(wallet) else "wallet:OTHER"


def filter_wallet_list(method: str, result: Any) -> Any:
    if method == "listwallets" and isinstance(result, list):
        return [w for w in result if is_jm_wallet(w)]
    if (
        method == "listwalletdir"
        and isinstance(result, dict)
        and isinstance(result.get("wallets"), list)
    ):
        result = dict(result)
        result["wallets"] = [
            w
            for w in result["wallets"]
            if isinstance(w, dict) and is_jm_wallet(w.get("name"))
        ]
    return result


def error_obj(call: Any, code: int, message: str) -> dict[str, Any]:
    cid = call.get("id") if isinstance(call, dict) else None
    if isinstance(call, dict) and call.get("jsonrpc") == "2.0":
        return {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": cid,
        }
    return {"result": None, "error": {"code": code, "message": message}, "id": cid}


def _is_notification(call: Any) -> bool:
    return isinstance(call, dict) and call.get("jsonrpc") == "2.0" and "id" not in call


# --- credentials ------------------------------------------------------------


def _read_cookie(path: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    if ":" not in content:
        raise ValueError(f"{path}: expected user:password")
    user, password = content.split(":", 1)
    return user, password


class NodeAuth:
    """The node's credentials: fixed, or a cookie file re-read when it changes."""

    def __init__(self, cookie: str | None, user: str | None, password: str | None):
        self._cookie = cookie
        self._fixed = (user or "", password or "") if not cookie else None
        self._lock = threading.Lock()
        self._cached: tuple[str, str] | None = None
        self._mtime: float | None = None

    def header(self, force_reload: bool = False) -> str:
        if self._fixed is not None:
            user, password = self._fixed
        else:
            assert self._cookie is not None
            with self._lock:
                try:
                    mtime = os.stat(self._cookie).st_mtime
                except OSError:
                    mtime = None
                if force_reload or self._cached is None or mtime != self._mtime:
                    self._cached = _read_cookie(self._cookie)
                    self._mtime = mtime
                user, password = self._cached
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return f"Basic {token}"


def load_client_credentials(env: dict[str, str]) -> tuple[str, str]:
    user, password = (
        env.get("JM_RPC_FILTER_CLIENT_USER"),
        env.get("JM_RPC_FILTER_CLIENT_PASS"),
    )
    if user and password:
        return user, password
    path = env.get("JM_RPC_FILTER_CLIENT_COOKIE")
    if not path:
        raise SystemExit(
            "rpc-filter: set JM_RPC_FILTER_CLIENT_COOKIE or JM_RPC_FILTER_CLIENT_USER/PASS"
        )
    if not os.path.exists(path):
        old = os.umask(0o077)
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"jmrpcfilter:{secrets.token_hex(32)}\n")
        finally:
            os.umask(old)
        owner = env.get("JM_RPC_FILTER_CLIENT_COOKIE_OWNER")
        if owner:
            uid, _, gid = owner.partition(":")
            os.chown(path, int(uid), int(gid or uid))
        log(f"created client credentials in {path}")
    return _read_cookie(path)


# --- the proxy --------------------------------------------------------------


class Filter:
    def __init__(
        self,
        node_url: str,
        node_auth: NodeAuth,
        client_user: str,
        client_password: str,
        mode: str = "enforce",
        record_file: str | None = None,
        timeout: float = 21600.0,
    ):
        u = urllib.parse.urlsplit(node_url)
        if u.scheme != "http" or not u.hostname:
            raise SystemExit(
                f"rpc-filter: JM_RPC_FILTER_NODE_URL must be http://host:port, got {node_url!r}"
            )
        self.node_host = u.hostname
        self.node_port = u.port or 8332
        self.node_base = u.path.rstrip("/")
        self.node_auth = node_auth
        token = base64.b64encode(f"{client_user}:{client_password}".encode()).decode()
        self._client_header = f"Basic {token}".encode()
        if mode not in ("enforce", "record"):
            raise SystemExit(
                f"rpc-filter: JM_RPC_FILTER_MODE must be enforce or record, got {mode!r}"
            )
        self.mode = mode
        self.record_file = record_file
        self._recorded: set[tuple[str, str]] = set()
        self._record_lock = threading.Lock()
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)

    def auth_ok(self, header: str | None) -> bool:
        return header is not None and hmac.compare_digest(
            header.strip().encode(), self._client_header
        )

    def record(self, method: Any, wallet: str | None, verdict: str | None) -> None:
        key = (str(method)[:64], scope_of(wallet))
        with self._record_lock:
            if key in self._recorded:
                return
            self._recorded.add(key)
            line = json.dumps(
                {"method": key[0], "scope": key[1], "enforce": verdict or "allow"}
            )
            log(f"record {line}")
            if self.record_file:
                with open(self.record_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")

    def forward(self, path: str, body: bytes) -> tuple[int, bytes, str]:
        """POST body to the node at path; one retry with a fresh cookie on 401."""
        for attempt in (0, 1):
            conn = http.client.HTTPConnection(
                self.node_host, self.node_port, timeout=self.timeout
            )
            try:
                conn.request(
                    "POST",
                    self.node_base + path,
                    body=body,
                    headers={
                        "Authorization": self.node_auth.header(
                            force_reload=attempt == 1
                        ),
                        "Content-Type": "application/json",
                    },
                )
                resp = conn.getresponse()
                data = resp.read()
                if resp.status == 401 and attempt == 0:
                    continue
                return (
                    resp.status,
                    data,
                    resp.getheader("Content-Type", "application/json"),
                )
            finally:
                conn.close()
        raise AssertionError("unreachable")

    def handle(self, path: str, body: bytes) -> tuple[int, Any]:
        """Filter one request. Returns (HTTP status, JSON value or raw bytes)."""
        valid, wallet = parse_path(path)
        if not valid:
            log("denied path (not / or /wallet/<name>)")
            return 404, error_obj(
                None, ERR_INVALID_REQUEST, "path not allowed by JoinBlaket's RPC filter"
            )
        try:
            request = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return 500, error_obj(None, ERR_PARSE, "parse error")

        upstream_path = (
            "/" if wallet is None else "/wallet/" + urllib.parse.quote(wallet, safe="")
        )

        if isinstance(request, list):
            return self._handle_batch(request, wallet, upstream_path)

        verdict = check_call(request, wallet)
        method = request.get("method") if isinstance(request, dict) else None
        if self.mode == "record":
            self.record(method, wallet, verdict)
            verdict = None if verdict != "invalid request" else verdict
        if verdict is not None:
            self._log_denied(method, wallet, verdict)
            code = (
                ERR_INVALID_REQUEST if verdict == "invalid request" else ERR_NOT_ALLOWED
            )
            status = (
                200
                if isinstance(request, dict) and request.get("jsonrpc") == "2.0"
                else (400 if code == ERR_INVALID_REQUEST else 404)
            )
            return status, error_obj(
                request,
                code,
                f"{verdict} by JoinBlaket's RPC filter: {str(method)[:64]}",
            )

        status, data, _ctype = self.forward(upstream_path, body)
        if method in WALLET_LIST_METHODS and self.mode == "enforce":
            try:
                reply = json.loads(data)
                if isinstance(reply, dict) and reply.get("error") is None:
                    reply["result"] = filter_wallet_list(method, reply.get("result"))
                    return status, reply
            except ValueError:
                pass
        return status, data

    def _handle_batch(
        self, batch: list[Any], wallet: str | None, upstream_path: str
    ) -> tuple[int, Any]:
        if not batch:
            return 200, error_obj(None, ERR_INVALID_REQUEST, "empty batch")
        verdicts: list[str | None] = []
        for call in batch:
            v = check_call(call, wallet)
            method = call.get("method") if isinstance(call, dict) else None
            if self.mode == "record":
                self.record(method, wallet, v)
                v = None if v != "invalid request" else v
            if v is not None:
                self._log_denied(method, wallet, v)
            verdicts.append(v)
        allowed = [c for c, v in zip(batch, verdicts) if v is None]
        upstream: list[Any] = []
        status = 200
        if allowed:
            status, data, _ctype = self.forward(
                upstream_path, json.dumps(allowed).encode()
            )
            try:
                upstream = json.loads(data)
            except ValueError:
                return 502, error_obj(None, ERR_INTERNAL, "node sent a non-JSON reply")
            if not isinstance(upstream, list):
                # The node refused the whole batch: pass its answer on.
                return status, upstream
        replies: list[Any] = []
        it = iter(upstream)
        for call, v in zip(batch, verdicts):
            if v is None:
                if _is_notification(call):
                    continue
                reply = next(it, None)
                if reply is None:
                    reply = error_obj(
                        call, ERR_INTERNAL, "node dropped this call's reply"
                    )
                elif (
                    self.mode == "enforce"
                    and isinstance(reply, dict)
                    and reply.get("error") is None
                ):
                    m = call.get("method")
                    if m in WALLET_LIST_METHODS:
                        reply = dict(reply)
                        reply["result"] = filter_wallet_list(m, reply.get("result"))
                replies.append(reply)
            elif not _is_notification(call):
                code = (
                    ERR_INVALID_REQUEST if v == "invalid request" else ERR_NOT_ALLOWED
                )
                method = call.get("method") if isinstance(call, dict) else None
                replies.append(
                    error_obj(
                        call,
                        code,
                        f"{v} by JoinBlaket's RPC filter: {str(method)[:64]}",
                    )
                )
        return (status if allowed else 200), replies

    @staticmethod
    def _log_denied(method: Any, wallet: str | None, verdict: str) -> None:
        name = re.sub(r"[^A-Za-z0-9_]", "?", str(method))[:64]
        log(f"DENIED {name} on {scope_of(wallet)}: {verdict}")


def make_handler(flt: Filter) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "jm-rpc-filter"
        sys_version = ""

        def log_message(
            self, format: str, *args: Any
        ) -> None:  # quiet: no params in logs
            pass

        def _send(
            self, status: int, payload: Any, extra: dict[str, str] | None = None
        ) -> None:
            body = (
                payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._send(
                405, error_obj(None, ERR_INVALID_REQUEST, "JSON-RPC over POST only")
            )

        def do_POST(self) -> None:
            if not flt.auth_ok(self.headers.get("Authorization")):
                time.sleep(0.25)
                self.close_connection = True
                self._send(401, b"", {"WWW-Authenticate": 'Basic realm="jsonrpc"'})
                return
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self._send(
                    411, error_obj(None, ERR_INVALID_REQUEST, "Content-Length required")
                )
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                self._send(
                    413, error_obj(None, ERR_INVALID_REQUEST, "bad request size")
                )
                return
            body = self.rfile.read(length)
            if not flt.slots.acquire(timeout=60):
                self._send(503, error_obj(None, ERR_INTERNAL, "rpc filter busy"))
                return
            try:
                status, payload = flt.handle(self.path, body)
            except (OSError, http.client.HTTPException) as exc:
                log(f"node unreachable: {type(exc).__name__}")
                status, payload = (
                    502,
                    error_obj(
                        None,
                        ERR_INTERNAL,
                        f"node unreachable through JoinBlaket's RPC filter ({type(exc).__name__})",
                    ),
                )
            finally:
                flt.slots.release()
            self._send(status, payload)

    return Handler


def build_from_env(env: dict[str, str]) -> Filter:
    node_url = env.get("JM_RPC_FILTER_NODE_URL", "")
    cookie = env.get("JM_RPC_FILTER_NODE_COOKIE") or None
    user = env.get("JM_RPC_FILTER_NODE_USER") or None
    password = env.get("JM_RPC_FILTER_NODE_PASS") or None
    if not node_url:
        raise SystemExit("rpc-filter: JM_RPC_FILTER_NODE_URL is not set")
    if not cookie and not (user and password):
        raise SystemExit(
            "rpc-filter: set JM_RPC_FILTER_NODE_COOKIE or JM_RPC_FILTER_NODE_USER/PASS"
        )
    client_user, client_password = load_client_credentials(env)
    return Filter(
        node_url,
        NodeAuth(cookie, user, password),
        client_user,
        client_password,
        mode=env.get("JM_RPC_FILTER_MODE", "enforce"),
        record_file=env.get("JM_RPC_FILTER_RECORD_FILE") or None,
        timeout=float(env.get("JM_RPC_FILTER_TIMEOUT", "21600")),
    )


def _listen_addr(env: dict[str, str]) -> tuple[str, int]:
    host, _, port = env.get("JM_RPC_FILTER_LISTEN", "127.0.0.1:28187").rpartition(":")
    return host.strip("[]") or "127.0.0.1", int(port)


def check(env: dict[str, str]) -> int:
    """Health check: one call through the running proxy."""
    host, port = _listen_addr(env)
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        user, password = load_client_credentials(
            {**env, "JM_RPC_FILTER_CLIENT_COOKIE_OWNER": ""}
        )
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        conn = http.client.HTTPConnection(host, port, timeout=20)
        conn.request(
            "POST",
            "/",
            body=json.dumps(
                {
                    "jsonrpc": "1.0",
                    "id": "health",
                    "method": "getblockchaininfo",
                    "params": [],
                }
            ),
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        reply = json.loads(resp.read())
        if reply.get("error"):
            print(f"node error: {reply['error'].get('message')}")
            return 1
        r = reply["result"]
        print(
            f"node OK through the RPC filter: chain={r.get('chain')} height={r.get('blocks')}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
        print(f"RPC filter or node not reachable: {type(exc).__name__}: {exc}")
        return 1


def main(argv: list[str]) -> int:
    env = dict(os.environ)
    if "--check" in argv:
        return check(env)
    flt = build_from_env(env)
    for k in ("JM_RPC_FILTER_NODE_PASS", "JM_RPC_FILTER_CLIENT_PASS"):
        os.environ.pop(k, None)
    host, port = _listen_addr(env)
    server = ThreadingHTTPServer((host, port), make_handler(flt))
    server.daemon_threads = True
    log(
        f"listening on {host}:{port}, node {flt.node_host}:{flt.node_port}, mode {flt.mode}, "
        f"{len(ALLOWED_METHODS)} methods allowed"
    )
    if flt.mode == "record":
        log("RECORD MODE: every method is forwarded. Never use this in production.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
