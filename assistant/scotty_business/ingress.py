from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from .config import RuntimeConfig
from .policy import (
    CODING_REFUSAL,
    EMPLOYEE_SUMMARY,
    FIXED_WIZARD_COMMAND,
    SETUP_WIZARD,
    Role,
    authorize_source,
)

EMPLOYEE_SUMMARY_COMMAND = "Scotty, send the employee summary."
CREDENTIAL_ROTATION_NOTICE = (
    "A credential may have been posted here. Scotty will not use or repeat it. "
    "Rotate it now, then enter the replacement only through the local hidden-input setup command."
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:api[_ -]?key|token|secret|password)\s*(?:is|=|:)\s*\S{12,}", re.I),
)
_CODING_PATTERNS = (
    re.compile(r"\bwrite\s+(?:some\s+)?code\b", re.I),
    re.compile(
        r"\b(?:build|create|modify)\s+(?:an?\s+)?(?:extension|integration|plugin|script)\b", re.I
    ),
    re.compile(r"\b(?:use|open)\s+(?:the\s+)?terminal\b", re.I),
    re.compile(r"\binstall\s+(?:a\s+)?package\b", re.I),
)


class IngressGuard:
    """Exact Discord tuple gate and fixed deterministic pre-model paths."""

    def __init__(
        self,
        config: RuntimeConfig,
        enqueue: Callable[[str, str], object],
    ) -> None:
        self.config = config
        self.enqueue = enqueue

    def __call__(self, event: object, **_: object) -> Mapping[str, str]:
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", None)
        if platform != "discord":
            return {"action": "skip", "reason": "unauthorized"}
        scope_id = getattr(source, "scope_id", None)
        guild_id = getattr(source, "guild_id", None)
        if scope_id is not None and guild_id is not None and scope_id != guild_id:
            return {"action": "skip", "reason": "unauthorized"}
        guild = scope_id if scope_id is not None else guild_id
        if getattr(source, "is_bot", None) is not False:
            return {"action": "skip", "reason": "unauthorized"}
        principal = authorize_source(
            self.config.principals,
            guild,
            getattr(source, "chat_id", None),
            getattr(source, "user_id", None),
            getattr(source, "parent_chat_id", None),
        )
        if principal is None:
            return {"action": "skip", "reason": "unauthorized"}
        text = getattr(event, "text", None)
        if type(text) is not str:
            return {"action": "skip", "reason": "malformed"}
        stripped = text.strip()
        if stripped.startswith("/"):
            return {"action": "skip", "reason": "commands-disabled"}
        if any(pattern.search(stripped) for pattern in _CREDENTIAL_PATTERNS):
            self.enqueue(principal.channel_id, CREDENTIAL_ROTATION_NOTICE)
            return {"action": "skip", "reason": "credential-redacted"}
        if any(pattern.search(stripped) for pattern in _CODING_PATTERNS):
            self.enqueue(principal.channel_id, CODING_REFUSAL)
            return {"action": "skip", "reason": "coding-refusal"}
        if stripped == FIXED_WIZARD_COMMAND:
            if principal.role == Role.MAINTAINER:
                destination = next(
                    item.channel_id
                    for item in self.config.principals
                    if item.role == Role.MAIN_OPERATOR
                )
                self.enqueue(destination, SETUP_WIZARD)
            return {"action": "skip", "reason": "fixed-wizard"}
        if stripped == EMPLOYEE_SUMMARY_COMMAND:
            destination = next(
                item.channel_id for item in self.config.principals if item.role == Role.EMPLOYEE
            )
            self.enqueue(destination, EMPLOYEE_SUMMARY)
            return {"action": "skip", "reason": "fixed-employee-summary"}
        return {"action": "allow"}
