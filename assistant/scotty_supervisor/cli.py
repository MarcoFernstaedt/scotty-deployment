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

from .journal import Journal, JournalError
from .releases import (
    ReleaseError,
    current_release,
    install_release,
    publish_release,
    rollback,
    select_release,
    verify_installed,
    verify_release,
)
from .state import (
    StateError,
    backup_state,
    restorable,
    restore_state,
    verify_backup,
)
from .supervise import (
    BLOCKED,
    DEGRADED,
    HEALTHY,
    UNKNOWN,
    DockerContainer,
    Supervisor,
)

CONTAINER = "scotty"
NETWORK = "scotty-egress"
DEPLOYMENT_DIR = Path("/srv/Scotty")
START_COMMAND = "/usr/local/sbin/scotty-start"
STATE_DIR = Path("/srv/Scotty/data/scotty")
SUPERVISOR_DIR = Path("/var/lib/scotty/supervisor")
BACKUP_DIR = Path("/var/lib/scotty/backups")
RELEASES_DIR = Path("/var/lib/scotty/releases")
WATCH_INTERVAL_SECONDS = 15.0

#: Written by the operator's own start path once setup is accepted. Its absence
#: is what keeps supervision from putting a half-installed deployment live.
ACTIVATION_MARKER = "activated"

#: Below this much free disk, the deployment is reported degraded rather than
#: left to fail on the next write.
MIN_FREE_BYTES = 256 * 1024 * 1024


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
    """Whether the bytes actually running are the ones that were accepted.

    Checking the archived release against its own manifest -- which is what
    this did -- proves the archive is intact and says nothing about the
    deployment. What matters on a restart is the tree being started.
    """

    name = current_release(RELEASES_DIR)
    if not name:
        # Nothing has been published through the release lifecycle yet, so
        # there is nothing to contradict.
        return True
    try:
        if verify_release(RELEASES_DIR / name):
            return False
        return not verify_installed(RELEASES_DIR, name, DEPLOYMENT_DIR)
    except ReleaseError:
        return False


def _activated() -> bool:
    """Whether anybody has accepted this deployment into service.

    Written once by the operator's own start path, root-owned, outside every
    mount. Without it a reboot that surfaces a stopped container would be
    enough to put a half-installed deployment live, which is not a decision
    supervision gets to make.
    """

    marker = SUPERVISOR_DIR / ACTIVATION_MARKER
    return marker.is_file() and not marker.is_symlink()


def activate() -> Path:
    """Record that a person accepted this deployment. Root only, once."""

    SUPERVISOR_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = SUPERVISOR_DIR / ACTIVATION_MARKER
    if not marker.exists():
        marker.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        marker.chmod(0o600)
    return marker


