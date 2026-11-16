"""Copy a read-only legacy object tree into a persistent server volume."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import uuid


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def import_objects(source: Path, target: Path, *, owner: tuple[int, int] | None = None) -> dict:
    source, target = source.resolve(), target.resolve()
    if not source.is_dir() or source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Source and destination must be separate directories")
    paths = sorted(source.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("Legacy object tree contains symbolic links")
    copied = verified = 0
    target.mkdir(parents=True, exist_ok=True)
    if owner is not None:
        os.chown(target, *owner)
    for path in paths:
        if not path.is_file():
            continue
        destination = target / path.relative_to(source)
        if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents if parent != target.parent):
            raise ValueError("Destination object tree contains symbolic links")
        if destination.exists():
            if not destination.is_file() or digest(destination) != digest(path):
                raise ValueError("Existing destination object differs; refusing to overwrite")
            verified += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if owner is not None:
            parent = destination.parent
            while parent != target:
                os.chown(parent, *owner)
                parent = parent.parent
        temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(path, temporary)
            if digest(temporary) != digest(path):
                raise ValueError("Object verification failed")
            temporary.replace(destination)
            if owner is not None:
                os.chown(destination, *owner)
            copied += 1
        finally:
            temporary.unlink(missing_ok=True)
    return {"copied": copied, "verified_existing": verified}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path("/app/data/docs"))
    parser.add_argument("--owner", help="UID:GID for files copied into a server volume")
    args = parser.parse_args()
    owner = tuple(int(value) for value in args.owner.split(":")) if args.owner else None
    if owner is not None and len(owner) != 2:
        raise ValueError("Owner must be UID:GID")
    print(import_objects(args.source, args.target, owner=owner))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
