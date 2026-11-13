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
