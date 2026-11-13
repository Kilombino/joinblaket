"""Telegram notices for the maker, set from Jam (JoinBlaket).

The maker already sends operator notices through Apprise (``jmcore.notifications``):
an offer taken, a CoinJoin done, directories lost. These endpoints let Jam set the
Telegram bot that receives them, without a console, on every platform:

* ``GET    /xbt/telegram``       whether notices are on, and to which chat
* ``POST   /xbt/telegram``       save a bot token and a chat id, and turn notices on
* ``DELETE /xbt/telegram``       turn notices off
* ``POST   /xbt/telegram/test``  send a test notice now

The token and chat id are kept in ``<data_dir>/telegram.env`` (mode 600), which the
container entrypoint also reads at start, so the setting survives restarts. The
token is never returned by the API.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from jmwalletd.deps import get_daemon_state, require_auth

router = APIRouter()

# BotFather tokens are "<digits>:<35 url-safe characters>"; chats are a number
# (negative for groups) or an @channel name.
_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
_CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")


class TelegramSettings(BaseModel):
    bot_token: str = Field(min_length=1, max_length=128)
    chat_id: str = Field(min_length=1, max_length=40)


def _env_file() -> Path:
    return Path(get_daemon_state().data_dir) / "telegram.env"


def _read() -> tuple[str | None, str | None]:
    path = _env_file()
    if not path.exists():
        return None, None
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values.get("TELEGRAM_BOT_TOKEN"), values.get("TELEGRAM_CHAT_ID")


def _apply(token: str | None, chat_id: str | None) -> None:
    """Point the process-wide notifier at the bot (or switch it off) right away."""
    from jmcore.notifications import reset_notifier

    if token and chat_id:
        os.environ["NOTIFICATIONS__ENABLED"] = "true"
        os.environ["NOTIFICATIONS__URLS"] = f'["tgram://{token}/{chat_id}"]'
        os.environ.setdefault("NOTIFICATIONS__TITLE_PREFIX", "JoinBlaket")
    else:
        os.environ["NOTIFICATIONS__ENABLED"] = "false"
        os.environ.pop("NOTIFICATIONS__URLS", None)
    reset_notifier()


def load_saved_telegram() -> bool:
    """At start: turn the saved bot on (native installs too, not only the Docker entrypoint).

    A NOTIFICATIONS__URLS already in the environment (any Apprise URL, set by the operator or
    the container entrypoint) wins and is left alone. Returns True when the saved bot was applied.
    """
    if os.environ.get("NOTIFICATIONS__URLS"):
        return False
    try:
        token, chat_id = _read()
    except OSError:
        return False
    if not (token and chat_id):
        return False
    _apply(token, chat_id)
    return True


@router.get("/xbt/telegram", operation_id="xbttelegramget")
async def get_telegram(_auth: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    token, chat_id = _read()
    return {"enabled": bool(token and chat_id), "chat_id": chat_id if token else None}


@router.post("/xbt/telegram", operation_id="xbttelegramset")
async def set_telegram(
    body: TelegramSettings, _auth: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    token, chat_id = body.bot_token.strip(), body.chat_id.strip()
    if not _TOKEN_RE.match(token):
        raise HTTPException(
            status_code=400, detail="That does not look like a bot token from @BotFather."
        )
    if not _CHAT_RE.match(chat_id):
        raise HTTPException(
            status_code=400, detail="The chat id is a number (negative for groups) or an @channel."
        )
    if chat_id == token.split(":", 1)[0]:
        raise HTTPException(
            status_code=400,
            detail=(
                "That is the bot's own number (the start of the token). "
                "Put your own Telegram id: @userinfobot tells you."
            ),
        )
    path = _env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n")
    os.replace(tmp, path)
    _apply(token, chat_id)
    return {"enabled": True, "chat_id": chat_id}


@router.delete("/xbt/telegram", operation_id="xbttelegramdelete")
async def delete_telegram(_auth: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    path = _env_file()
    if path.exists():
        path.unlink()
    _apply(None, None)
    return {"enabled": False, "chat_id": None}


@router.post("/xbt/telegram/test", operation_id="xbttelegramtest")
async def test_telegram(_auth: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    token, chat_id = _read()
    if not (token and chat_id):
        raise HTTPException(status_code=400, detail="Save a bot token and a chat id first.")
    from jmcore.notifications import get_notifier

    _apply(token, chat_id)
    sent = await get_notifier().notify(
        "Test",
        "JoinBlaket notices reach this chat. You will hear here when someone takes your offer.",
    )
    if not sent:
        raise HTTPException(
            status_code=502,
            detail=(
                "Telegram did not accept the message: check the token, the chat id, "
                "and that you sent /start to the bot."
            ),
        )
    return {"sent": True}
