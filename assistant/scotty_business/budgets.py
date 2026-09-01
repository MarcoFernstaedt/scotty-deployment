"""How much this deployment is allowed to do, and when.

Failing safely is not enough on its own. An assistant that retries a broken
provider forever, sends at three in the morning, or works through a thousand
cards because someone asked it to is a reliability problem even when every
individual action is authorized.

So every kind of action has a configured hourly and daily budget, counted
against the exact actor who asked rather than the deployment as a whole; quiet
hours defer work that can wait, without deferring an incident alert to the
maintainer; and a provider that keeps failing has its breaker opened so the next
caller is told it is unavailable instead of joining the pile-up. Nothing here is
implicit: a limit that is not configured is the documented default, and a
configuration that would remove a limit is refused.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .policy import Principal


class BudgetError(ValueError):
    """A budget configuration is not usable, so the defaults are not replaced."""


@dataclass(frozen=True, slots=True)
class Limit:
    per_hour: int
    per_day: int


#: The default budget for each kind of action. Chosen to be generous for
#: ordinary work and firm about anything that leaves the deployment.
DEFAULT_BUDGETS: Mapping[str, Limit] = {
    # Reversible work inside the deployment.
    "provider_read": Limit(per_hour=300, per_day=2_000),
    "card_write": Limit(per_hour=120, per_day=600),
    "workspace_write": Limit(per_hour=120, per_day=600),
    "chat_message": Limit(per_hour=120, per_day=800),
    # Work that reaches someone outside it.
    "external_send": Limit(per_hour=20, per_day=60),
    "announcement": Limit(per_hour=5, per_day=20),
    "administration": Limit(per_hour=20, per_day=60),
    # Reserved for telling the maintainer something broke.
    "incident_alert": Limit(per_hour=6, per_day=24),
}

#: Actions that must reach the maintainer whatever the hour. An incident that
#: waits until morning is an incident nobody handled.
URGENT_ACTIONS = frozenset({"incident_alert"})

MAX_LIMIT = 10_000
DEFAULT_BREAKER_THRESHOLD = 5
DEFAULT_BREAKER_COOLDOWN_MINUTES = 30
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether one action may proceed, and why not when it may not."""

    allowed: bool
    reason: str = ""
    deferred: bool = False
    remaining_today: int = 0
    remaining_this_hour: int = 0

    def as_json(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "deferred": self.deferred,
            "remaining_today": self.remaining_today,
            "remaining_this_hour": self.remaining_this_hour,
        }


