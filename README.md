# zarr-zip

A small pure-Python library and command line for Zarr stores in zip files. It zips any Zarr store,
OME-Zarr or plain, one zip per store: in the layout the
[BioImage Archive](https://www.ebi.ac.uk/bioimage-archive/help-zarr/) asks for, or as an NGFF RFC-9 `.ozx`.

It was called `ome-zarr-zip` up to 0.0.1.

- Finds every OME-Zarr (or plain Zarr) under the input folder, subfolders included.
- Writes one zip per store, named after the corresponding folder. Unzipping it gives back the original folder.
- Does not recompress the data. Assumes Zarr chunks are already compressed.
- Skips zips that already exist.

## Install

```
pip install zarr-zip
```

## Use

Zip everything under a folder, putting each zip next to its image:

```
zarr-zip D:\results
```

Put the zips in another folder (the subfolders are kept):

```
zarr-zip D:\results -o E:\upload
```

See what it would do, without writing anything:

```
zarr-zip D:\results -o E:\upload --dry-run
```

Other options:

| Option | What it does |
|---|---|
| `-j 4` (or `--workers 4`) | zip 4 images in parallel (default 2). |
| `--overwrite` | replace zips that already exist |
| `--verify list` | check only file names and sizes after writing. This is faster than the default full check. |
| `--verify none` | skip the check |
| `--rename-folder` | rename an OME-Zarr folder `name.zarr` to `name.ome.zarr` inside the zip, giving `name.ome.zarr.zip` as the BioImage Archive asks (see below) |
| `--layout rfc9` | write `name.ozx` files instead (see below) |

From Python:

```python
from zarr_zip import find_stores, zip_store

for store in find_stores(r"D:\results"):
    zip_store(store, r"E:\upload")
```

## Writing a store straight into a zip

A program that writes a Zarr store can write it into a zip directly, without a folder first.
`ZipWriter` takes each finished chunk or shard as it comes, from any number of threads:

```python
from zarr_zip import open_writer

with open_writer("img.ome.zarr.zip") as zw:     # holds the folder img.ome.zarr (BioImage Archive)
    zw.write("0/c/0/0/0", shard_bytes)          # a whole chunk or shard, from memory
    zw.write_from("0/c/0/0/1", "shard.tmp")     # or from a file
    level0 = zw.read("0/c/0/0/0")               # finished entries can be read back
    zw.write("zarr.json", metadata_bytes)       # written twice? the last copy counts
```

`open_writer("img.ozx")` writes the RFC-9 layout instead: the store at the top of the zip, and
the comment naming the OME-Zarr version, taken from the root `zarr.json` when the zip is closed.

zarr-python can read and write through the writer too (`pip install "zarr-zip[zarr]"`):

```python
import zarr
from zarr_zip.zarr_store import WriterStore

group = zarr.open_group(WriterStore(zw), mode="w-", zarr_format=3)
level0 = zarr.open_array(WriterStore(zw, read_only=True), path="0")
```

- The zip is written as `img.ome.zarr.zip.part` and gets its name only when it is complete and
  checked. If the program stops, no zip that looks finished is left behind.
- Entries are stored uncompressed (Zarr chunks are compressed already), so reading one back
  while the zip is still being written is a plain file read.
- The zip's table of contents lists each file once (its last copy) and the metadata files
  first, whatever order they were written in.
- Nothing can be deleted from a zip being written; a chunk or shard is best written once.

## The two layouts

**`bia`** (the default) follows the
[BioImage Archive's rules](https://www.ebi.ac.uk/bioimage-archive/help-zarr/):

- Each zip holds the original folder under its own name and is named after it. An OME-Zarr folder named `name.zarr` therefore becomes `name.zarr.zip`. 
- Add `--rename-folder` to rename the input folders named like `name.zarr` to `name.ome.zarr` inside the zip. The zip file itself also becomes `name.ome.zarr.zip`. 
- The BioImage Archive advises keeping each zip below 100 GB, and the tool warns when a zip is larger.

**`rfc9`** follows [NGFF RFC-9](https://ngff.openmicroscopy.org/rfc/9/index.html):

- The zip is named `name.ozx`.
- Its top level is the image itself, with `zarr.json` at the root and no folder around it.
- It uses ZIP64 entries.
- A comment in the zip names the OME-Zarr version.
- It works only for OME-Zarr 0.5 and newer.


## Tests

```
pip install -e .[test]
pytest
```
