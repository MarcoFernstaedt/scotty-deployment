from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assistant" / "scotty_business"
DIST = ROOT / "dist"
ARCHIVE = DIST / "scotty-business-1.0.0.tar.gz"


def build() -> tuple[Path, str]:
    paths = sorted(
        path
        for path in SOURCE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    if not paths or not (SOURCE / "plugin.yaml").is_file():
        raise RuntimeError("plugin source is incomplete")
    DIST.mkdir(mode=0o755, parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            relative = path.relative_to(SOURCE)
            info = archive.gettarinfo(str(path), arcname=str(Path("scotty_business") / relative))
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = 0
            info.mode = 0o600
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    with (
        ARCHIVE.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
    ):
        zipped.write(buffer.getvalue())
    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    (DIST / "scotty-business-1.0.0.tar.gz.sha256").write_text(
        f"{digest}  {ARCHIVE.name}\n", encoding="utf-8"
    )
    return ARCHIVE, digest


if __name__ == "__main__":
    archive, digest = build()
    print(f"built {archive} sha256={digest}")
