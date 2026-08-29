from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import RuntimeConfig
from .policy import Principal, authorize_source


class IdentityError(RuntimeError):
    """A tool call cannot be bound to an exact authorized Discord origin."""


class AuthorizedPrincipalResolver:
    """Resolve immutable gateway session provenance from the pinned state DB."""

    def __init__(self, hermes_home: str | Path, config: RuntimeConfig):
        self.home = Path(hermes_home)
        self.config = config

    def resolve(self, session_id: object) -> Principal:
        if type(session_id) is not str or not session_id or len(session_id) > 256:
            raise IdentityError("session identity is missing or malformed")
        state_path = self.home / "state.db"
        if state_path.is_symlink() or not state_path.is_file():
            raise IdentityError("session authority is unavailable")
        try:
            connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True, timeout=2.0)
            try:
                row = connection.execute(
                    "SELECT origin_json FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise IdentityError("session authority lookup failed") from exc
        if row is None or type(row[0]) is not str or len(row[0]) > 65_536:
            raise IdentityError("session origin is unavailable")
        try:
            origin = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise IdentityError("session origin is malformed") from exc
        if not isinstance(origin, dict) or origin.get("platform") != "discord":
            raise IdentityError("session is not an authorized Discord session")
        guild_id = origin.get("scope_id") or origin.get("guild_id")
        legacy_guild = origin.get("guild_id")
        if (
            type(guild_id) is not str
            or not guild_id
            or (legacy_guild is not None and legacy_guild != guild_id)
        ):
            raise IdentityError("session guild identity is malformed")
        principal = authorize_source(
            self.config.principals,
            guild_id,
            origin.get("chat_id"),
            origin.get("user_id"),
            origin.get("parent_chat_id"),
        )
        if principal is None:
            raise IdentityError("session principal tuple is unauthorized")
        return principal
