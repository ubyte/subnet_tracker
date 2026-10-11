import os
import tempfile
from pathlib import Path

from subnet_tracker.routes.render import routes_hash

_MODE = 0o644


def write_routes_file(path: Path, text: str) -> bool:
    if _matches_disk(path, text):
        return False
    # Copy the previous bytes first, so a failed replace leaves the routes file in place.
    if path.is_file():
        _atomic_write(path.with_name(f"{path.name}.prev"), path.read_bytes())
    _atomic_write(path, text.encode("utf-8"))
    return True


def _matches_disk(path: Path, text: str) -> bool:
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return False
    return routes_hash(current) == routes_hash(text)


def _atomic_write(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            os.fchmod(out.fileno(), _MODE)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
