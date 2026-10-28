"""Zip OME-Zarr stores one by one, for upload (BioImage Archive) or as RFC-9 ``.ozx`` files.

>>> from ome_zarr_zip import find_stores, zip_store
>>> for store in find_stores("results"):
...     zip_store(store)

Two layouts:

``bia`` (default)
    For the BioImage Archive: ``X.zip`` holding the store's folder ``X`` as it is.
    ``rename_folder`` gives the names the archive asks for: ``X.ome.zarr.zip`` holding
    ``X.ome.zarr/`` (``X.zarr.zip`` holding ``X.zarr/`` for a Zarr that is not OME-Zarr).
``rfc9``
    NGFF RFC-9 (draft): ``X.ozx`` whose root is the OME-Zarr root (``zarr.json`` at the top),
    ZIP64 entries, and a JSON comment naming the OME-Zarr version. Zarr v3 (OME-Zarr 0.5+) only.

In both, entries are stored uncompressed (Zarr chunks are compressed already) and the
``zarr.json`` / ``.zattrs`` files come first, shallowest first, so a reader finds the
metadata without scanning the archive.
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

__version__ = "0.0.1"
__all__ = ["Store", "find_stores", "zip_name", "inner_folder", "bia_folder", "follows_bia_naming",
           "zip_store", "verify_zip", "LAYOUTS"]

LAYOUTS = ("bia", "rfc9")
#: files that hold Zarr / OME-Zarr metadata, written before the chunks
METADATA_FILES = ("zarr.json", ".zgroup", ".zarray", ".zattrs", ".zmetadata")
#: the BioImage Archive asks for zips below this size (it refuses above 2 TB)
BIA_ADVISED_MAX = 100 * 1024 ** 3
_COPY_BUFFER = 8 * 1024 * 1024


@dataclass
class Store:
    """One Zarr hierarchy on disk: its root folder, Zarr format (2 or 3) and OME-Zarr
    version (None when it is a plain Zarr)."""
    path: Path
    zarr_format: int
    ome_version: Optional[str]

    @property
    def is_ome(self) -> bool:
        return self.ome_version is not None


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_store(path) -> Optional[Store]:
    """The Store rooted at *path*, or None when *path* is not the root of a Zarr hierarchy."""
    path = Path(path)
    if (path / "zarr.json").is_file():
        ome = _read_json(path / "zarr.json").get("attributes", {}).get("ome")
        version = ome.get("version") if isinstance(ome, dict) else None
        return Store(path, 3, str(version) if version is not None else None)
    if (path / ".zgroup").is_file() or (path / ".zattrs").is_file() or (path / ".zarray").is_file():
        return Store(path, 2, _ome_version_v2(_read_json(path / ".zattrs")))
    return None


def _ome_version_v2(attrs: dict) -> Optional[str]:
    """The OME-Zarr version (0.4 and older) of a Zarr v2 group's ``.zattrs``."""
    for key in ("multiscales", "plate", "well"):
        value = attrs.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            value = value[0]
        if isinstance(value, dict):
            return str(value.get("version", "unknown"))
    if "bioformats2raw.layout" in attrs:
        return "unknown"
    return None


def find_stores(root) -> List[Store]:
    """Every Zarr hierarchy under *root* (or *root* itself), sorted by path. A hierarchy is
    taken whole: a plate is one store, its wells and images are not looked into."""
    root = Path(root)
    store = read_store(root)
    if store is not None:
        return [store]
    found = []
    for folder, dirs, _files in os.walk(root):
        keep = []
        for name in sorted(dirs):
            store = read_store(Path(folder) / name)
            if store is not None:
                found.append(store)
            else:
                keep.append(name)
        dirs[:] = keep                  # do not walk into a store
    return sorted(found, key=lambda s: str(s.path).lower())


