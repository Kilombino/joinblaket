"""JoinBlaket's RPC filter (xbt-docker/rpc_filter.py): whitelist, wallet paths, batches, auth."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

_PATH = Path(__file__).resolve().parents[1] / "xbt-docker" / "rpc_filter.py"
_spec = importlib.util.spec_from_file_location("jm_xbt_rpc_filter", _PATH)
assert _spec and _spec.loader
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)

JM = "jm_8fc9971d_mainnet"
NODE_USER, NODE_PASS = "nodeuser", "nodepass"
CLIENT_USER, CLIENT_PASS = "jmrpcfilter", "s3cret"


class FakeNode:
    """A JSON-RPC 'node' recording what reaches it; answers every call."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, Any, str | None]] = []
        self.password = NODE_PASS
        node = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                auth = self.headers.get("Authorization")
                good = (
                    "Basic "
                    + base64.b64encode(f"{NODE_USER}:{node.password}".encode()).decode()
                )
                if auth != good:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                req = json.loads(body)
                node.seen.append((self.path, req, auth))

                def answer(c: dict[str, Any]) -> dict[str, Any]:
                    m = c["method"]
                    if m == "listwallets":
                        res: Any = [JM, "other-app", "", "jm_deadbeef_regtest"]
                    elif m == "listwalletdir":
                        res = {
                            "wallets": [{"name": JM}, {"name": "lnd"}, {"name": "jm_x"}]
                        }
                    else:
                        res = {"method": m}
                    return {"result": res, "error": None, "id": c.get("id")}

                out: Any = (
                    [answer(c) for c in req] if isinstance(req, list) else answer(req)
                )
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture()
def node() -> Iterator[FakeNode]:
    n = FakeNode()
    yield n
    n.close()


def _filter(
    node: FakeNode,
    cookie: str | None = None,
    mode: str = "enforce",
    record: str | None = None,
) -> Any:
    return rf.Filter(
        f"http://127.0.0.1:{node.port}",
        rf.NodeAuth(
            cookie, None if cookie else NODE_USER, None if cookie else NODE_PASS
        ),
        CLIENT_USER,
        CLIENT_PASS,
        mode=mode,
        record_file=record,
    )


def _call(
    flt: Any, method: str, params: Any = None, path: str = "/", v2: bool = True
) -> tuple[int, Any]:
    req: dict[str, Any] = {
        "id": 7,
        "method": method,
        "params": params if params is not None else [],
    }
    if v2:
        req["jsonrpc"] = "2.0"
    status, payload = flt.handle(path, json.dumps(req).encode())
    return status, json.loads(payload) if isinstance(payload, bytes) else payload


# --- methods ---------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(rf.NODE_METHODS))
def test_node_methods_pass_on_root(node: FakeNode, method: str) -> None:
    status, reply = _call(_filter(node), method)
    assert status == 200 and reply["result"] == {"method": method}
    assert node.seen[-1][0] == "/"


@pytest.mark.parametrize(
    "method",
    [
        "stop",
        "dumpprivkey",
        "dumpwallet",
        "sendtoaddress",
        "sendmany",
        "walletpassphrase",
        "signrawtransactionwithwallet",
        "importprivkey",
        "backupwallet",
        "setban",
        "addnode",
        "invalidateblock",
        "generatetoaddress",
        "help",
        "getblock",
        "lockunspent",
        "getbalances",
        "getnewaddress",
        "getblockcount",
        "Getblockchaininfo",
        "",
    ],
)
def test_other_methods_denied_and_never_reach_the_node(
    node: FakeNode, method: str
) -> None:
    flt = _filter(node)
    for path in ("/", f"/wallet/{JM}"):
        status, reply = _call(flt, method, path=path)
        assert status == 200
        assert reply["error"]["code"] == -32601
        assert "not allowed" in reply["error"]["message"]
        assert reply["id"] == 7 and "result" not in reply
    assert node.seen == []


def test_legacy_jsonrpc_denial_uses_404_like_bitcoind(node: FakeNode) -> None:
    status, reply = _call(_filter(node), "stop", v2=False)
    assert (
        status == 404 and reply["result"] is None and reply["error"]["code"] == -32601
    )


