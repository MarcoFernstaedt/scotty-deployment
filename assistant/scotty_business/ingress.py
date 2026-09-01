from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

from .config import RuntimeConfig
from .credential_intake import INTAKE_COMMANDS, CredentialIntake
from .persona import resolve_persona
from .policy import (
    CODING_REFUSAL,
    FIXED_WIZARD_COMMAND,
    Role,
    employee_summary,
    setup_wizard,
)
from .routing import RouteKind, resolve_route
from .wizard import deliver_once, message_key

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
        marker_root: Path | None = None,
        *,
        intake: CredentialIntake | None = None,
        personas: Callable[[Role], str] | None = None,
    ) -> None:
        self.config = config
        self.enqueue = enqueue
        self.marker_root = marker_root
        self.intake = intake
        self.personas = personas

    def _assistant_name(self, role: Role) -> str:
        """What this reader's own assistant is called, never the other's."""

        if self.personas is not None:
            return self.personas(role)
        return resolve_persona(self.config, role).assistant_name

    def __call__(self, event: object, **_: object) -> Mapping[str, str]:
        source = getattr(event, "source", None)
        route = resolve_route(self.config, source)
        if route is None:
            return {"action": "skip", "reason": "unauthorized"}
        text = getattr(event, "text", None)
        if type(text) is not str:
            return {"action": "skip", "reason": "malformed"}
        stripped = text.strip()
        if self.intake is not None:
            # The protected intake runs first: an expected credential is consumed
            # here, before the leak scan, the queue, any session, and the model.
            if self.intake.intercept(event, route) is not None:
                return {"action": "skip", "reason": "credential-intake"}
            if stripped in INTAKE_COMMANDS:
                self.intake.open_window(route, stripped)
                return {"action": "skip", "reason": "credential-intake-open"}
        if any(pattern.search(stripped) for pattern in _CREDENTIAL_PATTERNS):
            # Credential text never reaches a model on any route.
            if route.principal is not None:
                self.enqueue(route.principal.channel_id, CREDENTIAL_ROTATION_NOTICE)
            return {"action": "skip", "reason": "credential-redacted"}
        if route.kind is RouteKind.MAINTAINER:
            if stripped == FIXED_WIZARD_COMMAND:
                self.send_wizard(event, stripped)
                return {"action": "skip", "reason": "fixed-wizard"}
            return {"action": "allow"}
        principal = route.principal
        if principal is None:
            # Unreachable for a client route; fail closed rather than assume.
            return {"action": "skip", "reason": "unauthorized"}
        if stripped.startswith("/"):
            return {"action": "skip", "reason": "commands-disabled"}
        if any(pattern.search(stripped) for pattern in _CODING_PATTERNS):
            self.enqueue(principal.channel_id, CODING_REFUSAL)
            return {"action": "skip", "reason": "coding-refusal"}
        if stripped == FIXED_WIZARD_COMMAND:
            # Only the exact maintainer tuple may trigger it. Every other
            # principal gets no wizard, no reply, and no disclosure.
            return {"action": "skip", "reason": "fixed-wizard"}
        if stripped == EMPLOYEE_SUMMARY_COMMAND:
            destination = next(
                item.channel_id for item in self.config.principals if item.role == Role.EMPLOYEE
            )
            self.enqueue(destination, employee_summary(self._assistant_name(Role.EMPLOYEE)))
            return {"action": "skip", "reason": "fixed-employee-summary"}
        return {"action": "allow"}

    def send_wizard(self, event: object, stripped: str) -> bool:
        """Deliver the fixed wizard to the configured main-operator channel only.

        The destination comes from private configuration, never from the model
        and never from the message. Delivery is claimed once per inbound
        message, so the root hook and the profile-local guard cannot both send.
        """

        destination = self.config.principal_for(Role.MAIN_OPERATOR).channel_id
        wizard = setup_wizard(self._assistant_name(Role.MAIN_OPERATOR))
        if self.marker_root is None:
            self.enqueue(destination, wizard)
            return True
        return deliver_once(
            self.marker_root,
            message_key(event, stripped),
            destination,
            wizard,
            self.enqueue,
        )
