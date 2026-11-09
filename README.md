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
pip install git+https://github.com/Euro-BioImaging/zarr-zip
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
