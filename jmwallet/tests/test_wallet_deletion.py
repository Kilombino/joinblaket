"""Tests for fingerprint-scoped wallet deletion."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.cli_common import ResolvedBackendSettings
from typer.testing import CliRunner

from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    generate_wallet_name,
    get_mnemonic_fingerprint,
)
from jmwallet.backends.offline import OfflineBackend
from jmwallet.cli import app
from jmwallet.cli.mnemonic import save_mnemonic_file, save_mnemonic_meta
from jmwallet.history import (
    TransactionHistoryEntry,
    append_history_entry,
    delete_wallet_history_entries,
    read_history,
)
from jmwallet.history_state import ReconstructionCheckpoint
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    delete_wallet_registry_entries,
    get_legacy_registry_path,
    get_registry_path,
    load_registry,
    save_registry,
)
from jmwallet.wallet.deletion import (
    collect_neutrino_watch_addresses,
    delete_core_descriptor_wallet,
    local_wallet_artifact_paths,
)
from jmwallet.wallet.service import WalletService

MNEMONIC = "abandon " * 11 + "about"
OTHER_FINGERPRINT = "cafebabe"
runner = CliRunner()


def _bond(address: str, network: str = "mainnet") -> FidelityBondInfo:
    return FidelityBondInfo(
        address=address,
        locktime=1_800_000_000,
        locktime_human="2027-01-15 08:00:00",
        index=0,
        path="m/84'/0'/0'/2/0",
        pubkey="02" + "00" * 32,
        witness_script_hex="00" * 50,
        network=network,
        created_at="2026-01-01T00:00:00",
    )


def _owned_external_bond(wallet: WalletService, address: str) -> FidelityBondInfo:
    """Build a legacy registry entry owned through its explicit noncanonical path."""
    path = f"{wallet.root_path}/7/11"
    pubkey = wallet.master_key.derive(path).get_public_key_bytes(compressed=True).hex()
    return FidelityBondInfo(
        address=address,
        locktime=1_800_000_000,
        locktime_human="2027-01-15 08:00:00",
        index=0,
        path=path,
        pubkey=pubkey,
        witness_script_hex="00" * 50,
        network="regtest",
        created_at="2026-01-01T00:00:00",
    )


def test_delete_wallet_history_entries_preserves_other_and_legacy_rows(tmp_path: Path) -> None:
    for fingerprint, txid in [
        ("deadbeef", "mine-1"),
        (OTHER_FINGERPRINT, "other"),
        ("", "legacy"),
        ("deadbeef", "mine-2"),
    ]:
        append_history_entry(
            TransactionHistoryEntry(
                timestamp=f"2026-01-0{len(txid)}T00:00:00",
                txid=txid,
                wallet_fingerprint=fingerprint,
            ),
            tmp_path,
        )

    removed = delete_wallet_history_entries("DEADBEEF", tmp_path)

    assert removed == 2
    assert [(entry.wallet_fingerprint, entry.txid) for entry in read_history(tmp_path)] == [
        ("", "legacy"),
        (OTHER_FINGERPRINT, "other"),
    ]


def test_delete_wallet_registry_entries_filters_shared_legacy_file(tmp_path: Path) -> None:
    per_wallet = BondRegistry(bonds=[_bond("bc1qperwallet")])
    legacy = BondRegistry(bonds=[_bond("bc1qmine"), _bond("bc1qother")])
    save_registry(per_wallet, tmp_path, "deadbeef")
    save_registry(legacy, tmp_path)

    removed = delete_wallet_registry_entries(
        tmp_path,
        "deadbeef",
        lambda bond: bond.address == "bc1qmine",
    )

    assert removed == 2
    assert not get_registry_path(tmp_path, "deadbeef").exists()
    assert [bond.address for bond in load_registry(tmp_path).bonds] == ["bc1qother"]


def test_delete_wallet_registry_entries_fails_closed_on_invalid_file(tmp_path: Path) -> None:
    per_wallet_path = get_registry_path(tmp_path, "deadbeef")
    per_wallet_path.write_text("not json")
    legacy_path = get_legacy_registry_path(tmp_path)
    save_registry(BondRegistry(bonds=[_bond("bc1qkeep")]), tmp_path)

    with pytest.raises(ValueError, match="invalid bond registry"):
        delete_wallet_registry_entries(tmp_path, "deadbeef", lambda _bond: True)

    assert per_wallet_path.read_text() == "not json"
    assert legacy_path.exists()


@pytest.mark.asyncio
async def test_unload_wallet_for_deletion_disables_startup_loading() -> None:
    backend = DescriptorWalletBackend(wallet_name="jm_deadbeef_regtest")
    backend._rpc_call = AsyncMock(
        side_effect=[
            ["jm_deadbeef_regtest"],
            {},
        ]
    )

    removed = await backend.unload_wallet_for_deletion()

    assert removed is True
    assert backend._rpc_call.await_args_list[1].args == (
        "unloadwallet",
        ["jm_deadbeef_regtest", False],
    )
    assert backend._rpc_call.await_args_list[1].kwargs == {"use_wallet": False}


@pytest.mark.asyncio
async def test_delete_core_descriptor_wallet_unloads_before_removing_directory(
    tmp_path: Path,
) -> None:
    wallet_name = "jm_deadbeef_regtest"
    wallet_path = tmp_path / wallet_name
    wallet_path.mkdir()
    (wallet_path / "wallet.dat").write_text("watch-only")
    backend = MagicMock()
    backend.wallet_exists = AsyncMock(return_value=True)
    backend.unload_wallet_for_deletion = AsyncMock(return_value=True)
    backend.close = AsyncMock()
    backend_settings = ResolvedBackendSettings(
        network="regtest",
        bitcoin_network="regtest",
        backend_type="descriptor_wallet",
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        neutrino_url="",
        neutrino_add_peers=[],
        data_dir=tmp_path,
    )

    with patch("jmwallet.wallet.deletion.DescriptorWalletBackend", return_value=backend):
        removed_path = await delete_core_descriptor_wallet(
            backend_settings,
            wallet_name,
            tmp_path,
        )

    assert removed_path == wallet_path
    backend.unload_wallet_for_deletion.assert_awaited_once()
    assert not wallet_path.exists()
    backend.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_core_descriptor_wallet_rejects_mismatched_rpc_node(
    tmp_path: Path,
) -> None:
    wallet_name = "jm_deadbeef_regtest"
    wallet_path = tmp_path / wallet_name
    wallet_path.mkdir()
    backend = MagicMock()
    backend.wallet_exists = AsyncMock(return_value=False)
    backend.close = AsyncMock()
    backend_settings = ResolvedBackendSettings(
        network="regtest",
        bitcoin_network="regtest",
        backend_type="descriptor_wallet",
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        neutrino_url="",
        neutrino_add_peers=[],
        data_dir=tmp_path,
    )

    with (
        patch("jmwallet.wallet.deletion.DescriptorWalletBackend", return_value=backend),
        pytest.raises(ValueError, match="different Core instance"),
    ):
        await delete_core_descriptor_wallet(backend_settings, wallet_name, tmp_path)

    assert wallet_path.exists()
    backend.close.assert_awaited_once()


def test_local_artifacts_delete_mnemonic_last(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"

    paths = local_wallet_artifact_paths(tmp_path, mnemonic_file, "deadbeef")

    assert paths[-2] == tmp_path / "wallet.mnemonic.meta"
    assert paths[-1] == mnemonic_file


def test_collect_neutrino_watch_addresses_covers_wallet_state_deterministically(
    tmp_path: Path,
) -> None:
    wallet = WalletService(
        MNEMONIC,
        OfflineBackend(),
        network="regtest",
        mixdepth_count=1,
        gap_limit=6,
        scan_range=25,
    )
    fingerprint = wallet.wallet_fingerprint
    metadata_addresses = [wallet.get_address(0, 0, index) for index in range(43, 46)]
    metadata_path = tmp_path / f"wallet_metadata_{fingerprint}.jsonl"
    metadata_path.write_text(
        "\n".join(
            json.dumps({"type": "addr", "ref": address, "label": label})
            for address, label in zip(
                metadata_addresses,
                ["jm:used:deposit", "jm:reserved:customer", "jm:funded"],
                strict=True,
            )
        )
        + "\n"
    )
    checkpoint_path = tmp_path / "state" / f"history_reconstruction_{fingerprint}.json"
    checkpoint_path.parent.mkdir()
    checkpoint_path.write_text(
        ReconstructionCheckpoint(
            wallet_fingerprint=fingerprint,
            network="regtest",
            backend_id="jmwallet.backends.neutrino.NeutrinoBackend|http://neutrino.example",
            regular_branch_ends={"0:0": 26},
        ).model_dump_json()
    )
    per_wallet_address = wallet.get_address(0, 0, 40).upper()
    legacy_owned_address = wallet.get_address(0, 0, 41)
    foreign_wallet = WalletService(
        "legal winner thank year wave sausage worth useful legal winner thank yellow",
        OfflineBackend(),
        network="regtest",
        mixdepth_count=1,
    )
    legacy_foreign_address = foreign_wallet.get_address(0, 0, 0)
    save_registry(
        BondRegistry(bonds=[_bond(per_wallet_address, network="regtest")]),
        tmp_path,
        fingerprint,
    )
    save_registry(
        BondRegistry(
            bonds=[
                _owned_external_bond(wallet, legacy_owned_address),
                _bond(legacy_foreign_address, network="regtest"),
            ]
        ),
        tmp_path,
    )

    addresses = collect_neutrino_watch_addresses(
        data_dir=tmp_path,
        mnemonic=MNEMONIC,
        bip39_passphrase="",
        fingerprint=fingerprint,
        network="regtest",
        neutrino_url="http://neutrino.example",
        mixdepth_count=1,
        gap_limit=6,
        scan_range=25,
    )

    assert addresses == tuple(sorted(addresses))
    assert set(metadata_addresses) <= set(addresses)
    assert per_wallet_address.lower() in addresses
    assert per_wallet_address not in addresses
    assert legacy_owned_address in addresses
    assert legacy_foreign_address not in addresses
    assert wallet.get_address(0, 0, 26) in addresses
    assert wallet.get_address(0, 1, 24) in addresses
    assert wallet.get_address(0, 1, 25) not in addresses
    canonical_addresses = {
        wallet.get_fidelity_bond_address(index, 1_577_836_800 + index) for index in range(1)
    }
    # The known first canonical derivation is enough to prove that collection
    # uses the real WalletService fidelity-bond API; the aggregate proves all 960.
    assert canonical_addresses <= set(addresses)
    assert len(addresses) == 960 + 52 + len(metadata_addresses) + 2


def test_collect_neutrino_watch_addresses_ignores_valid_other_backend_checkpoint(
    tmp_path: Path,
) -> None:
    wallet = WalletService(
        MNEMONIC,
        OfflineBackend(),
        network="regtest",
        mixdepth_count=1,
        gap_limit=6,
        scan_range=25,
    )
    fingerprint = wallet.wallet_fingerprint
    checkpoint_path = tmp_path / "state" / f"history_reconstruction_{fingerprint}.json"
    checkpoint_path.parent.mkdir()
    checkpoint_path.write_text(
        ReconstructionCheckpoint(
            wallet_fingerprint=fingerprint,
            network="regtest",
            backend_id="jmwallet.backends.neutrino.NeutrinoBackend|http://other-neutrino.example",
            regular_branch_ends={"0:0": 1_000},
        ).model_dump_json()
    )

    addresses = collect_neutrino_watch_addresses(
        data_dir=tmp_path,
        mnemonic=MNEMONIC,
        bip39_passphrase="",
        fingerprint=fingerprint,
        network="regtest",
        neutrino_url="http://neutrino.example",
        mixdepth_count=1,
        gap_limit=6,
        scan_range=25,
    )

    assert wallet.get_address(0, 0, 24) in addresses
    assert wallet.get_address(0, 0, 25) not in addresses


def test_collect_neutrino_watch_addresses_honors_prior_mixdepth_coverage(
    tmp_path: Path,
) -> None:
    fingerprint = get_mnemonic_fingerprint(MNEMONIC)
    checkpoint_path = tmp_path / "state" / f"history_reconstruction_{fingerprint}.json"
    checkpoint_path.parent.mkdir()
    checkpoint_path.write_text(
        ReconstructionCheckpoint(
            wallet_fingerprint=fingerprint,
            network="regtest",
            backend_id="jmwallet.backends.neutrino.NeutrinoBackend|http://neutrino.example",
            regular_branch_ends={"2:1": 30},
        ).model_dump_json()
    )
    prior_wallet = WalletService(
        MNEMONIC,
        OfflineBackend(),
        network="regtest",
        mixdepth_count=3,
        gap_limit=6,
        scan_range=25,
    )

    addresses = collect_neutrino_watch_addresses(
        data_dir=tmp_path,
        mnemonic=MNEMONIC,
        bip39_passphrase="",
        fingerprint=fingerprint,
        network="regtest",
        neutrino_url="http://neutrino.example",
        mixdepth_count=1,
        gap_limit=6,
        scan_range=25,
    )

    assert prior_wallet.get_address(2, 1, 30) in addresses


def test_collect_neutrino_watch_addresses_rejects_invalid_checkpoint_and_metadata(
    tmp_path: Path,
) -> None:
    fingerprint = get_mnemonic_fingerprint(MNEMONIC)
    checkpoint_path = tmp_path / "state" / f"history_reconstruction_{fingerprint}.json"
    checkpoint_path.parent.mkdir()
    checkpoint_path.write_text("not JSON")

    kwargs = {
        "data_dir": tmp_path,
        "mnemonic": MNEMONIC,
        "bip39_passphrase": "",
        "fingerprint": fingerprint,
        "network": "regtest",
        "neutrino_url": "http://neutrino.example",
        "mixdepth_count": 1,
        "gap_limit": 6,
        "scan_range": 25,
    }
    with pytest.raises(ValueError, match="history reconstruction checkpoint"):
        collect_neutrino_watch_addresses(**kwargs)

    checkpoint_path.unlink()
    (tmp_path / f"wallet_metadata_{fingerprint}.jsonl").write_text(
        '{"type":"addr","ref":1,"label":"jm:used"}\n'
    )
    with pytest.raises(ValueError, match="wallet metadata address"):
        collect_neutrino_watch_addresses(**kwargs)


@pytest.mark.skip(reason="XBT build: neutrino backend is not supported")
def test_delete_cli_neutrino_removes_wallet_and_selected_shared_data(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallets" / "delete-me.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)
    fingerprint = get_mnemonic_fingerprint(MNEMONIC)
    metadata_path = tmp_path / f"wallet_metadata_{fingerprint}.jsonl"
    metadata_path.write_text("")
    checkpoint = tmp_path / "state" / f"history_reconstruction_{fingerprint}.json"
    append_history_entry(
        TransactionHistoryEntry(
            timestamp="2026-01-01T00:00:00",
            txid="mine",
            wallet_fingerprint=fingerprint,
        ),
        tmp_path,
    )
    append_history_entry(
        TransactionHistoryEntry(
            timestamp="2026-01-02T00:00:00",
            txid="other",
            wallet_fingerprint=OTHER_FINGERPRINT,
        ),
        tmp_path,
    )
    save_registry(BondRegistry(bonds=[_bond("bc1qmine")]), tmp_path, fingerprint)

    async def remove_watches(*_args: object) -> tuple[int, int]:
        assert mnemonic_file.exists()
        assert metadata_path.exists()
        return 3, 4

    with patch(
        "jmwallet.wallet.deletion.remove_neutrino_wallet_watches",
        new_callable=AsyncMock,
        side_effect=remove_watches,
    ) as remove_watches_mock:
        result = runner.invoke(
            app,
            [
                "delete",
                "--mnemonic-file",
                str(mnemonic_file),
                "--backend",
                "neutrino",
                "--network",
                "regtest",
                "--data-dir",
                str(tmp_path),
                "--delete-history",
                "--delete-bond-registry",
                "--yes",
            ],
        )

    assert result.exit_code == 0, result.output
    remove_watches_mock.assert_awaited_once()
    assert f"Deleted wallet {fingerprint}" in result.output
    assert "Removed Neutrino watched addresses: 3" in result.output
    assert "Removed Neutrino UTXOs: 4" in result.output
    assert not mnemonic_file.exists()
    assert not metadata_path.exists()
    assert not checkpoint.exists()
    assert [entry.wallet_fingerprint for entry in read_history(tmp_path)] == [OTHER_FINGERPRINT]
    assert not get_registry_path(tmp_path, fingerprint).exists()


@pytest.mark.skip(reason="XBT build: neutrino backend is not supported")
def test_delete_cli_neutrino_dry_run_derives_without_removing_watches(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)

    with patch(
        "jmwallet.wallet.deletion.remove_neutrino_wallet_watches",
        new_callable=AsyncMock,
    ) as remove_watches:
        result = runner.invoke(
            app,
            [
                "delete",
                "--mnemonic-file",
                str(mnemonic_file),
                "--backend",
                "neutrino",
                "--network",
                "regtest",
                "--data-dir",
                str(tmp_path),
                "--dry-run",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Neutrino watched addresses: remove " in result.output
    assert mnemonic_file.exists()
    remove_watches.assert_not_awaited()


def test_delete_cli_neutrino_uses_bitcoin_address_network(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)
    backend_settings = ResolvedBackendSettings(
        network="mainnet",
        bitcoin_network="regtest",
        backend_type="neutrino",
        rpc_url="",
        rpc_user="",
        rpc_password="",
        neutrino_url="http://localhost:8334",
        neutrino_add_peers=[],
        data_dir=tmp_path,
    )

    with (
        patch("jmwallet.cli.wallet.resolve_backend_settings", return_value=backend_settings),
        patch(
            "jmwallet.wallet.deletion.collect_neutrino_watch_addresses",
            return_value=("bcrt1qwatch",),
        ) as collect_watches,
        patch(
            "jmwallet.wallet.deletion.remove_neutrino_wallet_watches",
            new_callable=AsyncMock,
        ) as remove_watches,
    ):
        result = runner.invoke(
            app,
            [
                "delete",
                "--mnemonic-file",
                str(mnemonic_file),
                "--backend",
                "neutrino",
                "--data-dir",
                str(tmp_path),
                "--dry-run",
            ],
        )

    assert result.exit_code == 0, result.output
    assert collect_watches.call_args.kwargs["network"] == "regtest"
    remove_watches.assert_not_awaited()


@pytest.mark.skip(reason="XBT build: neutrino backend is not supported")
def test_delete_cli_neutrino_cleanup_failure_keeps_local_data(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)
    fingerprint = get_mnemonic_fingerprint(MNEMONIC)
    metadata_path = tmp_path / f"wallet_metadata_{fingerprint}.jsonl"
    metadata_path.write_text("")

    with patch(
        "jmwallet.wallet.deletion.remove_neutrino_wallet_watches",
        new_callable=AsyncMock,
        side_effect=RuntimeError("active rescan"),
    ):
        result = runner.invoke(
            app,
            [
                "delete",
                "--mnemonic-file",
                str(mnemonic_file),
                "--backend",
                "neutrino",
                "--network",
                "regtest",
                "--data-dir",
                str(tmp_path),
                "--yes",
            ],
        )

    assert result.exit_code == 1
    assert "local wallet data was not deleted" in result.output
    assert mnemonic_file.exists()
    assert metadata_path.exists()


def test_delete_cli_dry_run_keeps_everything_and_skips_core_rpc(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)
    fingerprint = get_mnemonic_fingerprint(MNEMONIC)
    wallet_name = generate_wallet_name(fingerprint, "regtest")
    core_wallet_dir = tmp_path / "core-wallets"
    core_wallet_path = core_wallet_dir / wallet_name
    core_wallet_path.mkdir(parents=True)

    with patch("jmwallet.wallet.deletion.DescriptorWalletBackend") as backend:
        result = runner.invoke(
            app,
            [
                "delete",
                "--mnemonic-file",
                str(mnemonic_file),
                "--backend",
                "descriptor_wallet",
                "--network",
                "regtest",
                "--data-dir",
                str(tmp_path),
                "--core-wallet-dir",
                str(core_wallet_dir),
                "--dry-run",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Dry run complete; nothing was deleted." in result.output
    assert mnemonic_file.exists()
    assert core_wallet_path.exists()
    backend.assert_not_called()


def test_delete_cli_requires_core_wallet_dir_or_explicit_keep(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)

    result = runner.invoke(
        app,
        [
            "delete",
            "--mnemonic-file",
            str(mnemonic_file),
            "--backend",
            "descriptor_wallet",
            "--network",
            "regtest",
            "--data-dir",
            str(tmp_path),
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "--core-wallet-dir" in result.output
    assert mnemonic_file.exists()


def test_delete_cli_keep_backend_wallet_only_removes_local_data(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)
    wallet_name = generate_wallet_name(get_mnemonic_fingerprint(MNEMONIC), "regtest")

    with patch("jmwallet.wallet.deletion.DescriptorWalletBackend") as backend:
        result = runner.invoke(
            app,
            [
                "delete",
                "--mnemonic-file",
                str(mnemonic_file),
                "--backend",
                "descriptor_wallet",
                "--network",
                "regtest",
                "--data-dir",
                str(tmp_path),
                "--keep-backend-wallet",
                "--yes",
            ],
        )

    assert result.exit_code == 0, result.output
    assert f"Kept Bitcoin Core wallet: {wallet_name}" in result.output
    assert not mnemonic_file.exists()
    backend.assert_not_called()


@pytest.mark.skip(reason="XBT build: neutrino backend is not supported")
def test_delete_cli_rejects_wrong_fingerprint_confirmation(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)

    result = runner.invoke(
        app,
        [
            "delete",
            "--mnemonic-file",
            str(mnemonic_file),
            "--backend",
            "neutrino",
            "--network",
            "regtest",
            "--data-dir",
            str(tmp_path),
        ],
        input="wrong\n",
    )

    assert result.exit_code == 1
    assert "Wallet deletion cancelled." in result.output
    assert mnemonic_file.exists()


def test_delete_cli_rejects_unknown_backend(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)

    result = runner.invoke(
        app,
        [
            "delete",
            "--mnemonic-file",
            str(mnemonic_file),
            "--backend",
            "typo",
            "--data-dir",
            str(tmp_path),
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "Unsupported backend" in result.output
    assert mnemonic_file.exists()


def test_delete_cli_rejects_mnemonic_meta_fingerprint_mismatch(tmp_path: Path) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, mnemonic_file, None)
    save_mnemonic_meta(mnemonic_file, fingerprint=get_mnemonic_fingerprint(MNEMONIC))

    result = runner.invoke(
        app,
        [
            "delete",
            "--mnemonic-file",
            str(mnemonic_file),
            "--backend",
            "neutrino",
            "--data-dir",
            str(tmp_path),
            "--yes",
        ],
        env={"BIP39_PASSPHRASE": "wrong passphrase"},
    )

    assert result.exit_code == 1
    assert "does not match" in result.output
    assert mnemonic_file.exists()