def test_whitelist_is_what_joinblaket_uses() -> None:
    # Derived from recorded regtest traffic of every flow (XBT.md, "RPC filter").
    assert rf.ALLOWED_METHODS == {
        "createwallet", "loadwallet", "unloadwallet", "listwallets", "listwalletdir",
        "getwalletinfo", "importdescriptors", "listdescriptors", "getdescriptorinfo",
        "getaddressinfo", "listunspent", "listlockunspent", "listsinceblock",
        "gettransaction", "getreceivedbyaddress", "rescanblockchain",
        "getblockchaininfo", "getblockhash", "getblockheader", "getrawtransaction", "gettxout",
        "gettxspendingprevout", "getmempoolinfo", "estimatesmartfee", "testmempoolaccept",
        "sendrawtransaction",
    }  # fmt: skip


# --- wallet paths ----------------------------------------------------------


@pytest.mark.parametrize("method", sorted(rf.WALLET_METHODS))
def test_wallet_methods_only_on_a_joinblaket_wallet(
    node: FakeNode, method: str
) -> None:
    flt = _filter(node)
    status, reply = _call(flt, method, path=f"/wallet/{JM}")
    assert reply["result"] == {"method": method}
    assert node.seen[-1][0] == f"/wallet/{JM}"
    # never on "/": the node would pick its default wallet (maybe another app's)
    status, reply = _call(flt, method)
    assert reply["error"]["code"] == -32601
    for other in (
        "lnd",
        "jm_8fc9971d_mainnet/../x",
        "jm_8FC9971D_mainnet",
        "jm_8fc9971d_main",
        "",
        "jm_descriptor_wallet",
    ):
        status, reply = _call(flt, method, path="/wallet/" + other)
        assert reply["error"]["code"] == -32601, other
    assert len(node.seen) == 1


def test_url_encoded_wallet_path(node: FakeNode) -> None:
    flt = _filter(node)
    _, reply = _call(flt, "getwalletinfo", path="/wallet/jm%5F8fc9971d%5Fmainnet")
    assert reply["result"] == {"method": "getwalletinfo"}
    _, reply = _call(flt, "getwalletinfo", path="/wallet/%2e%2e%2fother")
    assert reply["error"]["code"] == -32601


def test_node_method_on_other_wallet_path_denied(node: FakeNode) -> None:
    _, reply = _call(_filter(node), "sendrawtransaction", ["00"], path="/wallet/lnd")
    assert reply["error"]["code"] == -32601 and node.seen == []


def test_unknown_paths_rejected(node: FakeNode) -> None:
    flt = _filter(node)
    for path in ("/rest/tx/abc.json", "/wallet", "/x"):
        status, payload = flt.handle(path, b'{"method":"getblockchaininfo","id":1}')
        assert status == 404
    assert node.seen == []


@pytest.mark.parametrize("method", ["createwallet", "loadwallet", "unloadwallet"])
def test_wallet_admin_needs_a_joinblaket_name(node: FakeNode, method: str) -> None:
    flt = _filter(node)
    _, reply = _call(flt, method, [JM, False])
    assert reply["result"] == {"method": method}
    key = rf._WALLET_NAME_KEYS[method]
    _, reply = _call(flt, method, {key: JM})
    assert reply["result"] == {"method": method}
    for bad in (["lnd"], ["../../x"], [], {key: "lnd"}, [None], [1]):
        _, reply = _call(flt, method, bad)
        assert reply["error"]["code"] == -32601, bad
    assert len(node.seen) == 2


def test_unloadwallet_without_name_on_own_wallet_path(node: FakeNode) -> None:
    _, reply = _call(_filter(node), "unloadwallet", [], path=f"/wallet/{JM}")
    assert reply["result"] == {"method": "unloadwallet"}


def test_wallet_lists_show_only_joinblaket_wallets(node: FakeNode) -> None:
    flt = _filter(node)
    _, reply = _call(flt, "listwallets")
    assert reply["result"] == [JM, "jm_deadbeef_regtest"]
    _, reply = _call(flt, "listwalletdir")
    assert reply["result"] == {"wallets": [{"name": JM}]}


# --- batches ---------------------------------------------------------------


def test_batch_denied_items_get_their_own_error(node: FakeNode) -> None:
    flt = _filter(node)
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "getaddressinfo", "params": ["a"]},
        {"jsonrpc": "2.0", "id": 2, "method": "dumpprivkey", "params": ["a"]},
        {"jsonrpc": "2.0", "id": 3, "method": "getaddressinfo", "params": ["b"]},
        {"jsonrpc": "2.0", "id": 4, "method": "listwallets", "params": []},
        "garbage",
    ]
    status, replies = flt.handle(f"/wallet/{JM}", json.dumps(batch).encode())
    assert status == 200 and [r["id"] for r in replies] == [1, 2, 3, 4, None]
    assert replies[0]["result"] == {"method": "getaddressinfo"}
    assert replies[1]["error"]["code"] == -32601
    assert replies[2]["result"] == {"method": "getaddressinfo"}
    assert replies[3]["result"] == [JM, "jm_deadbeef_regtest"]
    assert replies[4]["error"]["code"] == -32600
    # only the allowed calls reached the node, as one batch
    assert len(node.seen) == 1
    assert [c["method"] for c in node.seen[0][1]] == [
        "getaddressinfo",
        "getaddressinfo",
        "listwallets",
    ]


