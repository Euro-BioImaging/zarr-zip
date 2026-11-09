"""open_writer (both layouts), byte ranges, and zarr-python through WriterStore."""
import json
import zipfile

import pytest

from zarr_zip import ZipWriter, layout_of, open_writer


def test_byte_ranges(tmp_path):
    with ZipWriter(tmp_path / "x.zip") as zw:
        zw.write("a", b"0123456789")
        assert zw.read("a", 2, 5) == b"234"
        assert zw.read("a", 7) == b"789"
        assert zw.read("a", -3) == b"789"
        assert zw.read("a", 8, 100) == b"89"
        assert zw.read("a", 20) == b""


def test_layout_from_the_name():
    assert layout_of("x.ozx") == "rfc9"
    assert layout_of("x.ome.zarr.zip") == "bia"


def test_bia_folder_is_the_zip_name_without_zip(tmp_path):
    with open_writer(tmp_path / "img.ome.zarr.zip") as zw:
        zw.write("zarr.json", b"{}")
    with zipfile.ZipFile(tmp_path / "img.ome.zarr.zip") as zf:
        assert zf.namelist() == ["img.ome.zarr/zarr.json"]
        assert zf.comment == b""


def test_rfc9_comment_comes_from_the_root_zarr_json_at_close(tmp_path):
    with open_writer(tmp_path / "img.ozx") as zw:
        zw.write("zarr.json", json.dumps({"attributes": {"ome": {"version": "0.4"}}}).encode())
        zw.write("zarr.json", json.dumps({"attributes": {"ome": {"version": "0.5"}}}).encode())
    with zipfile.ZipFile(tmp_path / "img.ozx") as zf:
        assert zf.namelist() == ["zarr.json"]
        assert json.loads(zf.comment)["ome"]["version"] == "0.5"


def test_rfc9_without_a_root_zarr_json_is_refused_and_leaves_nothing(tmp_path):
    zw = open_writer(tmp_path / "img.ozx")
    zw.write(".zattrs", b"{}")
    with pytest.raises(ValueError, match="Zarr v3"):
        zw.close()
    assert list(tmp_path.iterdir()) == []


def test_rfc9_takes_no_root(tmp_path):
    with pytest.raises(ValueError, match="root"):
        open_writer(tmp_path / "img.ozx", root="img.ome.zarr")


@pytest.mark.parametrize("name", ["img.ome.zarr.zip", "img.ozx"])
def test_zarr_writes_and_reads_through_the_writer(tmp_path, name):
    zarr = pytest.importorskip("zarr")
    np = pytest.importorskip("numpy")
    from zarr_zip.zarr_store import WriterStore

    data = np.arange(6 * 10 * 10, dtype="uint16").reshape(6, 10, 10)
    with open_writer(tmp_path / name) as zw:
        store = WriterStore(zw)
        group = zarr.open_group(store, mode="w-", zarr_format=3)
        arr = group.create_array("0", shape=data.shape, chunks=(2, 5, 5), shards=(2, 10, 10),
                                 dtype="uint16")
        arr[:] = data
        group.attrs["ome"] = {"version": "0.5"}            # zarr.json written again
        # read back before the zip is closed: partial reads of the shards
        back = zarr.open_array(WriterStore(zw, read_only=True), path="0", mode="r")
        np.testing.assert_array_equal(back[1:5, 3:9, 2:7], data[1:5, 3:9, 2:7])
        assert sorted(zarr.open_group(store, mode="r").array_keys()) == ["0"]
        with pytest.raises(NotImplementedError):
            import asyncio
            asyncio.run(store.delete("zarr.json"))

    zipped = zarr.storage.ZipStore(str(tmp_path / name), mode="r")
    root = "img.ome.zarr" if name.endswith(".zip") else ""
    final = zarr.open_group(zipped, mode="r", path=root)
    assert final.attrs["ome"]["version"] == "0.5"
    np.testing.assert_array_equal(final["0"][:], data)