def _health() -> tuple[str, str]:
    """Whether the deployment is actually serving, not merely running.

    Each of these is something that has been up while the assistant answered
    nobody: a container whose process died inside it, a state directory that
    disappeared under a bad mount, a disk with nowhere left to write, and a
    consumer lease no live process holds.
    """

    status, output = _run(["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER])
    if status != 0:
        return UNKNOWN, "the container's state could not be read"
    observed = output.strip()
    if observed != "running":
        return DEGRADED, f"the container reports {observed or 'nothing'}"
    if not STATE_DIR.is_dir():
        return BLOCKED, "the deployment's state directory is not there"
    try:
        usage = shutil.disk_usage(STATE_DIR)
    except OSError:
        return UNKNOWN, "the deployment's disk could not be measured"
    if usage.free < MIN_FREE_BYTES:
        return DEGRADED, "the deployment is nearly out of disk"
    lease = STATE_DIR / "consumer.lease"
    if not lease.is_file():
        return DEGRADED, "no process is holding the Discord consumer lease"
    return HEALTHY, ""


def _supervisor() -> Supervisor:
    return Supervisor(
        DockerContainer(CONTAINER, _run),
        SUPERVISOR_DIR,
        alert=_alert,
        integrity=_integrity,
        activated=_activated,
        health=_health,
    )


def _named_backup(name: str) -> Path:
    candidate = (BACKUP_DIR / name).resolve()
    if candidate.parent != BACKUP_DIR.resolve():
        raise StateError("that is not the name of a backup")
    return candidate


def _recover_cutovers() -> list[str]:
    """Undo any cutover a process died in the middle of, before doing anything.

    Both the state directory and the deployment tree can be left mid-move by a
    process that stopped at the wrong instant. Whatever is found is put back to
    the generation that was running, which is a generation somebody accepted.
    """

    recovered: list[str] = []
    for root in (STATE_DIR, DEPLOYMENT_DIR):
        if not root.is_dir():
            continue
        try:
            restored = Journal(root).recover()
        except JournalError as exc:
            _alert("cutover", f"an interrupted change to {root} needs a person: {exc}")
            continue
        if restored:
            _alert("cutover", f"an interrupted change to {root} was undone")
            recovered.extend(restored)
    return recovered


def run(argv: Sequence[str]) -> int:
    command, *rest = argv
    _recover_cutovers()
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
        if command == "publish":
            if len(rest) < 2:
                print("scotty-supervisor: publish needs a source and a name", file=sys.stderr)
                return 2
            manifest = publish_release(Path(rest[0]), RELEASES_DIR, rest[1])
            published = manifest["files"]
            print(
                json.dumps(
                    {
                        "release": manifest["release"],
                        "files": len(published) if isinstance(published, list) else 0,
                    }
                )
            )
            return 0
        if command == "stage":
            if not rest:
                print("scotty-supervisor: stage needs a release name", file=sys.stderr)
                return 2
            mismatched = verify_release(RELEASES_DIR / rest[0])
            print(
                json.dumps(
                    {"release": rest[0], "intact": not mismatched, "mismatched": list(mismatched)}
                )
            )
            return 0 if not mismatched else 1
        if command == "accept":
            if not rest:
                print("scotty-supervisor: accept needs a release name", file=sys.stderr)
                return 2
            return _accept(rest[0])
        if command == "activate":
            print(json.dumps({"activated": str(activate())}, indent=2))
            return 0
        if command == "uninstall":
            return _uninstall()
    except (StateError, ReleaseError) as exc:
        print(f"scotty-supervisor: {exc}", file=sys.stderr)
        return 1
    print(f"scotty-supervisor: unknown command {command}", file=sys.stderr)
    return 2


def _accept(name: str) -> int:
    """Install one release, prove the installed bytes, then make it current.

    The order is the correction. `current` used to be selected before the
    install succeeded, so a failure left the new release recorded as running
    and the old bytes on disk -- and a rollback then had nowhere honest to go.
    Nothing is accepted until what is actually installed matches what was
    published.
    """

    release = RELEASES_DIR / name
    mismatched = verify_release(release)
    if mismatched:
        print("scotty-supervisor: that release does not match its manifest", file=sys.stderr)
        return 1
    previous = current_release(RELEASES_DIR)
    try:
        install_release(RELEASES_DIR, name, DEPLOYMENT_DIR)
    except ReleaseError as exc:
        print(f"scotty-supervisor: {exc}", file=sys.stderr)
        return 1
    installed = verify_installed(RELEASES_DIR, name, DEPLOYMENT_DIR)
    if installed:
        print(
            "scotty-supervisor: the installed bytes do not match the release; it was not accepted",
            file=sys.stderr,
        )
        return 1
    select_release(RELEASES_DIR, name, accepted=True)
    print(
        json.dumps({"accepted": name, "previous": previous, "installed_verified": True}, indent=2)
    )
    return 0


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
    """Remove what this product installed, and nothing else on the host.

    Disabling supervision and the broker while leaving the container running
    was the defect: the Discord consumer stayed connected and the bridge stayed
    up, so an "uninstalled" deployment was still answering people. Supervision
    is held first so nothing restarts what is being removed, and the container
    is stopped and proven stopped before anything else happens.
    """

    removed: list[str] = []
    _supervisor().hold("uninstalling")
    container = DockerContainer(CONTAINER, _run)
    if container.present():
        owned, ownership = _run(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.scotty.deployment" }}',
                CONTAINER,
            ]
        )
        if owned != 0 or ownership.strip() != "managed":
            # Something with this name that this product did not create.
            print(
                "scotty-supervisor: refusing to remove a container this product does not own",
                file=sys.stderr,
            )
            return 1
        if container.is_running():
            container.stop()
        if container.is_running():
            print(
                "scotty-supervisor: the container did not stop; nothing was removed",
                file=sys.stderr,
            )
            return 1
        removed.append(f"container {CONTAINER} (stopped)")
        status, _ = _run(["docker", "container", "rm", CONTAINER])
        if status == 0:
            removed.append(f"container {CONTAINER} (removed)")
    network, listed = _run(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            '{{ index .Labels "com.scotty.deployment" }}',
            NETWORK,
        ]
    )
    if network == 0 and listed.strip() == "managed":
        status, _ = _run(["docker", "network", "rm", NETWORK])
        if status == 0:
            removed.append(f"network {NETWORK}")
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
