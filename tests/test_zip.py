import json
import zipfile
from pathlib import Path

import pytest

from zarr_zip import (find_stores, follows_bia_naming, inner_folder, read_store, store_files,
                          zip_name, zip_store)
from zarr_zip.__main__ import main


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        data = json.dumps(data)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def make_v3(root: Path, ome=True):
    """A small OME-Zarr 0.5 image written with zarr (so a real reader can open the zip)."""
    zarr = pytest.importorskip("zarr")
    import numpy as np
    group = zarr.open_group(str(root), mode="w", zarr_format=3)
    arr = group.create_array("0", shape=(4, 8, 8), chunks=(2, 4, 4), dtype="uint16")
    arr[:] = np.arange(4 * 8 * 8, dtype="uint16").reshape(4, 8, 8)
    if ome:
        group.attrs["ome"] = {"version": "0.5", "multiscales": [{"datasets": [{"path": "0"}]}]}
    return root


def make_v2(root: Path):
    _write(root / ".zgroup", {"zarr_format": 2})
    _write(root / ".zattrs", {"multiscales": [{"version": "0.4", "datasets": [{"path": "0"}]}]})
    _write(root / "0" / ".zarray", {"zarr_format": 2, "shape": [2], "chunks": [2], "dtype": "<u1"})
    _write(root / "0" / "0", b"\x01\x02")
    return root


def test_find_stores_does_not_enter_stores(tmp_path):
    make_v3(tmp_path / "a" / "x 1.zarr")
    make_v2(tmp_path / "b" / "y.ome.zarr")
    (tmp_path / "c").mkdir()
    stores = find_stores(tmp_path)
    assert [s.path.name for s in stores] == ["x 1.zarr", "y.ome.zarr"]
    assert [(s.zarr_format, s.ome_version) for s in stores] == [(3, "0.5"), (2, "0.4")]
    assert find_stores(tmp_path / "a" / "x 1.zarr")[0].path.name == "x 1.zarr"


def test_names():
    from zarr_zip import Store
    ome, ome_named, plain = (Store(Path("x.zarr"), 3, "0.5"), Store(Path("x.ome.zarr"), 3, "0.5"),
                             Store(Path("x.zarr"), 3, None))
    assert zip_name(ome) == "x.zarr.zip"                            # the folder, as it is
    assert zip_name(ome, rename_folder=True) == "x.ome.zarr.zip"
    assert zip_name(ome_named) == zip_name(ome_named, rename_folder=True) == "x.ome.zarr.zip"
    assert zip_name(plain) == zip_name(plain, rename_folder=True) == "x.zarr.zip"
    assert zip_name(ome_named, "rfc9") == "x.ozx"
    assert [follows_bia_naming(s) for s in (ome, ome_named, plain)] == [False, True, True]
    assert follows_bia_naming(ome, rename_folder=True)


def test_metadata_first_breadth_first(tmp_path):
    store = read_store(make_v3(tmp_path / "s.zarr"))
    files = store_files(store)
    assert files[:2] == ["zarr.json", "0/zarr.json"]
    assert all(not f.endswith("zarr.json") for f in files[2:])


@pytest.mark.parametrize("rename_folder, inner", [(False, "img.zarr"), (True, "img.ome.zarr")])
def test_bia_layout_folder_name(tmp_path, rename_folder, inner):
    store = read_store(make_v3(tmp_path / "img.zarr"))
    assert follows_bia_naming(store, rename_folder=rename_folder) is rename_folder
    target = zip_store(store, tmp_path / "out", rename_folder=rename_folder)
    assert target.name == inner + ".zip"
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        assert names[0] == f"{inner}/zarr.json"
        assert all(n.startswith(f"{inner}/") for n in names)
        assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_STORED}
        zf.extractall(tmp_path / "unzipped")
    zarr = pytest.importorskip("zarr")
    group = zarr.open_group(str(tmp_path / "unzipped" / inner), mode="r")
    assert int(group["0"][3, 7, 7]) == 4 * 8 * 8 - 1


