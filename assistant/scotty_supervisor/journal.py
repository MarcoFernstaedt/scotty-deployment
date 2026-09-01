"""Moving several files into place as one thing, or not at all.

Both defects this exists for had the same shape. A restore staged every file
and then replaced the live ones one at a time; a release install did the same.
Each replacement is atomic on its own, and the set of them was not — so a
failure on the second left the first replaced and the rest as they were, which
is a generation nobody designed, tested, or could name.

There is no way to rename many files at once. What there is, is a record: write
down what is about to happen, keep a copy of every byte being replaced, and
then do it. A process that dies partway leaves that record behind, and the next
one either finishes the move or puts every prior byte back. Either answer is a
whole generation; "half" is not among the outcomes.

The record lives beside the files it describes rather than in a temporary
directory, because the case it exists for is the one where the process that
knew about the temporary directory is gone.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

#: The record itself, and the directory holding the bytes it can put back.
JOURNAL_NAME = ".scotty-cutover.json"
KEEP_NAME = ".scotty-cutover"


class JournalError(RuntimeError):
    """A cutover could not be completed, and every prior byte was put back."""


class Journal:
    """One cutover's record, and the recovery that reads it back."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / JOURNAL_NAME
        self.keep = self.root / KEEP_NAME

    def __repr__(self) -> str:
        return f"Journal(root={self.root!s})"

    # -- writing ---------------------------------------------------------

    def begin(self, moves: Sequence[tuple[Path, Path]]) -> list[dict[str, str]]:
        """Record what is about to happen, and keep what it will overwrite.

        The kept copy is what makes a rollback possible: once a file has been
        replaced, the only place its previous bytes exist is here.
        """

        if self.path.exists():
            raise JournalError("a cutover is already in progress in this directory")
        self.keep.mkdir(mode=0o700, parents=True, exist_ok=True)
        entries: list[dict[str, str]] = []
        for source, target in moves:
            entry = {
                "source": str(source),
                "target": str(target),
                "kept": "",
                "existed": "no",
            }
            if target.exists() and not target.is_symlink():
                kept = self.keep / f"{uuid.uuid4().hex}"
                shutil.copy2(target, kept)
                entry["kept"] = str(kept)
                entry["existed"] = "yes"
            entries.append(entry)
        self._write({"started_at": datetime.now(UTC).isoformat(), "entries": entries})
        return entries

    def done(self) -> None:
        """The cutover finished. Nothing is left to put back."""

        with suppress(OSError):
            self.path.unlink(missing_ok=True)
        shutil.rmtree(self.keep, ignore_errors=True)

    def _write(self, body: dict[str, object]) -> None:
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self.root / f".{JOURNAL_NAME}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise JournalError("the cutover record could not be written") from exc

    # -- undoing ---------------------------------------------------------

    def rollback(self, entries: Iterable[dict[str, str]]) -> None:
        """Put every prior byte back, in the reverse of the order taken."""

        for entry in reversed(list(entries)):
            target = Path(entry["target"])
            if entry["existed"] == "yes" and entry["kept"]:
                with suppress(OSError):
                    os.replace(entry["kept"], target)
            elif entry["existed"] == "no":
                # There was nothing here before, so nothing is what goes back.
                with suppress(OSError):
                    target.unlink(missing_ok=True)
        self.done()

    def recover(self) -> tuple[str, ...]:
        """Undo a cutover whose process is gone. Returns what it put back.

        Called at startup. A journal that is still here means nobody finished,
        so the deployment goes back to the generation it was running -- which
        is a generation somebody accepted, unlike the half of two it has.
        """

        if self.path.is_symlink() or not self.path.is_file():
            return ()
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Unreadable: the kept bytes are still there, but nothing says
            # where they go. Leaving them is safer than guessing.
            raise JournalError("an interrupted cutover left an unreadable record") from None
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise JournalError("an interrupted cutover left a malformed record")
        restored = tuple(
            str(entry.get("target", ""))
            for entry in entries
            if isinstance(entry, dict) and entry.get("existed") == "yes"
        )
        self.rollback([entry for entry in entries if isinstance(entry, dict)])
        return restored


def replace_all(root: Path, moves: Sequence[tuple[Path, Path]]) -> tuple[str, ...]:
    """Move every staged file into place, or leave everything as it was.

    `root` is where the record lives; it should be the directory the targets
    are in, so that a recovery pass finds it without being told.
    """

    journal = Journal(root)
    entries = journal.begin(moves)
    moved: list[str] = []
    try:
        for entry in entries:
            os.replace(entry["source"], entry["target"])
            moved.append(entry["target"])
    except BaseException as exc:
        # Including KeyboardInterrupt and SystemExit: a cutover interrupted by
        # a signal is exactly the case a partial generation comes from.
        journal.rollback(entries)
        if isinstance(exc, OSError):
            # Reported as a failed cutover rather than a raw write error, so a
            # caller cannot mistake it for "some of it may have happened".
            raise JournalError("the cutover failed and was undone") from exc
        raise
    journal.done()
    return tuple(moved)


__all__ = ["JOURNAL_NAME", "KEEP_NAME", "Journal", "JournalError", "replace_all"]
