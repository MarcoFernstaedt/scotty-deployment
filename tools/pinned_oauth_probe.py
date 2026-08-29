"""Discover the pinned runtime's own OAuth/login command.

The Codex OAuth step must use the command the pinned Hermes 0.20.6 image
actually supports. This probe reads that from the image itself instead of
guessing it, using a disposable, network-disabled container. It performs no
login, stores no credential, and mutates nothing.

Run it on a host that has the pinned image loaded:

    python3 tools/pinned_oauth_probe.py
"""

from __future__ import annotations

import subprocess

IMAGE = "nousresearch/hermes-agent@sha256:d64f4e9aba92884fff3d5020c02a75676066f237622d0776759ca1437b9b0517"
PROBE = r"""
import json
import subprocess

candidates = []
for argv in (["hermes", "--help"], ["hermes", "auth", "--help"], ["hermes", "login", "--help"]):
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=60)
    candidates.append(
        {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
        }
    )
print(json.dumps({"image": "hermes 0.20.6", "probes": candidates}, indent=2))
"""


def main() -> int:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",  # noqa: S108 - isolated container tmpfs, not a host temp path
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        PROBE,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=300)
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "pinned-image OAuth probe failed")
    print(
        "\nUse the exact subcommand printed above for the Codex OAuth step. "
        "Do not substitute a command from another Hermes version."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
