"""ZipWriter: a store written straight into a zip, readable while it is written."""
import os
import random
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from zarr_zip import ZipWriter, rfc9_comment


def _entries(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def test_entries_from_memory_and_file_and_metadata_first(tmp_path):
    shard = tmp_path / "shard.tmp"
    shard.write_bytes(b"S" * 1000)
    with ZipWriter(tmp_path / "x.zip", root="x.ome.zarr") as zw:
        zw.write("0/c/0/0", b"chunk")
        zw.write_from("0/c/0/1", shard, remove=True)
        zw.write("0/zarr.json", b'{"node_type": "array"}')
        zw.write("zarr.json", b'{"node_type": "group"}')
    assert not shard.exists()                                   # remove=True
    with zipfile.ZipFile(tmp_path / "x.zip") as zf:
        assert zf.namelist() == ["x.ome.zarr/zarr.json", "x.ome.zarr/0/zarr.json",
                                 "x.ome.zarr/0/c/0/0", "x.ome.zarr/0/c/0/1"]
        assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_STORED}
        assert zf.read("x.ome.zarr/0/c/0/1") == b"S" * 1000
    assert not (tmp_path / "x.zip.part").exists()


def test_finished_entries_read_back_before_close(tmp_path):
    zw = ZipWriter(tmp_path / "x.zip")
    zw.write("a", b"first")
    zw.write("b", b"")
    assert zw.read("a") == b"first" and zw.read("b") == b""
    assert "a" in zw and "c" not in zw and zw.get("c") is None
    with pytest.raises(KeyError):
        zw.read("c")
    assert (tmp_path / "x.zip.part").exists() and not (tmp_path / "x.zip").exists()
    zw.close()
    assert (tmp_path / "x.zip").exists()


def test_a_rewritten_key_keeps_its_last_copy_only(tmp_path):
    with ZipWriter(tmp_path / "x.zip") as zw:
        zw.write("zarr.json", b"old")
        zw.write("0/c/0", b"data")
        zw.write("zarr.json", b"new, longer")
        assert zw.read("zarr.json") == b"new, longer"
    assert _entries(tmp_path / "x.zip") == {"zarr.json": b"new, longer", "0/c/0": b"data"}


def test_threads_write_and_read_at_once(tmp_path):
    rng = random.Random(0)
    payload = {f"0/c/{t}/{i}": bytes([t]) * rng.randint(0, 50_000) for t in range(8) for i in range(40)}
    errors = []

    with ZipWriter(tmp_path / "x.zip") as zw:
        def worker(t):
            try:
                for i in range(40):
                    key = f"0/c/{t}/{i}"
                    zw.write(key, payload[key])
                    other = f"0/c/{t}/{rng.randint(0, i)}"
                    assert zw.read(other) == payload[other]
            except Exception as exc:                    # noqa: BLE001 - collected
                errors.append(exc)
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert not errors
    with zipfile.ZipFile(tmp_path / "x.zip") as zf:
        assert zf.testzip() is None
        assert {i.filename: zf.read(i) for i in zf.infolist()} == payload


def test_an_error_leaves_no_zip_and_no_part(tmp_path):
    with pytest.raises(RuntimeError):
        with ZipWriter(tmp_path / "x.zip") as zw:
            zw.write("a", b"1")
            raise RuntimeError("stop")
    assert list(tmp_path.iterdir()) == []


def test_closed_writer_refuses_writes(tmp_path):
    zw = ZipWriter(tmp_path / "x.zip")
    zw.close()
    with pytest.raises(ValueError, match="closed"):
        zw.write("a", b"1")


def test_an_existing_zip_is_refused_unless_overwrite(tmp_path):
    (tmp_path / "x.zip").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        ZipWriter(tmp_path / "x.zip")
    with ZipWriter(tmp_path / "x.zip", overwrite=True) as zw:
        zw.write("a", b"1")
    assert _entries(tmp_path / "x.zip") == {"a": b"1"}


