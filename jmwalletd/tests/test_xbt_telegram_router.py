"""Tests for the JoinBlaket Telegram notice settings (/api/v1/xbt/telegram)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from jmwalletd.deps import get_daemon_state

TOKEN = "123456789:" + "A" * 35
URL = "/api/v1/xbt/telegram"


@pytest.fixture
def authed(app_with_wallet: TestClient, auth_token: str) -> tuple[TestClient, dict[str, str]]:
    return app_with_wallet, {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture(autouse=True)
def _clean_env():
    keys = ("NOTIFICATIONS__ENABLED", "NOTIFICATIONS__URLS", "NOTIFICATIONS__TITLE_PREFIX")
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _env_file() -> Path:
    return Path(get_daemon_state().data_dir) / "telegram.env"


def test_requires_auth(app_with_wallet: TestClient) -> None:
    assert app_with_wallet.get(URL).status_code == 401
    assert app_with_wallet.post(URL, json={"bot_token": TOKEN, "chat_id": "1"}).status_code == 401


def test_off_by_default(authed) -> None:
    client, h = authed
    assert client.get(URL, headers=h).json() == {"enabled": False, "chat_id": None}


def test_save_then_read_never_returns_token(authed) -> None:
    client, h = authed
    r = client.post(URL, headers=h, json={"bot_token": TOKEN, "chat_id": "-100123"})
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "chat_id": "-100123"}
    got = client.get(URL, headers=h).json()
    assert got == {"enabled": True, "chat_id": "-100123"}
    assert TOKEN not in client.get(URL, headers=h).text
    path = _env_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=-100123\n"
    assert os.environ["NOTIFICATIONS__URLS"] == f'["tgram://{TOKEN}/-100123"]'
    assert os.environ["NOTIFICATIONS__ENABLED"] == "true"


@pytest.mark.parametrize(
    "token,chat",
    [("not-a-token", "1"), (TOKEN, "chat"), (TOKEN + "/x", "1"), (TOKEN, "1/../x"), ("1:" + "A" * 35, "1")],
)
def test_rejects_malformed(authed, token: str, chat: str) -> None:
    client, h = authed
    r = client.post(URL, headers=h, json={"bot_token": token, "chat_id": chat})
    assert r.status_code == 400
    assert not _env_file().exists()


def test_delete_turns_off(authed) -> None:
    client, h = authed
    client.post(URL, headers=h, json={"bot_token": TOKEN, "chat_id": "@joinblaket_test"})
    r = client.delete(URL, headers=h)
    assert r.json() == {"enabled": False, "chat_id": None}
    assert not _env_file().exists()
    assert os.environ["NOTIFICATIONS__ENABLED"] == "false"
    assert "NOTIFICATIONS__URLS" not in os.environ


def test_test_message_needs_settings(authed) -> None:
    client, h = authed
    assert client.post(URL + "/test", headers=h).status_code == 400


def test_test_message_reports_result(authed) -> None:
    client, h = authed
    client.post(URL, headers=h, json={"bot_token": TOKEN, "chat_id": "42"})
    with patch("jmcore.notifications.Notifier.notify", new=AsyncMock(return_value=True)) as sent:
        assert client.post(URL + "/test", headers=h).json() == {"sent": True}
        sent.assert_awaited_once()
    with patch("jmcore.notifications.Notifier.notify", new=AsyncMock(return_value=False)):
        assert client.post(URL + "/test", headers=h).status_code == 502
