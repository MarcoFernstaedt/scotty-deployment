"""Workflows a client declares, composed only from operations that exist.

Trent and Mikey should be able to say "when a new lead arrives, open a card and
remind me" and have that be a real, repeatable thing. What they must never be
able to do is grow the system by describing it: a workflow cannot add an
integration, a credential, a tool, a network destination, or any authority the
deployment did not already grant. Those are maintainer decisions, and a
definition that reaches for one is refused rather than partially honoured.

So a workflow is a declaration, validated whole before it can be activated. It
names installed operations only, carries its own limits, its own approval class,
its own idempotency and stop rules, and its own retention. It belongs to exactly
one client user, who is the only person who can read, revise, or run it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .policy import Role


class WorkflowError(ValueError):
    """A workflow definition or transition is not allowed, so nothing changes."""


class WorkflowState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


#: Exactly the operations a workflow step may name. Every one of them is an
#: operation this deployment already exposes, under the same policy and the
#: same approvals it has when a person asks for it directly.
INSTALLED_OPERATIONS: Mapping[str, str] = {
    "property_card.create": "open a property card",
    "property_card.update": "update a property card",
    "property_card.move": "move a property card to another list",
    "property_card.reformat": "tidy a property card without changing its values",
    "property_card.apply_template": "fill blank fields from an approved template",
    "property_card.duplicates": "check whether this property already has a card",
    "property_card.archive": "archive a property card",
    "reminder.create": "create a private reminder",
    "reminder.cancel": "cancel a private reminder",
    "discord.post_update": "post a progress update to a configured channel",
    "discord.announce": "publish to a configured announcement channel",
    "google.create_draft": "prepare an email draft for review",
    "google.create_event": "put an event on the calendar",
    "google.send_draft": "send an exact prepared email",
    "trello.list_cards": "read the configured board",
    "ghl.read_contact": "read a configured business contact",
    "ghl.send_sms": "send an exact prepared message",
    "rentcast.lookup": "look up a property valuation",
}

#: Steps whose effect is not freely reversible. A workflow containing one is a
#: consequence workflow, whatever its author wrote in the definition.
CONSEQUENCE_OPERATIONS = frozenset(
    {
        "property_card.archive",
        "discord.announce",
        "google.send_draft",
        "ghl.send_sms",
    }
)

APPROVAL_CLASSES = frozenset({"routine", "consequence"})

#: Keys that would let a definition grant itself something. Present at any
#: depth, the definition is refused: this is the boundary between describing
#: work and expanding the system.
_FORBIDDEN_KEYS = frozenset(
    {
        "credential",
        "credentials",
        "destination",
        "destinations",
        "integration",
        "integrations",
        "mcp",
        "model",
        "network",
        "permissions",
        "plugin",
        "plugins",
        "provider_account",
        "role",
        "scopes",
        "secret",
        "secrets",
        "skills",
        "token",
        "tools",
        "webhook",
    }
)

_REQUIRED_SECTIONS = (
    "name",
    "purpose",
    "trigger",
    "steps",
    "limits",
    "approval_class",
    "retries",
    "idempotency",
    "examples",
)

_REQUIRED_LIMITS = ("cards_per_run", "runs_per_day", "recipients")

MAX_DEFINITION_BYTES = 16_384
MAX_DEPTH = 8
MAX_STEPS = 20
MAX_TEXT_CHARS = 1_000
MAX_LIMIT = 1_000
MAX_RETENTION_DAYS = 3_650

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 '\-.,()]{0,79}")
_TRIGGER_KINDS = frozenset({"manual", "schedule", "new_lead", "card_moved", "message"})


def _text(value: object, label: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    if type(value) is not str or not value.strip():
        raise WorkflowError(f"{label} is required")
    text = value.strip()
    if len(text) > limit:
        raise WorkflowError(f"{label} is longer than {limit} characters")
    return text


def _depth(value: object, level: int = 0) -> int:
    if level > MAX_DEPTH:
        return level
    if isinstance(value, Mapping):
        return max((_depth(item, level + 1) for item in value.values()), default=level)
    if isinstance(value, list | tuple):
        return max((_depth(item, level + 1) for item in value), default=level)
    return level


def _reject_grants(value: object, level: int = 0) -> None:
    """Refuse anything that would hand the workflow new authority."""

    if level > MAX_DEPTH:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is str and key.casefold() in _FORBIDDEN_KEYS:
                raise WorkflowError(
                    f"a workflow cannot define {key}; that is a maintainer decision"
                )
            _reject_grants(item, level + 1)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _reject_grants(item, level + 1)


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One installed operation, with the arguments the author fixed."""

    operation: str
    arguments: Mapping[str, object]

    @property
    def explanation(self) -> str:
        return INSTALLED_OPERATIONS[self.operation]

    def as_json(self) -> dict[str, object]:
        return {"operation": self.operation, "arguments": dict(self.arguments)}


