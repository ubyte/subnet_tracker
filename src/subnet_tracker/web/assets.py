import gzip
import hashlib
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

_PRECOMPRESSED = {".css", ".js"}


def hashed_name(name: str, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()[:12]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}.{digest}"
    return f"{stem}.{digest}.{ext}"


def _mimetype(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _precompressed(name: str, data: bytes) -> bytes | None:
    if Path(name).suffix not in _PRECOMPRESSED:
        return None
    return gzip.compress(data, mtime=0)


@dataclass(frozen=True, slots=True)
class Asset:
    url_name: str
    mimetype: str
    data: bytes
    gzipped: bytes | None


@dataclass(frozen=True, slots=True)
class Assets:
    by_name: Mapping[str, Asset]
    by_url_name: Mapping[str, Asset]


def build_assets(files: Mapping[str, bytes]) -> Assets:
    by_name: dict[str, Asset] = {}
    by_url_name: dict[str, Asset] = {}
    for name, data in files.items():
        asset = Asset(hashed_name(name, data), _mimetype(name), data, _precompressed(name, data))
        by_name[name] = asset
        by_url_name[asset.url_name] = asset
    return Assets(by_name, by_url_name)


def load_assets(root: Traversable) -> Assets:
    return build_assets({item.name: item.read_bytes() for item in root.iterdir() if item.is_file()})


def asset_url(assets: Assets, name: str) -> str:
    asset = assets.by_name.get(name)
    if asset is None:
        raise LookupError(name)
    return f"/static/{asset.url_name}"
