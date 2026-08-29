from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "SHA256SUMS"
EXCLUDED_PREFIXES = (".git/", "dist/", "build/", "__pycache__/")
EXCLUDED_NAMES = {"SHA256SUMS"}


def inventory() -> tuple[str, ...]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    paths = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8")
        if path in EXCLUDED_NAMES or path.startswith(EXCLUDED_PREFIXES):
            continue
        candidate = ROOT / path
        if candidate.is_file() and not candidate.is_symlink():
            paths.append(path)
    return tuple(sorted(paths))


def render(paths: tuple[str, ...]) -> str:
    lines = []
    for path in paths:
        digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        lines.append(f"{digest}  {path}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render(inventory())
    if args.check:
        observed = MANIFEST.read_text(encoding="utf-8")
        if observed != expected:
            raise SystemExit("SHA256SUMS is stale or incomplete")
        print(f"checksum inventory: PASS ({len(inventory())} files)")
        return 0
    MANIFEST.write_text(expected, encoding="utf-8")
    print(f"wrote SHA256SUMS for {len(inventory())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