@dataclass(frozen=True, slots=True)
class Workflow:
    """One client user's declared workflow."""

    workflow_id: str
    owner: Role
    name: str
    purpose: str
    trigger: Mapping[str, object]
    steps: tuple[WorkflowStep, ...]
    limits: Mapping[str, int]
    approval_class: str
    schedule: Mapping[str, object]
    retries: Mapping[str, object]
    idempotency: Mapping[str, object]
    retention_days: int
    client_wording: str
    examples: tuple[Mapping[str, object], ...]
    state: WorkflowState = WorkflowState.DRAFT
    version: int = 1
    updated_at: str = ""

    def preview(self) -> dict[str, object]:
        """What this workflow would do, in the words its author will read.

        A preview carries no identifier a client should not see and no
        credential of any kind: it is the description, not the wiring.
        """

        return {
            "name": self.name,
            "purpose": self.purpose,
            "state": self.state.value,
            "approval_class": self.approval_class,
            "trigger": dict(self.trigger),
            "steps": [
                {"operation": step.operation, "explanation": step.explanation}
                for step in self.steps
            ],
            "limits": dict(self.limits),
            "schedule": dict(self.schedule),
            "retries": dict(self.retries),
            "idempotency": dict(self.idempotency),
            "retention_days": self.retention_days,
            "client_wording": self.client_wording,
            "version": self.version,
        }

    def as_json(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "owner": self.owner.value,
            "name": self.name,
            "purpose": self.purpose,
            "trigger": dict(self.trigger),
            "steps": [step.as_json() for step in self.steps],
            "limits": dict(self.limits),
            "approval_class": self.approval_class,
            "schedule": dict(self.schedule),
            "retries": dict(self.retries),
            "idempotency": dict(self.idempotency),
            "retention_days": self.retention_days,
            "client_wording": self.client_wording,
            "examples": [dict(item) for item in self.examples],
            "state": self.state.value,
            "version": self.version,
            "updated_at": self.updated_at,
        }


