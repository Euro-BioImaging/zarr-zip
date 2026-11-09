"""Write a Zarr store straight into a zip, entry by entry, while it is being written.

A zip entry's bytes must be contiguous, so only finished pieces go in: whole chunks or whole
shards, from memory (:meth:`ZipWriter.write`) or from a file (:meth:`ZipWriter.write_from`,
for a shard assembled on disk). Writers in several threads share one :class:`ZipWriter`; a
lock lets one entry in at a time.

What makes it usable as a store being written:

- **Read back.** Entries are stored uncompressed, and the writer keeps where each one's bytes
  are. :meth:`ZipWriter.read` returns a finished entry from the unfinished file, so a pyramid
  writer can read level 0 back to build the coarser levels before the zip is closed.
- **Last copy wins.** A key written twice (``zarr.json`` rewritten once the data is in) is
  listed once in the central directory, its last copy. The earlier bytes stay in the file,
  unlisted; readers that use the central directory (zarr's ``ZipStore``, ``zipfile``,
  ``tar``, NGFF RFC-9 readers) never see them.
- **Metadata first.** The central directory lists the metadata files first, shallowest first
  (as RFC-9's ``jsonFirst`` asks), in whatever order they were written.
- **No half zip under the final name.** The zip is written as ``<name>.part`` and renamed
  when closed and checked; :meth:`ZipWriter.abort` (or an error inside ``with``) deletes it.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple, Union

__all__ = ["ZipWriter", "METADATA_FILES", "is_metadata"]

#: files that hold Zarr / OME-Zarr metadata: listed first in the central directory
METADATA_FILES = ("zarr.json", ".zgroup", ".zarray", ".zattrs", ".zmetadata")
_COPY_BUFFER = 8 * 1024 * 1024

BytesLike = Union[bytes, bytearray, memoryview]


def is_metadata(name: str) -> bool:
    """True for a Zarr metadata file (``zarr.json``, ``.zattrs``, ...) at any depth."""
    return name.rsplit("/", 1)[-1] in METADATA_FILES


class ZipWriter:
    """A zip file written entry by entry, readable while it is written.

    Parameters
    ----------
    path
        The zip to write. The file is ``<path>.part`` until :meth:`close` renames it.
    root
        A folder every key is placed in, e.g. ``"img.ome.zarr"`` (the BioImage Archive's
        layout: the store's folder inside the zip). ``""`` (default) puts the store's root
        at the zip's root (NGFF RFC-9).
    comment
        The zip comment (RFC-9: a JSON object naming the OME-Zarr version), or a function
        of the writer returning it, called at :meth:`close` (when the version is known
        only once the metadata is written). Settable until then as ``writer.comment``.
    force_zip64
        Write ZIP64 headers for every entry (RFC-9 asks for it); otherwise only where an
        entry or the archive needs them.
    overwrite
        Replace an existing zip at *path* when closing; without it a zip there is refused
        at once, before anything is written.

    Keys are ``/``-separated paths relative to the store's root (``"zarr.json"``,
    ``"0/c/0/0/0"``). Use it as a context manager: leaving the block closes it, or aborts it
    when an exception left the block.
    """

    def __init__(self, path, *, root: str = "",
                 comment: Union[bytes, Callable[["ZipWriter"], bytes]] = b"",
                 force_zip64: bool = False, overwrite: bool = False):
        self.path = Path(path)
        if self.path.exists() and not overwrite:
            raise FileExistsError(f"{self.path} exists (pass overwrite=True to replace it)")
        self.part = self.path.with_name(self.path.name + ".part")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        root = root.strip("/")
        self._prefix = root + "/" if root else ""
        self.comment = comment
        self._force_zip64 = force_zip64
        self._overwrite = overwrite
        self._date_time = time.localtime(time.time())[:6]
        self._lock = threading.Lock()           # one entry in at a time
        self._index_lock = threading.Lock()     # the index alone: reads never wait for a copy
        self._read_lock = threading.Lock()
        # key -> (data offset, size) of its last copy
        self._index: Dict[str, Tuple[int, int]] = {}
        self._zf = zipfile.ZipFile(self.part, "w", compression=zipfile.ZIP_STORED,
                                   allowZip64=True)
        self._reader = None
        self._closed = False

    # ── writing ───────────────────────────────────────────────────────────────

    def write(self, key: str, data: BytesLike) -> None:
        """Add the entry *key* holding *data* (a whole chunk or shard)."""
        data = memoryview(data).cast("B")
        info = self._info(key, len(data))

        def fill(out):
            out.write(data)
        self._append(key, info, fill)

    def write_from(self, key: str, source, remove: bool = False) -> None:
        """Add the entry *key* holding the file *source* (a shard assembled on disk), copied
        in pieces of 8 MB; with *remove*, *source* is deleted once it is in."""
        source = Path(source)
        info = self._info(key, source.stat().st_size)

        def fill(out):
            with open(source, "rb") as fin:
                shutil.copyfileobj(fin, out, _COPY_BUFFER)
        self._append(key, info, fill)
        if remove:
            source.unlink()

    def _info(self, key: str, size: int) -> zipfile.ZipInfo:
        key = self._key(key)
        info = zipfile.ZipInfo(self._prefix + key, date_time=self._date_time)
        info.compress_type = zipfile.ZIP_STORED
        info.file_size = size               # lets zipfile choose ZIP64 for a large entry
        info.external_attr = 0o644 << 16
        return info

    def _append(self, key: str, info: zipfile.ZipInfo, fill: Callable) -> None:
        key = self._key(key)
        with self._lock:
            self._check_open()
            # a rewrite: forget the earlier copy here (zipfile would warn); close() drops it
            # from the central directory
            self._zf.NameToInfo.pop(info.filename, None)
            with self._zf.open(info, "w", force_zip64=self._force_zip64) as out:
                offset = self._zf.fp.tell()     # the local header is written: data starts here
                fill(out)
            self._zf.fp.flush()                 # readers see the bytes at once
            with self._index_lock:
                self._index[key] = (offset, info.file_size)

    # ── reading back ──────────────────────────────────────────────────────────

    def read(self, key: str, start: int = 0, end: Optional[int] = None) -> bytes:
        """The bytes of the finished entry *key* (its last copy), or ``[start:end]`` of
        them (Python slice rules: negative counts from the end); KeyError if there is
        none."""
        key = self._key(key)
        with self._index_lock:
            if key not in self._index:
                raise KeyError(key)
            offset, size = self._index[key]
        start, end, _ = slice(start, end).indices(size)
        return self._pread(offset + start, max(0, end - start))

    def get(self, key: str, default=None):
        try:
            return self.read(key)
        except KeyError:
            return default

    def __contains__(self, key) -> bool:
        with self._index_lock:
            return self._key(key) in self._index

    def keys(self) -> Iterator[str]:
        """The keys written so far (each once), in no fixed order."""
        with self._index_lock:
            return iter(list(self._index))

    def size(self, key: str) -> int:
        with self._index_lock:
            return self._index[self._key(key)][1]

    def _pread(self, offset: int, size: int) -> bytes:
        if size == 0:
            return b""
        with self._read_lock:
            if self._reader is None:
                self._reader = open(self.part, "rb")
            if hasattr(os, "pread"):
                fd = self._reader.fileno()
            else:                                   # Windows: no pread; one reader at a time
                self._reader.seek(offset)
                data = self._reader.read(size)
                fd = None
        if fd is not None:
            data, got = [], 0
            while got < size:
                piece = os.pread(fd, size - got, offset + got)
                if not piece:
                    break
                data.append(piece)
                got += len(piece)
            data = b"".join(data)
        if len(data) != size:
            raise OSError(f"{self.part}: read {len(data)} of {size} bytes at offset {offset}")
        return data

    # ── closing ───────────────────────────────────────────────────────────────

    def close(self, verify: str = "list",
              check: Optional[Callable[[Path], None]] = None) -> Path:
        """Write the central directory, check the zip and rename it to its final name.

        ``verify``: "list" (default) reads the central directory back and compares names and
        sizes with what was written; "full" also checks every entry's CRC; "none" skips both.
        *check*, if given, is called with the ``.part`` path before the rename (raise to
        refuse it). On any failure the ``.part`` file is deleted and the error raised.
        """
        with self._lock:
            if self._closed:
                return self.path
            try:
                self._order_central_directory()
                comment = self.comment(self) if callable(self.comment) else self.comment
                self._zf.comment = bytes(comment or b"")
                self._zf.close()
                self._close_reader()
                if verify != "none":
                    self._verify(crc=(verify == "full"))
                if check is not None:
                    check(self.part)
                if self.path.exists() and not self._overwrite:
                    raise FileExistsError(f"{self.path} appeared while it was written")
                os.replace(self.part, self.path)
            except BaseException:
                self._discard()
                raise
            self._closed = True
        return self.path

    def abort(self) -> None:
        """Stop and delete the ``.part`` file."""
        with self._lock:
            if not self._closed:
                self._discard()

    def _discard(self) -> None:
        self._closed = True
        try:
            self._zf.close()
        except Exception:                           # noqa: BLE001 - deleting it anyway
            pass
        self._close_reader()
        self.part.unlink(missing_ok=True)

    def _close_reader(self) -> None:
        with self._read_lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    def _order_central_directory(self) -> None:
        """Keep the last copy of each name; metadata first (shallowest first), then the rest
        in the order written."""
        last = {}
        for position, info in enumerate(self._zf.filelist):
            last[info.filename] = (position, info)
        kept = sorted(last.values())
        meta = sorted((info for _, info in kept if is_metadata(info.filename)),
                      key=lambda i: (i.filename.count("/"), i.filename))
        data = [info for _, info in kept if not is_metadata(info.filename)]
        self._zf.filelist[:] = meta + data
        self._zf.NameToInfo = {info.filename: info for info in self._zf.filelist}

    def _verify(self, crc: bool) -> None:
        expected = {self._prefix + key: size for key, (_, size) in self._index.items()}
        with zipfile.ZipFile(self.part) as zf:
            got = {i.filename: i.file_size for i in zf.infolist()}
            if got != expected:
                missing = sorted(set(expected) - set(got))[:5]
                extra = sorted(set(got) - set(expected))[:5]
                raise ValueError(f"{self.part}: the central directory does not list what was "
                                 f"written (missing {missing}, extra {extra})")
            if crc:
                bad = zf.testzip()
                if bad is not None:
                    raise ValueError(f"{self.part}: CRC check failed for {bad}")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _key(self, key: str) -> str:
        key = str(key).replace("\\", "/").strip("/")
        if not key or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError(f"not a store key: {key!r}")
        return key

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError(f"{self.path}: the zip writer is closed")

    def __enter__(self) -> "ZipWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"{len(self._index)} entries"
        return f"ZipWriter({str(self.path)!r}, {state})"
