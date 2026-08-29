from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PACKAGES = (
    ("scotty_business", "scotty-business-1.0.0.tar.gz"),
    ("scotty_guard", "scotty-guard-1.0.0.tar.gz"),
)


def build(package: str = "scotty_business", archive_name: str | None = None) -> tuple[Path, str]:
    source = ROOT / "assistant" / package
    archive_path = DIST / (archive_name or f"{package.replace('_', '-')}-1.0.0.tar.gz")
    paths = sorted(
        path
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    if not paths or not (source / "plugin.yaml").is_file():
        raise RuntimeError(f"plugin source is incomplete: {package}")
    DIST.mkdir(mode=0o755, parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            relative = path.relative_to(source)
            info = archive.gettarinfo(str(path), arcname=str(Path(package) / relative))
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = 0
            info.mode = 0o600
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    with (
        archive_path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
    ):
        zipped.write(buffer.getvalue())
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (DIST / f"{archive_path.name}.sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="utf-8"
    )
    return archive_path, digest


if __name__ == "__main__":
    for package_name, archive_file in PACKAGES:
        built, package_digest = build(package_name, archive_file)
        print(f"built {built} sha256={package_digest}")
