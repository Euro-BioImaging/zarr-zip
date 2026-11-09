"""A zarr-python store over a :class:`~zarr_zip.ZipWriter`: zarr reads and writes a zip that
is still being written.

>>> import zarr
>>> from zarr_zip import open_writer
>>> from zarr_zip.zarr_store import WriterStore
>>> with open_writer("img.ome.zarr.zip") as zw:
...     group = zarr.open_group(WriterStore(zw), mode="w-", zarr_format=3)
...     group.attrs["ome"] = {"version": "0.5"}
...     # arrays written elsewhere into zw are read back with zarr.open_array(WriterStore(zw), path="0")

A value written is a whole zip entry; writing a key again appends a new copy and the zip's
table of contents lists only the last one. Nothing can be deleted from a zip being written,
so ``delete`` refuses. Needs zarr-python 3 (``pip install "zarr-zip[zarr]"``).
"""
from __future__ import annotations

from typing import AsyncIterator, Iterable, Optional

from zarr.abc.store import (ByteRequest, OffsetByteRequest, RangeByteRequest, Store,
                            SuffixByteRequest)
from zarr.core.buffer import Buffer, BufferPrototype, default_buffer_prototype

from .writer import ZipWriter

__all__ = ["WriterStore"]


class WriterStore(Store):
    """zarr-python's view of a :class:`~zarr_zip.ZipWriter` (keys relative to the store's
    root, or to ``prefix`` inside it). Reads see every entry already written; writes
    append entries.

    ``prefix`` roots the store at a group inside the zip, as a folder path roots a local
    store: zarr then writes no metadata for the groups above it (opening ``path="A/1"``
    of an unprefixed store would create ``A`` and ``A/1`` as groups)."""

    supports_writes: bool = True
    supports_deletes: bool = False
    supports_listing: bool = True
    supports_partial_writes: bool = False

    def __init__(self, writer: ZipWriter, *, read_only: bool = False, prefix: str = "") -> None:
        super().__init__(read_only=read_only)
        self.writer = writer
        self.prefix = str(prefix).strip("/")

    def with_read_only(self, read_only: bool = False) -> "WriterStore":
        return type(self)(self.writer, read_only=read_only, prefix=self.prefix)

    def __str__(self) -> str:
        return f"zip-writer://{self.writer.path}" + (f"/{self.prefix}" if self.prefix else "")

    def __repr__(self) -> str:
        return f"WriterStore({str(self.writer.path)!r}, prefix={self.prefix!r})"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, WriterStore) and other.writer is self.writer
                and other.read_only == self.read_only and other.prefix == self.prefix)

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _mine(self):
        """The keys under the prefix, relative to it."""
        if not self.prefix:
            yield from self.writer.keys()
            return
        start = self.prefix + "/"
        for key in self.writer.keys():
            if key.startswith(start):
                yield key[len(start):]

    # ── reading ───────────────────────────────────────────────────────────────

    def _read(self, key: str, byte_range: Optional[ByteRequest]) -> Optional[bytes]:
        key = self._full(key)
        if key not in self.writer:
            return None
        if byte_range is None:
            return self.writer.read(key)
        if isinstance(byte_range, RangeByteRequest):
            return self.writer.read(key, byte_range.start, byte_range.end)
        if isinstance(byte_range, OffsetByteRequest):
            return self.writer.read(key, byte_range.offset)
        if isinstance(byte_range, SuffixByteRequest):
            size = self.writer.size(key)
            return self.writer.read(key, max(0, size - byte_range.suffix))
        raise TypeError(f"unexpected byte range {byte_range!r}")

    async def get(self, key: str, prototype: Optional[BufferPrototype] = None,
                  byte_range: Optional[ByteRequest] = None) -> Optional[Buffer]:
        prototype = prototype or default_buffer_prototype()
        data = self._read(key, byte_range)
        return None if data is None else prototype.buffer.from_bytes(data)

    async def get_partial_values(self, prototype: BufferPrototype,
                                 key_ranges: Iterable[tuple[str, Optional[ByteRequest]]]
                                 ) -> list[Optional[Buffer]]:
        return [await self.get(key, prototype, byte_range) for key, byte_range in key_ranges]

    async def exists(self, key: str) -> bool:
        return self._full(key) in self.writer

    # ── writing ───────────────────────────────────────────────────────────────

    async def set(self, key: str, value: Buffer, byte_range=None) -> None:
        self._check_writable()
        if byte_range is not None:
            raise NotImplementedError("a zip entry is written whole: no partial writes")
        self.writer.write(self._full(key), value.to_bytes())

    async def set_if_not_exists(self, key: str, value: Buffer) -> None:
        if self._full(key) not in self.writer:
            await self.set(key, value)

    async def delete(self, key: str) -> None:
        raise NotImplementedError(
            f"{self.writer.path}: nothing can be deleted from a zip being written (a key "
            f"written again replaces the old copy in the table of contents)")

    # ── listing ───────────────────────────────────────────────────────────────

    async def list(self) -> AsyncIterator[str]:
        for key in self._mine():
            yield key

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        for key in self._mine():
            if key.startswith(prefix):
                yield key

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        prefix = prefix.rstrip("/")
        seen = set()
        for key in self._mine():
            if prefix:
                if not key.startswith(prefix + "/"):
                    continue
                key = key[len(prefix) + 1:]
            name = key.split("/", 1)[0]
            if name not in seen:
                seen.add(name)
                yield name