@dataclass(frozen=True, slots=True)
class BreakerState:
    open: bool
    reason: str = ""
    failures: int = 0


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """The configured limits. Every field has a default; none can be removed."""

    limits: Mapping[str, Limit]
    quiet_hours: tuple[int, int] | None
    breaker_threshold: int
    breaker_cooldown: timedelta
    retention_days: int

    @classmethod
    def load(cls, path: Path | str, *, owner_uid: int | None = None) -> BudgetPolicy:
        """Read the configured budgets, or use the declared defaults.

        The runtime built its policy from an empty mapping, so this file was
        never read at all and the defaults were the only limits that existed --
        which was invisible, because the defaults are reasonable.

        A file the model-facing account could write is not a limit, so when the
        deployment says which account that is, a writable file is refused
        rather than trusted.
        """

        target = Path(path)
        if target.is_symlink():
            raise BudgetError("the budget configuration path is unsafe")
        if not target.is_file():
            return cls.from_mapping({})
        metadata = target.stat()
        if owner_uid is not None and (metadata.st_uid == owner_uid or metadata.st_mode & 0o022):
            raise BudgetError(
                "the budget configuration is writable by the runtime, so it is not a limit"
            )
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BudgetError("the budget configuration could not be read") from exc
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: object) -> BudgetPolicy:
        if not isinstance(raw, Mapping):
            raise BudgetError("budget configuration must be an object")
        limits = dict(DEFAULT_BUDGETS)
        reserved = {
            "quiet_hours",
            "breaker_threshold",
            "breaker_cooldown_minutes",
            "retention_days",
        }
        for name, entry in raw.items():
            if name in reserved:
                continue
            if name not in DEFAULT_BUDGETS:
                raise BudgetError(f"{name} is not an action this deployment budgets")
            if not isinstance(entry, Mapping):
                raise BudgetError(f"the budget for {name} must be an object")
            limits[name] = _limit(name, entry)
        return cls(
            limits=limits,
            quiet_hours=_quiet_hours(raw.get("quiet_hours")),
            breaker_threshold=_bounded(
                raw.get("breaker_threshold", DEFAULT_BREAKER_THRESHOLD),
                "breaker_threshold",
                1,
                100,
            ),
            breaker_cooldown=timedelta(
                minutes=_bounded(
                    raw.get("breaker_cooldown_minutes", DEFAULT_BREAKER_COOLDOWN_MINUTES),
                    "breaker_cooldown_minutes",
                    1,
                    1_440,
                )
            ),
            retention_days=_bounded(
                raw.get("retention_days", DEFAULT_RETENTION_DAYS),
                "retention_days",
                1,
                MAX_RETENTION_DAYS,
            ),
        )

    def limit(self, action: str) -> Limit:
        if action not in self.limits:
            raise BudgetError(f"{action} is not an action this deployment budgets")
        return self.limits[action]

    def in_quiet_hours(self, at: datetime) -> bool:
        """Whether this instant falls inside the configured quiet window."""

        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours
        hour = at.astimezone(UTC).hour
        if start <= end:
            return start <= hour < end
        # A window that crosses midnight, which is the usual case.
        return hour >= start or hour < end


def _bounded(value: object, label: str, low: int, high: int) -> int:
    if type(value) is not int or isinstance(value, bool) or not low <= value <= high:
        raise BudgetError(f"{label} must be a whole number from {low} to {high}")
    return value


def _limit(name: str, entry: Mapping[str, object]) -> Limit:
    per_day = _bounded(entry.get("per_day"), f"{name}.per_day", 1, MAX_LIMIT)
    per_hour = _bounded(entry.get("per_hour"), f"{name}.per_hour", 1, MAX_LIMIT)
    if per_hour > per_day:
        raise BudgetError(f"{name}.per_hour cannot exceed {name}.per_day")
    return Limit(per_hour=per_hour, per_day=per_day)