def _base_name(name: str) -> str:
    """``X.ome.zarr`` / ``X.zarr`` / ``X`` -> ``X``."""
    for suffix in (".ome.zarr", ".zarr"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def bia_folder(store: Store) -> str:
    """The folder name the BioImage Archive asks for: ``X.ome.zarr`` for OME-Zarr, ``X.zarr``
    for any other Zarr."""
    return _base_name(store.path.name) + (".ome.zarr" if store.is_ome else ".zarr")


def inner_folder(store: Store, layout: str = "bia", rename_folder: bool = False) -> str:
    """The folder the zip unpacks to: the store's own name, or with *rename_folder* the name
    the BioImage Archive asks for (``X.zarr`` -> ``X.ome.zarr``); "" for rfc9, whose root is
    the store itself."""
    if layout == "rfc9":
        return ""
    return bia_folder(store) if rename_folder else store.path.name


def zip_name(store: Store, layout: str = "bia", rename_folder: bool = False) -> str:
    """The zip file name for *store*: the folder inside plus ``.zip`` (bia), ``X.ozx`` (rfc9)."""
    if layout == "rfc9":
        return _base_name(store.path.name) + ".ozx"
    return inner_folder(store, layout, rename_folder) + ".zip"


def follows_bia_naming(store: Store, layout: str = "bia", rename_folder: bool = False) -> bool:
    """True when the zip is named as the BioImage Archive asks (``X.ome.zarr.zip`` holding
    ``X.ome.zarr/`` for OME-Zarr, ``X.zarr.zip`` holding ``X.zarr/`` otherwise). Always true
    for rfc9, which follows its own rules."""
    return layout == "rfc9" or inner_folder(store, layout, rename_folder) == bia_folder(store)


def store_files(store: Store) -> List[str]:
    """The files of *store* relative to its root (``/``-separated): metadata files first,
    shallowest first (breadth-first, as RFC-9 asks), then all others."""
    meta, data = [], []
    for folder, dirs, files in os.walk(store.path):
        dirs.sort()
        rel = Path(folder).relative_to(store.path).as_posix()
        for name in sorted(files):
            path = name if rel == "." else f"{rel}/{name}"
            (meta if name in METADATA_FILES else data).append(path)
    meta.sort(key=lambda p: (p.count("/"), p))
    return meta + data


def zip_store(store: Store, dest=None, layout: str = "bia", overwrite: bool = False,
              verify: str = "full", rename_folder: bool = False) -> Path:
    """Zip *store* into *dest* (a folder, or the zip path; default: next to the store) and
    return the zip path. The zip is written as ``<name>.part`` and renamed when complete, so
    an interrupted run leaves no zip that looks finished. ``verify``: "full" reads the zip
    back and checks every CRC, "list" checks names and sizes only, "none" skips both.
    ``rename_folder`` (bia): name the folder inside, and so the zip, as the BioImage Archive
    asks (``X.zarr`` -> ``X.ome.zarr`` in ``X.ome.zarr.zip``); by default the folder keeps
    the store's own name (``X.zarr`` in ``X.zarr.zip``)."""
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}, not {layout!r}")
    if layout == "rfc9" and store.zarr_format != 3:
        raise ValueError(f"{store.path}: RFC-9 zips hold Zarr v3 (OME-Zarr 0.5+) only; this "
                         f"store is Zarr v{store.zarr_format}. Use layout 'bia'.")
    name = zip_name(store, layout, rename_folder)
    if dest is None:
        target = store.path.parent / name
    else:
        dest = Path(dest)
        target = dest if dest.suffix.lower() in (".zip", ".ozx") else dest / name
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} exists (pass overwrite=True / --overwrite to replace it)")
    target.parent.mkdir(parents=True, exist_ok=True)

    files = store_files(store)
    inner = inner_folder(store, layout, rename_folder)
    prefix = inner + "/" if inner else ""
    part = target.with_name(target.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED, allowZip64=True,
                             strict_timestamps=False) as zf:
            for rel in files:
                src = store.path / rel
                info = zipfile.ZipInfo.from_file(src, prefix + rel, strict_timestamps=False)
                info.compress_type = zipfile.ZIP_STORED
                with open(src, "rb") as fin, zf.open(info, "w", force_zip64=(layout == "rfc9")) as fout:
                    shutil.copyfileobj(fin, fout, _COPY_BUFFER)
            if layout == "rfc9":
                zf.comment = json.dumps({"ome": {
                    "version": store.ome_version or "unknown",
                    "zipFile": {"centralDirectory": {"jsonFirst": True}}}}).encode("utf-8")
        if verify != "none":
            verify_zip(part, store, layout, crc=(verify == "full"), rename_folder=rename_folder)
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return target


def verify_zip(path, store: Store, layout: str = "bia", crc: bool = True,
               rename_folder: bool = False) -> None:
    """Raise ValueError unless the zip at *path* holds exactly the files of *store*, with
    their sizes (and, with *crc*, intact data)."""
    prefix = inner_folder(store, layout, rename_folder)
    prefix = prefix + "/" if prefix else ""
    expected = {prefix + rel: (store.path / rel).stat().st_size for rel in store_files(store)}
    with zipfile.ZipFile(path) as zf:
        got = {i.filename: i.file_size for i in zf.infolist() if not i.is_dir()}
        if got != expected:
            missing = sorted(set(expected) - set(got))[:5]
            extra = sorted(set(got) - set(expected))[:5]
            sizes = sorted(k for k in set(got) & set(expected) if got[k] != expected[k])[:5]
            raise ValueError(f"{path}: zip does not match {store.path} "
                             f"(missing {missing}, extra {extra}, size differs {sizes})")
        if crc:
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(f"{path}: CRC check failed for {bad}")


def iter_plan(stores, layout="bia", output=None, input_root=None,
              rename_folder=False) -> Iterator[tuple]:
    """(store, target path) for each store. With *output*, the folders between *input_root*
    and each store are kept under *output*; without, each zip goes next to its store."""
    for store in stores:
        name = zip_name(store, layout, rename_folder)
        if output is None:
            yield store, store.path.parent / name
            continue
        rel = Path()
        if input_root is not None:
            try:
                rel = store.path.parent.relative_to(input_root)
            except ValueError:
                pass
        yield store, Path(output) / rel / name


def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _store_size(store: Store) -> int:
    return sum(f.stat().st_size for f in store.path.rglob("*") if f.is_file())