def test_ome_zarr_named_store_follows_bia_without_rename(tmp_path):
    store = read_store(make_v2(tmp_path / "img.ome.zarr"))
    assert follows_bia_naming(store)
    assert inner_folder(store) == "img.ome.zarr"
    assert zip_name(store) == "img.ome.zarr.zip"


def test_rfc9_layout_opens_with_zarr_zipstore(tmp_path):
    zarr = pytest.importorskip("zarr")
    store = read_store(make_v3(tmp_path / "img.ome.zarr"))
    target = zip_store(store, tmp_path / "out", layout="rfc9")
    assert target.name == "img.ozx"
    with zipfile.ZipFile(target) as zf:
        assert zf.namelist()[:2] == ["zarr.json", "0/zarr.json"]
        assert json.loads(zf.comment) == {
            "ome": {"version": "0.5", "zipFile": {"centralDirectory": {"jsonFirst": True}}}}
    zs = zarr.storage.ZipStore(str(target), mode="r")
    group = zarr.open_group(zs, mode="r")
    assert group.attrs["ome"]["version"] == "0.5"
    assert int(group["0"][3, 7, 7]) == 4 * 8 * 8 - 1


@pytest.mark.parametrize("layout", ["bia", "rfc9"])
def test_extracted_files_are_byte_identical(tmp_path, layout):
    src = make_v3(tmp_path / "img.zarr")
    _write(src / "OME" / "zarr.json", {"zarr_format": 3, "node_type": "group"})
    target = zip_store(read_store(src), tmp_path / "out", layout=layout)
    with zipfile.ZipFile(target) as zf:
        zf.extractall(tmp_path / "x")
    root = tmp_path / "x" / "img.zarr" if layout == "bia" else tmp_path / "x"

    def contents(top):
        return {p.relative_to(top).as_posix(): p.read_bytes() for p in top.rglob("*") if p.is_file()}

    assert contents(root) == contents(src)


def test_rfc9_refuses_zarr_v2(tmp_path):
    store = read_store(make_v2(tmp_path / "old.zarr"))
    with pytest.raises(ValueError, match="Zarr v3"):
        zip_store(store, layout="rfc9")


def test_existing_zip_is_not_replaced_and_no_part_left(tmp_path):
    store = read_store(make_v2(tmp_path / "old.zarr"))
    target = zip_store(store)
    assert target == tmp_path / "old.zarr.zip"
    with pytest.raises(FileExistsError):
        zip_store(store)
    zip_store(store, overwrite=True)
    assert not list(tmp_path.glob("*.part"))


def test_cli_keeps_subfolders_and_skips_existing(tmp_path, capsys):
    make_v2(tmp_path / "in" / "plate A" / "one.zarr")
    make_v2(tmp_path / "in" / "plate B" / "two.zarr")
    out = tmp_path / "zips"
    assert main([str(tmp_path / "in"), "-o", str(out)]) == 0
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.zip")) == [
        "plate A/one.zarr.zip", "plate B/two.zarr.zip"]
    err = capsys.readouterr().err
    assert "2 zip(s) will not be named as the BioImage Archive asks" in err
    assert "--rename-folder" in err
    assert main([str(tmp_path / "in"), "-o", str(out)]) == 0
    assert "2 skipped" in capsys.readouterr().out


def test_cli_rename_folder_does_not_warn(tmp_path, capsys):
    make_v2(tmp_path / "in" / "one.zarr")
    assert main([str(tmp_path / "in"), "--rename-folder"]) == 0
    assert "warning" not in capsys.readouterr().err
    with zipfile.ZipFile(tmp_path / "in" / "one.ome.zarr.zip") as zf:
        assert zf.namelist()[0] == "one.ome.zarr/.zattrs"