def _quiet_hours(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise BudgetError("quiet_hours must be a start and an end hour")
    start, end = (_bounded(item, "quiet_hours", 0, 23) for item in value)
    if start == end:
        raise BudgetError("quiet_hours cannot cover the whole day")
    return (start, end)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS spending (
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    day TEXT NOT NULL,
    hour TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spending_lookup ON spending (actor, action, day, hour);
CREATE TABLE IF NOT EXISTS breakers (
    provider TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    opened_at TEXT NOT NULL DEFAULT ''
);
"""


class BudgetLedger:
    """Durable spending counts and breaker state, per actor and per provider."""

    def __init__(self, path: Path | str, policy: BudgetPolicy):
        self.path = str(path)
        self.policy = policy

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    def spend(self, actor: Principal, action: str, *, at: datetime | None = None) -> Decision:
        """Count one action against this actor's budget, or explain the refusal.

        A refusal costs nothing: the action is not counted, so a caller who
        waits for the next hour has their full budget rather than a spent one.
        """

        limit = self.policy.limit(action)
        moment = (at or datetime.now(UTC)).astimezone(UTC)
        if action not in URGENT_ACTIONS and self.policy.in_quiet_hours(moment):
            return Decision(
                allowed=False,
                deferred=True,
                reason="this is inside the configured quiet hours; it will wait until they end",
            )
        day = moment.date().isoformat()
        hour = moment.strftime("%Y-%m-%dT%H")
        key = _actor_key(actor)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            today = connection.execute(
                "SELECT COUNT(*) FROM spending WHERE actor = ? AND action = ? AND day = ?",
                (key, action, day),
            ).fetchone()[0]
            this_hour = connection.execute(
                "SELECT COUNT(*) FROM spending WHERE actor = ? AND action = ? AND hour = ?",
                (key, action, hour),
            ).fetchone()[0]
            if this_hour >= limit.per_hour:
                connection.execute("COMMIT")
                return Decision(
                    allowed=False,
                    reason=f"the hourly limit for {action} is spent",
                    remaining_today=max(limit.per_day - today, 0),
                )
            if today >= limit.per_day:
                connection.execute("COMMIT")
                return Decision(
                    allowed=False,
                    reason=f"the daily limit for {action} is spent",
                    remaining_this_hour=max(limit.per_hour - this_hour, 0),
                )
            connection.execute(
                "INSERT INTO spending (actor, action, day, hour, at) VALUES (?, ?, ?, ?, ?)",
                (key, action, day, hour, moment.isoformat()),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()
        return Decision(
            allowed=True,
            remaining_today=limit.per_day - today - 1,
            remaining_this_hour=limit.per_hour - this_hour - 1,
        )

    def record_failure(self, provider: str, *, at: datetime | None = None) -> None:
        moment = (at or datetime.now(UTC)).astimezone(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT failures FROM breakers WHERE provider = ?", (provider,)
            ).fetchone()
            failures = (row["failures"] if row else 0) + 1
            opened = moment.isoformat() if failures >= self.policy.breaker_threshold else ""
            connection.execute(
                "INSERT INTO breakers (provider, failures, opened_at) VALUES (?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET failures = ?, opened_at = ?",
                (provider, failures, opened, failures, opened),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def record_success(self, provider: str, *, at: datetime | None = None) -> None:
        """One success closes the breaker: the provider is answering again."""

        del at
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO breakers (provider, failures, opened_at) VALUES (?, 0, '') "
                "ON CONFLICT(provider) DO UPDATE SET failures = 0, opened_at = ''",
                (provider,),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def breaker(self, provider: str, *, at: datetime | None = None) -> BreakerState:
        """Whether this provider is currently held open, and why."""

        moment = (at or datetime.now(UTC)).astimezone(UTC)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT failures, opened_at FROM breakers WHERE provider = ?", (provider,)
            ).fetchone()
        finally:
            connection.close()
        if row is None or not row["opened_at"]:
            return BreakerState(open=False, failures=row["failures"] if row else 0)
        opened = datetime.fromisoformat(row["opened_at"])
        if moment - opened >= self.policy.breaker_cooldown:
            # The cooldown has passed, so the next caller may try again. One
            # failure after that reopens it immediately.
            return BreakerState(open=False, failures=row["failures"])
        return BreakerState(
            open=True,
            failures=row["failures"],
            reason=f"{provider} has failed repeatedly and is being left alone to recover",
        )

    def prune(self, *, at: datetime | None = None) -> int:
        """Drop spending records past the retention window."""

        moment = (at or datetime.now(UTC)).astimezone(UTC)
        cutoff = (moment - timedelta(days=self.policy.retention_days)).date().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM spending WHERE day < ?", (cutoff,))
            removed = cursor.rowcount
            connection.execute("COMMIT")
        finally:
            connection.close()
        return max(removed, 0)


def _actor_key(actor: Principal) -> str:
    """Budgets are per person, so the key is the authenticated actor."""

    return f"{actor.role.value}:{actor.user_id}"


__all__ = [
    "DEFAULT_BREAKER_THRESHOLD",
    "DEFAULT_BUDGETS",
    "DEFAULT_RETENTION_DAYS",
    "URGENT_ACTIONS",
    "BreakerState",
    "BudgetError",
    "BudgetLedger",
    "BudgetPolicy",
    "Decision",
    "Limit",
]