@pytest.mark.parametrize("key", ["", "../x", "a/../b", "a//b", "."])
def test_keys_outside_the_store_are_refused(tmp_path, key):
    with ZipWriter(tmp_path / "x.zip") as zw:
        with pytest.raises(ValueError, match="not a store key"):
            zw.write(key, b"1")


def test_force_zip64_and_comment(tmp_path):
    with ZipWriter(tmp_path / "x.ozx", comment=rfc9_comment("0.5"), force_zip64=True) as zw:
        zw.write("zarr.json", b"{}")
    with zipfile.ZipFile(tmp_path / "x.ozx") as zf:
        assert b'"jsonFirst": true' in zf.comment
        assert zf.read("zarr.json") == b"{}"


@pytest.mark.parametrize("root", ["", "img.ome.zarr"])
def test_zarr_reads_a_store_written_in_any_order(tmp_path, root):
    """Copy a store written by zarr into a ZipWriter data first and metadata last (as a
    pyramid writer does), rewriting zarr.json on the way: zarr's ZipStore reads it."""
    zarr = pytest.importorskip("zarr")
    np = pytest.importorskip("numpy")
    src = tmp_path / "src.zarr"
    group = zarr.open_group(str(src), mode="w", zarr_format=3)
    arr = group.create_array("0", shape=(4, 8, 8), chunks=(2, 4, 4), dtype="uint16")
    arr[:] = np.arange(4 * 8 * 8, dtype="uint16").reshape(4, 8, 8)
    group.attrs["ome"] = {"version": "0.5"}
    files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
    data = [f for f in files if not f.endswith("zarr.json")]
    meta = [f for f in files if f.endswith("zarr.json")]

    with ZipWriter(tmp_path / "out.zip", root=root) as zw:
        zw.write("zarr.json", b"{}")                    # a placeholder, rewritten below
        for f in data + meta:
            zw.write_from(f, src / f)
    store = zarr.storage.ZipStore(str(tmp_path / "out.zip"), mode="r")
    back = zarr.open_group(store, mode="r", path=root)
    assert back.attrs["ome"]["version"] == "0.5"
    np.testing.assert_array_equal(back["0"][:], arr[:])


def _outside_tar():
    """A tar that reads zips (bsdtar: Windows' and macOS' tar), or None."""
    if sys.platform == "win32":                     # not Git's GNU tar, which may come first
        tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
        return str(tar) if tar.is_file() else None
    if sys.platform == "darwin":
        return "/usr/bin/tar"
    return shutil.which("bsdtar")


def test_an_outside_tool_extracts_it_byte_for_byte(tmp_path):
    tar = _outside_tar()
    if tar is None:
        pytest.skip("no bsdtar here")
    payload = {"zarr.json": b"{}", "0/zarr.json": b"[]", "0/c/0": os.urandom(70_000)}
    with ZipWriter(tmp_path / "x.zip", root="x.zarr") as zw:
        for key in ("0/c/0", "0/zarr.json", "zarr.json", "zarr.json"):
            zw.write(key, payload[key])
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run([tar, "-xf", str(tmp_path / "x.zip"), "-C", str(out)], check=True)
    got = {p.relative_to(out / "x.zarr").as_posix(): p.read_bytes()
           for p in (out / "x.zarr").rglob("*") if p.is_file()}
    assert got == payload


def test_a_read_does_not_wait_for_a_long_append(tmp_path):
    """Reading level 0 back while a large shard is being copied in must not block."""
    started, release = threading.Event(), threading.Event()

    with ZipWriter(tmp_path / "x.zip") as zw:
        zw.write("done", b"ready")
        original = zw._append

        def slow_append(key, info, fill):
            def slow_fill(out):
                started.set()
                assert release.wait(10)
                fill(out)
            return original(key, info, slow_fill)
        zw._append = slow_append
        writer = threading.Thread(target=zw.write, args=("big", b"x" * 1000))
        writer.start()
        assert started.wait(10)
        assert zw.read("done") == b"ready"          # while the append holds its lock
        release.set()
        writer.join()
        zw._append = original
    assert _entries(tmp_path / "x.zip") == {"done": b"ready", "big": b"x" * 1000}