def _limits(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise WorkflowError("limits must name how much this workflow may do")
    missing = [name for name in _REQUIRED_LIMITS if name not in value]
    if missing:
        raise WorkflowError(f"limits must include {', '.join(missing)}")
    bounded: dict[str, int] = {}
    for name in _REQUIRED_LIMITS:
        entry = value[name]
        if type(entry) is not int or isinstance(entry, bool):
            raise WorkflowError(f"limit {name} must be a whole number")
        if entry < 0 or entry > MAX_LIMIT:
            raise WorkflowError(f"limit {name} must be between 0 and {MAX_LIMIT}")
        bounded[name] = entry
    if bounded["cards_per_run"] < 1:
        raise WorkflowError("a workflow that can touch no card does nothing")
    return bounded


def _steps(value: object) -> tuple[WorkflowStep, ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowError("a workflow needs at least one step")
    if len(value) > MAX_STEPS:
        raise WorkflowError(f"a workflow may have at most {MAX_STEPS} steps")
    steps: list[WorkflowStep] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise WorkflowError("each step must name an operation and its arguments")
        operation = entry.get("operation")
        if type(operation) is not str or operation not in INSTALLED_OPERATIONS:
            raise WorkflowError(
                "each step must name an installed operation; a workflow cannot introduce a new one"
            )
        arguments = entry.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise WorkflowError("step arguments must be an object")
        steps.append(WorkflowStep(operation=operation, arguments=dict(arguments)))
    return tuple(steps)


def _trigger(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowError("a workflow needs a trigger")
    kind = value.get("kind")
    if type(kind) is not str or kind not in _TRIGGER_KINDS:
        raise WorkflowError("the trigger kind is not one this deployment supports")
    return dict(value)


def _retries(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowError("a workflow needs a retry and stop rule")
    attempts = value.get("attempts")
    breaker = value.get("circuit_breaker")
    stop = value.get("stop_rule")
    if type(attempts) is not int or not 0 <= attempts <= 5:
        raise WorkflowError("attempts must be a whole number from 0 to 5")
    if type(breaker) is not int or not 1 <= breaker <= 20:
        raise WorkflowError("the circuit breaker must be a whole number from 1 to 20")
    if stop not in {"on_unknown", "on_failure", "never"}:
        raise WorkflowError("the stop rule must be on_unknown, on_failure, or never")
    if stop == "never":
        raise WorkflowError("a workflow must stop on something; 'never' is not a stop rule")
    return {"attempts": attempts, "circuit_breaker": breaker, "stop_rule": stop}


def _idempotency(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowError("a workflow needs an idempotency rule")
    key = value.get("key")
    on_duplicate = value.get("on_duplicate")
    if type(key) is not str or not key:
        raise WorkflowError("the idempotency key must name what makes a run unique")
    if on_duplicate not in {"skip", "reconcile"}:
        raise WorkflowError("on_duplicate must be skip or reconcile")
    return {"key": key, "on_duplicate": on_duplicate}


def _examples(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowError("a workflow needs at least one synthetic example")
    if len(value) > 10:
        raise WorkflowError("a workflow may carry at most ten examples")
    for entry in value:
        if not isinstance(entry, Mapping) or "input" not in entry or "expect" not in entry:
            raise WorkflowError("each example needs an input and what it should produce")
    return tuple(dict(entry) for entry in value)


#: Only a client user has workflows. The maintainer route is served separately
#: and never owns one, so a stored entry cannot claim that owner.
_OWNER_ROLES: frozenset[Role] = frozenset({Role.MAIN_OPERATOR, Role.EMPLOYEE})


def parse_workflow(definition: object, *, owner: Role, workflow_id: str | None = None) -> Workflow:
    """Validate one definition whole, or refuse it whole."""

    if owner not in _OWNER_ROLES:
        raise WorkflowError("only a client user owns a workflow")
    if not isinstance(definition, Mapping):
        raise WorkflowError("a workflow definition must be an object")
    try:
        encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("a workflow definition must be plain data") from exc
    if len(encoded.encode("utf-8")) > MAX_DEFINITION_BYTES:
        raise WorkflowError("this workflow definition is too large")
    if _depth(definition) > MAX_DEPTH:
        raise WorkflowError("this workflow definition is nested too deeply")
    _reject_grants(definition)
    missing = [name for name in _REQUIRED_SECTIONS if name not in definition]
    if missing:
        raise WorkflowError(f"the workflow is missing: {', '.join(missing)}")

    name = _text(definition.get("name"), "the workflow name", limit=80)
    if not _NAME.fullmatch(name):
        raise WorkflowError("the workflow name uses characters that are not allowed")
    steps = _steps(definition.get("steps"))
    approval_class = definition.get("approval_class")
    if approval_class not in APPROVAL_CLASSES:
        raise WorkflowError("the approval class must be routine or consequence")
    consequential = [step.operation for step in steps if step.operation in CONSEQUENCE_OPERATIONS]
    if consequential and approval_class != "consequence":
        raise WorkflowError(
            "this workflow does something that is not freely reversible ("
            + ", ".join(sorted(set(consequential)))
            + "), so its approval class must be consequence"
        )
    retention = definition.get("retention_days", 90)
    if type(retention) is not int or not 1 <= retention <= MAX_RETENTION_DAYS:
        raise WorkflowError("retention_days must be a whole number of days")
    schedule = definition.get("schedule", {})
    if not isinstance(schedule, Mapping):
        raise WorkflowError("the schedule must be an object")

    return Workflow(
        workflow_id=workflow_id or uuid.uuid4().hex,
        owner=owner,
        name=name,
        purpose=_text(definition.get("purpose"), "the workflow purpose"),
        trigger=_trigger(definition.get("trigger")),
        steps=steps,
        limits=_limits(definition.get("limits")),
        approval_class=approval_class,
        schedule=dict(schedule),
        retries=_retries(definition.get("retries")),
        idempotency=_idempotency(definition.get("idempotency")),
        retention_days=retention,
        client_wording=str(definition.get("client_wording", ""))[:MAX_TEXT_CHARS],
        examples=_examples(definition.get("examples")),
    )


#: Which state changes are allowed. Retirement is final: a retired workflow is
#: never resumed, so a stopped workflow cannot come back without being rewritten.
_TRANSITIONS: Mapping[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.DRAFT: frozenset({WorkflowState.ACTIVE, WorkflowState.RETIRED}),
    WorkflowState.ACTIVE: frozenset({WorkflowState.PAUSED, WorkflowState.RETIRED}),
    WorkflowState.PAUSED: frozenset({WorkflowState.ACTIVE, WorkflowState.RETIRED}),
    WorkflowState.RETIRED: frozenset(),
}


class WorkflowStore:
    """One client user's workflows, private to them.

    Ownership is checked on every read and every change, so one user's workflow
    is invisible to the other rather than merely unlisted.
    """

    def __init__(self, path: Path, *, owner_uid: int | None = 10000):
        self.path = path
        self.owner_uid = owner_uid

    def _raw_entries(self) -> list[Mapping[str, object]]:
        """Every stored entry, parseable or not.

        Kept separate from `_read_all` so that writing one user's workflow can
        never drop an entry this version happens not to understand — including
        the other user's.
        """

        if self.path.is_symlink() or not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, Mapping) or not isinstance(raw.get("workflows"), list):
            return []
        stored = raw["workflows"]
        assert isinstance(stored, list)  # noqa: S101 - checked immediately above
        return [entry for entry in stored if isinstance(entry, Mapping)]

    def _read_all(self) -> list[Workflow]:
        if self.path.is_symlink() or not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, Mapping):
            return []
        stored = raw.get("workflows")
        if not isinstance(stored, list):
            return []
        workflows: list[Workflow] = []
        for entry in stored:
            if not isinstance(entry, Mapping):
                continue
            try:
                owner = Role(str(entry.get("owner", "")))
                workflow = parse_workflow(
                    entry, owner=owner, workflow_id=str(entry.get("workflow_id", ""))
                )
            except (WorkflowError, ValueError):
                # A tampered or stale entry is dropped rather than trusted.
                continue
            state = str(entry.get("state", WorkflowState.DRAFT.value))
            version = entry.get("version", 1)
            workflows.append(
                replace(
                    workflow,
                    state=WorkflowState(state)
                    if state in set(WorkflowState)
                    else WorkflowState.DRAFT,
                    version=version if type(version) is int and version > 0 else 1,
                    updated_at=str(entry.get("updated_at", "")),
                )
            )
        return workflows

    def _write_all(self, workflows: Sequence[Workflow]) -> None:
        self._write_entries([item.as_json() for item in workflows])

    def _write_entries(self, entries: Sequence[Mapping[str, object]]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise WorkflowError("the workflow state path is unsafe")
        payload = json.dumps(
            {"workflows": [dict(entry) for entry in entries]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if self.owner_uid is not None:
                with suppress(OSError, PermissionError):
                    os.chown(temporary, self.owner_uid, self.owner_uid)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise WorkflowError("the workflow could not be stored") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def list(self, owner: Role) -> tuple[Workflow, ...]:
        return tuple(item for item in self._read_all() if item.owner == owner)

    def get(self, workflow_id: str, owner: Role) -> Workflow:
        for item in self._read_all():
            if item.workflow_id == workflow_id:
                if item.owner != owner:
                    # Not "forbidden": the other user's workflow is simply not
                    # something this user has.
                    raise WorkflowError("no such workflow")
                return item
        raise WorkflowError("no such workflow")

    def save(self, workflow: Workflow) -> Workflow:
        """Write one workflow, preserving every other entry byte for byte."""

        stored = replace(workflow, updated_at=datetime.now(UTC).isoformat())
        kept = [
            entry
            for entry in self._raw_entries()
            if str(entry.get("workflow_id", "")) != workflow.workflow_id
        ]
        self._write_entries([*kept, stored.as_json()])
        return stored

    def transition(self, workflow_id: str, owner: Role, state: WorkflowState) -> Workflow:
        current = self.get(workflow_id, owner)
        if state not in _TRANSITIONS[current.state]:
            raise WorkflowError(f"a {current.state.value} workflow cannot become {state.value}")
        return self.save(replace(current, state=state))

    def revise(self, workflow_id: str, owner: Role, revision: Workflow) -> Workflow:
        """Replace a definition, returning it to draft for review."""

        current = self.get(workflow_id, owner)
        if current.state is WorkflowState.RETIRED:
            raise WorkflowError("a retired workflow cannot be revised")
        return self.save(
            replace(
                revision,
                workflow_id=current.workflow_id,
                owner=current.owner,
                state=WorkflowState.DRAFT,
                version=current.version + 1,
            )
        )


__all__ = [
    "APPROVAL_CLASSES",
    "CONSEQUENCE_OPERATIONS",
    "INSTALLED_OPERATIONS",
    "MAX_STEPS",
    "Workflow",
    "WorkflowError",
    "WorkflowState",
    "WorkflowStep",
    "WorkflowStore",
    "parse_workflow",
]
