"""
Test configuration for taker tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _taker_test_helpers import SAMPLE_MNEMONIC

from taker.podle_manager import initialize_commitment_history


@pytest.fixture(autouse=True)
def isolated_default_commitment_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful taker fixtures must not depend on the operator's real history."""
    data_dir = tmp_path / "default-taker-data"
    monkeypatch.setattr("jmcore.paths.get_default_data_dir", lambda: data_dir)
    initialize_commitment_history(data_dir)


@pytest.fixture
def sample_mnemonic() -> str:
    """Test mnemonic (not for production use!)."""
    return SAMPLE_MNEMONIC


# XBT build: there are no default directory servers and makers/takers refuse to
# start without one. Tests that build configs from default settings get a
# placeholder XBT directory here; tests that pass directories explicitly are
# unaffected (init/CLI values take precedence over the environment).
XBT_TEST_DIRECTORY = "xbttestdirectoryxbttestdirectoryxbttestdirectoryxbttestd.onion:5222"


@pytest.fixture(autouse=True)
def _xbt_test_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETWORK_CONFIG__DIRECTORY_SERVERS", XBT_TEST_DIRECTORY)
