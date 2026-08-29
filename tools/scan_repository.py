from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "private key": re.compile(rb"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "Slack token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "Discord token": re.compile(rb"[0-9]{8,12}:[A-Za-z0-9_-]{20,}"),
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main() -> int:
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).split(b"\0")
    current_checked = 0
    for raw in paths:
        if not raw:
            continue
        path = raw.decode("utf-8")
        if Path(path).suffix.lower() in FORBIDDEN_SUFFIXES:
            raise RuntimeError(f"forbidden tracked artifact: {path}")
        content = (ROOT / path).read_bytes()
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                raise RuntimeError(f"{label} pattern in working tree: {path}")
        current_checked += 1
    objects = git("rev-list", "--objects", "--all").decode("utf-8").splitlines()
    checked = 0
    for entry in objects:
        object_id, _, path = entry.partition(" ")
        if not path:
            continue
        kind = git("cat-file", "-t", object_id).strip()
        if kind != b"blob":
            continue
        size = int(git("cat-file", "-s", object_id))
        if size > 2_000_000:
            raise RuntimeError(f"oversized historical blob requires review: {path}")
        content = git("cat-file", "-p", object_id)
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                raise RuntimeError(f"{label} pattern in reachable history: {path}")
        checked += 1
    print(
        f"repository scan: PASS ({current_checked} working files, "
        f"{checked} reachable historical blobs, no secret patterns)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
