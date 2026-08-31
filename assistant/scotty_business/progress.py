"""Near-real-time work updates that cannot flood a Discord channel.

Scotty reports what it is doing while a task runs. Doing that with one message
per step would spam the channel, so a task keeps a single status message and
edits it in place: updates arriving inside the minimum interval are coalesced
into the latest text and sent as one edit when the interval elapses.

Every bound here is a hard bound. A task may post only a small number of
messages and make only a limited number of edits; past that, updates are still
coalesced but nothing further is sent until the task finishes, which posts one
final message. A failed send or edit never retries blindly: the reporter reports
the failure and stops writing rather than duplicating a status message.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

#: Never edit the status message more often than this.
MIN_EDIT_INTERVAL_SECONDS = 5.0

#: Hard ceilings for one task, whatever it asks for.
MAX_EDITS = 40
MAX_POSTS = 3
MAX_UPDATE_CHARS = 1900


class ProgressState(StrEnum):
    """Fixed vocabulary describing what the reporter did with an update."""

    POSTED = "posted"
    EDITED = "edited"
    COALESCED = "coalesced"
    CAPPED = "capped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProgressOutcome:
    state: ProgressState
    message_id: str | None = None


class ProgressReporter:
    """One coalesced, rate-limited status message per task."""

    def __init__(
        self,
        channel_id: str,
        send: Callable[[str, str], Mapping[str, str]],
        edit: Callable[[str, str, str], Mapping[str, str]],
        *,
        clock: Callable[[], float],
        min_interval: float = MIN_EDIT_INTERVAL_SECONDS,
        max_edits: int = MAX_EDITS,
        max_posts: int = MAX_POSTS,
    ) -> None:
        self.channel_id = channel_id
        self.send = send
        self.edit = edit
        self.clock = clock
        self.min_interval = max(float(min_interval), MIN_EDIT_INTERVAL_SECONDS)
        self.max_edits = min(int(max_edits), MAX_EDITS)
        self.max_posts = min(int(max_posts), MAX_POSTS)
        self.message_id: str | None = None
        self.posts = 0
        self.edits = 0
        self.pending: str | None = None
        self._last_write: float | None = None
        self._stopped = False

    @property
    def coalesced(self) -> str | None:
        """The latest update not yet written, if any."""

        return self.pending

    def _bounded(self, text: object) -> str | None:
        if type(text) is not str:
            return None
        stripped = text.strip()
        if not stripped:
            return None
        return stripped[:MAX_UPDATE_CHARS]

    def _due(self) -> bool:
        if self._last_write is None:
            return True
        return (self.clock() - self._last_write) >= self.min_interval

    def update(self, text: object) -> ProgressOutcome:
        """Record one update, writing it only when the rate limit allows."""

        bounded = self._bounded(text)
        if bounded is None or self._stopped:
            return ProgressOutcome(ProgressState.UNAVAILABLE, self.message_id)
        self.pending = bounded
        if not self._due():
            return ProgressOutcome(ProgressState.COALESCED, self.message_id)
        return self.flush()

    def flush(self) -> ProgressOutcome:
        """Write the coalesced update, if there is one and budget remains."""

        pending = self.pending
        if pending is None or self._stopped:
            return ProgressOutcome(ProgressState.UNAVAILABLE, self.message_id)
        if self.message_id is None:
            if self.posts >= self.max_posts:
                return ProgressOutcome(ProgressState.CAPPED, self.message_id)
            return self._write(ProgressState.POSTED, pending)
        if self.edits >= self.max_edits:
            # Keep coalescing, but stop writing until the task finishes.
            return ProgressOutcome(ProgressState.CAPPED, self.message_id)
        return self._write(ProgressState.EDITED, pending)

    def _write(self, state: ProgressState, text: str) -> ProgressOutcome:
        try:
            if state is ProgressState.POSTED:
                acknowledgement = self.send(self.channel_id, text)
                message_id = acknowledgement.get("message_id")
                if type(message_id) is not str or not message_id:
                    raise ValueError("ambiguous status acknowledgement")
                self.message_id = message_id
                self.posts += 1
            else:
                assert self.message_id is not None
                self.edit(self.channel_id, self.message_id, text)
                self.edits += 1
        except Exception:
            # An ambiguous or failed write is never retried into a duplicate.
            self._stopped = True
            return ProgressOutcome(ProgressState.UNAVAILABLE, self.message_id)
        self.pending = None
        self._last_write = self.clock()
        return ProgressOutcome(state, self.message_id)

    def finish(self, text: object) -> ProgressOutcome:
        """Write the final state once, ignoring the interval but not the caps."""

        bounded = self._bounded(text)
        if bounded is None or self._stopped:
            return ProgressOutcome(ProgressState.UNAVAILABLE, self.message_id)
        self.pending = bounded
        if self.message_id is not None:
            # The final write always lands, so a finished task never reads as
            # still running just because the edit budget ran out mid-task.
            return self._write(ProgressState.EDITED, bounded)
        if self.posts >= self.max_posts:
            return ProgressOutcome(ProgressState.CAPPED, self.message_id)
        return self._write(ProgressState.POSTED, bounded)