def test_batch_all_denied_never_reaches_node(node: FakeNode) -> None:
    flt = _filter(node)
    batch = [
        {"id": 1, "method": "stop"},
        {"id": 2, "method": "getwalletinfo"},
    ]  # a wallet method on "/"
    status, replies = flt.handle("/", json.dumps(batch).encode())
    assert status == 200 and all(r["error"]["code"] == -32601 for r in replies)
    assert node.seen == []


def test_batch_on_foreign_wallet_denied(node: FakeNode) -> None:
    batch = [{"id": 1, "method": "getaddressinfo", "params": ["a"]}]
    _, replies = _filter(node).handle("/wallet/lnd", json.dumps(batch).encode())
    assert replies[0]["error"]["code"] == -32601 and node.seen == []


def test_empty_batch_and_bad_json(node: FakeNode) -> None:
    flt = _filter(node)
    status, reply = flt.handle("/", b"[]")
    assert reply["error"]["code"] == -32600
    status, reply = flt.handle("/", b"{nope")
    assert reply["error"]["code"] == -32700
    status, reply = flt.handle("/", b'{"method": 5, "id": 1}')
    assert reply["error"]["code"] == -32600
    status, reply = flt.handle(
        "/", b'{"method": "getblockhash", "params": "x", "id": 1}'
    )
    assert reply["error"]["code"] == -32600
    assert node.seen == []


# --- credentials -----------------------------------------------------------


def test_client_auth_constant_and_exact(node: FakeNode) -> None:
    flt = _filter(node)
    good = "Basic " + base64.b64encode(f"{CLIENT_USER}:{CLIENT_PASS}".encode()).decode()
    assert flt.auth_ok(good)
    assert not flt.auth_ok(None)
    assert not flt.auth_ok(
        "Basic " + base64.b64encode(f"{NODE_USER}:{NODE_PASS}".encode()).decode()
    )
    assert not flt.auth_ok(
        "Basic " + base64.b64encode(f"{CLIENT_USER}:x".encode()).decode()
    )


def test_node_cookie_reread_after_node_restart(node: FakeNode, tmp_path: Path) -> None:
    cookie = tmp_path / ".cookie"
    cookie.write_text(f"{NODE_USER}:{NODE_PASS}")
    flt = _filter(node, cookie=str(cookie))
    assert _call(flt, "getblockhash", [1])[1]["result"]
    # node restarts with a new cookie; same mtime is possible, the 401 forces a re-read
    node.password = "rotated"
    st = os.stat(cookie)
    cookie.write_text(f"{NODE_USER}:rotated")
    os.utime(cookie, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _call(flt, "getblockhash", [1])[1]["result"] == {"method": "getblockhash"}


def test_client_cookie_created_0600_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "client.cookie"
    env = {"JM_RPC_FILTER_CLIENT_COOKIE": str(path)}
    user, password = rf.load_client_credentials(env)
    assert user == "jmrpcfilter" and len(password) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rf.load_client_credentials(env) == (user, password)


def test_client_cookie_never_follows_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    link = tmp_path / "client.cookie"
    link.symlink_to(target)
    with pytest.raises(OSError):
        rf.load_client_credentials({"JM_RPC_FILTER_CLIENT_COOKIE": str(link)})
    assert not target.exists()


def test_env_needs_node_and_client_credentials() -> None:
    with pytest.raises(SystemExit):
        rf.build_from_env({"JM_RPC_FILTER_NODE_URL": "http://127.0.0.1:1"})
    with pytest.raises(SystemExit):
        rf.build_from_env(
            {"JM_RPC_FILTER_NODE_USER": "a", "JM_RPC_FILTER_NODE_PASS": "b"}
        )
    with pytest.raises(SystemExit):
        rf.build_from_env(
            {
                "JM_RPC_FILTER_NODE_URL": "https://x:1",
                "JM_RPC_FILTER_NODE_USER": "a",
                "JM_RPC_FILTER_NODE_PASS": "b",
                "JM_RPC_FILTER_CLIENT_USER": "c",
                "JM_RPC_FILTER_CLIENT_PASS": "d",
            }
        )


# --- over HTTP -------------------------------------------------------------


@pytest.fixture()
def served(node: FakeNode) -> Iterator[tuple[int, FakeNode]]:
    flt = _filter(node)
    server = ThreadingHTTPServer(("127.0.0.1", 0), rf.make_handler(flt))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], node
    server.shutdown()


