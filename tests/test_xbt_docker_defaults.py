"""The XBT image's taker defaults must agree with the code defaults (JoinBlaket)."""

from __future__ import annotations

import re
from pathlib import Path

from jmcore.settings import TakerSettings

DOCKERFILE = Path(__file__).resolve().parents[1] / "xbt-docker" / "Dockerfile"


def _env(name: str) -> str:
    match = re.search(rf"\b{name}=(\S+)", DOCKERFILE.read_text())
    assert match, f"{name} not set in xbt-docker/Dockerfile"
    return match.group(1)


def test_image_minimum_makers_matches_code_default() -> None:
    # 0.43.0-xbt2: the code default became 1 but the image still forced 4.
    assert int(_env("TAKER__MINIMUM_MAKERS")) == TakerSettings().minimum_makers == 1


def test_image_keeps_bonded_only_makers() -> None:
    assert _env("TAKER__BONDLESS_MAKERS_ALLOWANCE") == "0"


def test_image_directories_all_nick_verified() -> None:
    # 0.43.0-xbt9: three JoinBlaket directories. Each one must have its
    # nick-auth id (its own lowercase onion:5222), or require_verified
    # would refuse it.
    import json

    from jmcore.settings import NetworkSettings

    ids = json.loads(_env("NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS").strip("'"))
    config = NetworkSettings(
        directory_servers=_env("NETWORK_CONFIG__DIRECTORY_SERVERS"),
        nick_auth_mode=_env("NETWORK_CONFIG__NICK_AUTH_MODE"),
        nick_auth_directory_ids=ids,
    )
    servers = config.directory_servers
    assert len(servers) == len(set(servers)) == 3
    assert servers[0].startswith("f3tfzgk5")
    assert set(servers) == set(config.nick_auth_directory_ids)
    for server in servers:
        assert server == server.lower() and server.endswith(".onion:5222")
        assert config.nick_auth_directory_ids[server] == server
