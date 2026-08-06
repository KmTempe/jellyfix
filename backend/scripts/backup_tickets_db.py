from __future__ import annotations

import argparse
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the legacy JellyFix SQLite database before secure cutover.")
    parser.add_argument("--source", default="backend/tickets.db", help="Legacy database path.")
    parser.add_argument("--dest-dir", default="backups", help="Directory for the database copy and checksum file.")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        raise SystemExit(f"source database not found: {source}")
    dest_dir = Path(args.dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = dest_dir / f"{source.stem}-{stamp}{source.suffix}"
    checksum_file = dest_dir / f"{backup.name}.sha256"
    shutil.copy2(source, backup)
    checksum_file.write_text(f"{sha256(backup)}  {backup.name}\n", encoding="utf-8")
    print(f"backup={backup}")
    print(f"sha256={checksum_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