def _post(
    port: int,
    body: Any,
    user: str = CLIENT_USER,
    password: str = CLIENT_PASS,
    path: str = "/",
) -> tuple[int, Any]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def test_http_wrong_credentials_401_and_node_untouched(
    served: tuple[int, FakeNode],
) -> None:
    port, node = served
    status, _ = _post(port, {"id": 1, "method": "getblockchaininfo"}, password="wrong")
    assert status == 401
    status, _ = _post(
        port, {"id": 1, "method": "getblockchaininfo"}, NODE_USER, NODE_PASS
    )
    assert status == 401
    assert node.seen == []


def test_http_forwards_with_node_credentials(served: tuple[int, FakeNode]) -> None:
    port, node = served
    status, reply = _post(
        port, {"jsonrpc": "2.0", "id": 1, "method": "getblockchaininfo"}
    )
    assert status == 200 and reply["result"] == {"method": "getblockchaininfo"}
    assert (
        node.seen[0][2]
        == "Basic " + base64.b64encode(f"{NODE_USER}:{NODE_PASS}".encode()).decode()
    )
    status, reply = _post(port, {"jsonrpc": "2.0", "id": 2, "method": "stop"})
    assert reply["error"]["code"] == -32601 and len(node.seen) == 1


def test_http_node_down_is_an_rpc_error(node: FakeNode) -> None:
    flt = rf.Filter(
        "http://127.0.0.1:9", rf.NodeAuth(None, "a", "b"), CLIENT_USER, CLIENT_PASS
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), rf.make_handler(flt))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, reply = _post(
            server.server_address[1], {"id": 1, "method": "getblockchaininfo"}
        )
        assert status == 502 and "node unreachable" in reply["error"]["message"]
    finally:
        server.shutdown()


def test_denied_calls_logged_without_params(
    node: FakeNode, capsys: pytest.CaptureFixture[str]
) -> None:
    _call(_filter(node), "dumpprivkey", ["SECRET-ADDRESS-xyz"])
    err = capsys.readouterr().err
    assert "DENIED dumpprivkey" in err and "SECRET" not in err


def test_record_mode_forwards_and_writes_each_method_once(
    node: FakeNode, tmp_path: Path
) -> None:
    rec = tmp_path / "rec.jsonl"
    flt = _filter(node, mode="record", record=str(rec))
    _call(flt, "getblock", ["h"])
    _call(flt, "getblock", ["h2"])
    _call(flt, "getwalletinfo", path=f"/wallet/{JM}")
    lines = [json.loads(x) for x in rec.read_text().splitlines()]
    assert lines == [
        {"method": "getblock", "scope": "node", "enforce": "method not allowed"},
        {"method": "getwalletinfo", "scope": "wallet:jm", "enforce": "allow"},
    ]
    assert len(node.seen) == 3


# --- packaging -------------------------------------------------------------

_XBT_DOCKER = Path(__file__).resolve().parents[1] / "xbt-docker"


def test_image_installs_the_filter_and_its_own_user() -> None:
    dockerfile = (_XBT_DOCKER / "Dockerfile").read_text()
    assert "xbt-docker/rpc_filter.py /usr/local/lib/jm-xbt/rpc_filter.py" in dockerfile
    assert "--uid 1001" in dockerfile and "jmrpc" in dockerfile
    # starts as root only to split users; the entrypoint drops to uid 1000
    assert not any(line.startswith("USER ") for line in dockerfile.splitlines())


def test_entrypoint_hands_jmwalletd_only_the_filter() -> None:
    entry = (_XBT_DOCKER / "entrypoint.sh").read_text()
    assert "start_rpc_filter jmrpc /run/jm-xbt-rpc" in entry
    assert "setpriv --reuid=jm --regid=jm" in entry
    assert 'export BITCOIN__RPC_URL="http://127.0.0.1:$FILTER_PORT"' in entry
    assert "unset BITCOIN__RPC_USER BITCOIN__RPC_PASSWORD" in entry
    assert 'env -i PATH="$PATH"' in entry  # the filter starts with a clean environment
