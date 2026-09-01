"""The operator's own way in to supervision, backup, restore and rollback.

Every one of these is reachable from the installed command. A helper only the
tests call is not a capability the operator has, so the command surface and the
implementation are the same code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .releases import (
    ReleaseError,
    current_release,
    install_release,
    rollback,
    select_release,
    verify_release,
)
from .state import (
    StateError,
    backup_state,
    restorable,
    restore_state,
    verify_backup,
)
from .supervise import DockerContainer, Supervisor

CONTAINER = "scotty"
DEPLOYMENT_DIR = Path("/srv/Scotty")
START_COMMAND = "/usr/local/sbin/scotty-start"
STATE_DIR = Path("/srv/Scotty/data/scotty")
SUPERVISOR_DIR = Path("/var/lib/scotty/supervisor")
BACKUP_DIR = Path("/var/lib/scotty/backups")
RELEASES_DIR = Path("/var/lib/scotty/releases")
WATCH_INTERVAL_SECONDS = 15.0


def _run(command: Sequence[str]) -> tuple[int, str]:
    executable = shutil.which(command[0])
    if executable is None:
        return 127, ""
    result = subprocess.run(  # noqa: S603 - executable resolved by shutil.which
        [executable, *command[1:]], check=False, capture_output=True, text=True, timeout=60
    )
    return result.returncode, result.stdout


def _alert(kind: str, text: str) -> None:
    """One line to the journal. The runtime relays incidents to the maintainer.

    Writing here rather than sending from this process keeps the supervisor
    free of any credential: it has no Discord token and needs none.
    """

    print(f"scotty-supervisor: {kind}: {text}", file=sys.stderr)


def _integrity() -> bool:
    """Whether the selected release still matches what was recorded."""

    name = current_release(RELEASES_DIR)
    if not name:
        # Nothing has been published through the release lifecycle yet, so
        # there is nothing to contradict.
        return True
    try:
        return not verify_release(RELEASES_DIR / name)
    except ReleaseError:
        return False


def _supervisor() -> Supervisor:
    return Supervisor(
        DockerContainer(CONTAINER, _run),
        SUPERVISOR_DIR,
        alert=_alert,
        integrity=_integrity,
    )


def _named_backup(name: str) -> Path:
    candidate = (BACKUP_DIR / name).resolve()
    if candidate.parent != BACKUP_DIR.resolve():
        raise StateError("that is not the name of a backup")
    return candidate


def run(argv: Sequence[str]) -> int:
    command, *rest = argv
    try:
        if command == "watch":
            return _watch()
        if command == "once":
            print(json.dumps(_supervisor().tick().as_json(), indent=2))
            return 0
        if command == "status":
            supervisor = _supervisor()
            print(
                json.dumps(
                    {
                        "container": CONTAINER,
                        "running": DockerContainer(CONTAINER, _run).is_running(),
                        "current_release": current_release(RELEASES_DIR),
                        "integrity_ok": _integrity(),
                        "supervision": supervisor.state().as_json(),
                    },
                    indent=2,
                )
            )
            return 0
        if command == "hold":
            _supervisor().hold(" ".join(rest) or "held by the operator")
            return 0
        if command == "release":
            _supervisor().release()
            return 0
        if command == "backup":
            destination = BACKUP_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            backup_state(STATE_DIR, destination)
            mismatched = verify_backup(destination)
            print(
                json.dumps(
                    {
                        "backup": destination.name,
                        "files": list(restorable(destination)),
                        "intact": not mismatched,
                    },
                    indent=2,
                )
            )
            return 0 if not mismatched else 1
        if command == "verify":
            if not rest:
                print("scotty-supervisor: verify needs a backup name", file=sys.stderr)
                return 2
            mismatched = verify_backup(_named_backup(rest[0]))
            print(json.dumps({"intact": not mismatched, "mismatched": list(mismatched)}))
            return 0 if not mismatched else 1
        if command == "restore":
            if not rest:
                print("scotty-supervisor: restore needs a backup name", file=sys.stderr)
                return 2
            restored = restore_state(_named_backup(rest[0]), STATE_DIR)
            print(json.dumps({"restored": list(restored)}, indent=2))
            return 0
        if command == "rollback":
            return _rollback(execute="--execute" in rest)
        if command == "uninstall":
            return _uninstall()
    except (StateError, ReleaseError) as exc:
        print(f"scotty-supervisor: {exc}", file=sys.stderr)
        return 1
    print(f"scotty-supervisor: unknown command {command}", file=sys.stderr)
    return 2


def _rollback(*, execute: bool) -> int:
    """Name the accepted release to go back to, and on request actually go.

    Without `--execute` this reports and touches nothing, so an operator can
    see where a rollback would land before committing to one.

    With it, the order is the safety property: the container stops and is
    proven stopped before a release is selected, because two processes holding
    one Discord bot token is exactly the failure a rollback must not cause.
    Supervision is held for the whole operation so the watch loop cannot start
    the old container back up halfway through, and the hold is lifted only when
    the new release is up. A rollback that failed leaves it held, which is what
    a person needs to find rather than a deployment quietly flapping.
    """

    plan = rollback(RELEASES_DIR)
    if not execute:
        print(json.dumps(plan.as_json(), indent=2))
        return 0
    report: dict[str, object] = dict(plan.as_json())
    if not plan.available:
        report["state"] = "failed"
        print(json.dumps(report, indent=2))
        return 1

    supervisor = _supervisor()
    supervisor.hold(f"rolling back to {plan.target}")
    container = DockerContainer(CONTAINER, _run)
    if container.is_running():
        container.stop()
        if container.is_running():
            # Never select a release while something may still be consuming
            # Discord on the old one.
            report["state"] = "failed"
            report["reason"] = "the running container did not stop, so nothing was changed"
            print(json.dumps(report, indent=2))
            return 1

    select_release(RELEASES_DIR, plan.target)
    report["installed"] = list(install_release(RELEASES_DIR, plan.target, DEPLOYMENT_DIR))
    report["current_release"] = current_release(RELEASES_DIR)

    status, _ = _run([START_COMMAND])
    if not container.is_running():
        # The start may have half-happened. Saying "unknown" and stopping is the
        # only honest answer; starting again could be the second consumer.
        report["state"] = "unknown"
        report["reason"] = (
            f"{plan.target} is selected and in place, but the container is not "
            f"running after the start returned {status}; reconcile before retrying"
        )
        _alert("rollback", str(report["reason"]))
        print(json.dumps(report, indent=2))
        return 1
    supervisor.release()
    report["state"] = "verified"
    print(json.dumps(report, indent=2))
    return 0


def _watch() -> int:
    """Supervise until systemd stops us. One decision per interval."""

    import time

    supervisor = _supervisor()
    while True:
        decision = supervisor.tick()
        if decision.action not in {"none", "waiting", "held"}:
            print(f"scotty-supervisor: {decision.action}: {decision.reason}", file=sys.stderr)
        time.sleep(WATCH_INTERVAL_SECONDS)


def _uninstall() -> int:
    """Remove what this product installed, and nothing else on the host."""

    removed: list[str] = []
    for unit in (
        "scotty-supervisor.service",
        "scotty-credential-broker.service",
        "scotty-egress-guard.service",
    ):
        status, _ = _run(["systemctl", "disable", "--now", unit])
        if status == 0:
            removed.append(unit)
    for path in (
        Path("/etc/systemd/system/scotty-supervisor.service"),
        Path("/etc/systemd/system/scotty-credential-broker.service"),
        Path("/etc/systemd/system/scotty-egress-guard.service"),
        Path("/usr/local/sbin/scotty-supervisor"),
        Path("/usr/local/sbin/scotty-credential-broker"),
        Path("/usr/local/sbin/scotty-start"),
        Path("/usr/local/libexec/scotty-egress-guard"),
        Path("/run/scotty/credential-broker.sock"),
    ):
        if path.is_symlink() or path.exists():
            path.unlink(missing_ok=True)
            removed.append(str(path))
    for directory in (Path("/usr/local/lib/scotty"), Path("/run/scotty")):
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(str(directory))
    _run(["systemctl", "daemon-reload"])
    # The deployment's own data and the credential store are deliberately left
    # in place: removing a product should not silently destroy the operator's
    # records. The paths are printed so a person can decide.
    print(
        json.dumps(
            {
                "removed": removed,
                "left_in_place": [
                    "/srv/Scotty (deployment data, profiles and state)",
                    "/var/lib/scotty (credential store, backups and releases)",
                ],
            },
            indent=2,
        )
    )
    return 0


__all__ = ["BACKUP_DIR", "CONTAINER", "DEPLOYMENT_DIR", "RELEASES_DIR", "STATE_DIR", "run"]
