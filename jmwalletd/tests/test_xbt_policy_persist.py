"""JoinBlaket: Jam's fee limits ([POLICY] configset) survive a restart and a wallet lock."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from jmwalletd.state import DaemonState


def test_policy_saved_and_reloaded(tmp_path: Path) -> None:
    state = DaemonState(data_dir=tmp_path)
    state.config_overrides["POLICY"] = {"tx_fees": "6", "tx_fees_factor": "0.1"}
    state.config_overrides["OTHER"] = {"x": "1"}
    state.save_persisted_policy()
    path = tmp_path / "xbt-policy.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"POLICY": {"tx_fees": "6", "tx_fees_factor": "0.1"}}

    restarted = DaemonState(data_dir=tmp_path)
    assert restarted.config_overrides == {"POLICY": {"tx_fees": "6", "tx_fees_factor": "0.1"}}


def test_bad_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "xbt-policy.json").write_text("{not json")
    assert DaemonState(data_dir=tmp_path).config_overrides == {}
    (tmp_path / "xbt-policy.json").write_text('{"POLICY": ["no"]}')
    assert DaemonState(data_dir=tmp_path).config_overrides == {}
