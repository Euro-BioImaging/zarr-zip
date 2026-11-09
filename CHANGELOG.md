# Changelog

## 0.1.0

The first release on PyPI, under a new name.

- Renamed from `ome-zarr-zip` to `zarr-zip` (import `zarr_zip`, command `zarr-zip`): it zips any
  Zarr store, OME-Zarr or plain, and is the zip library dyna-zarr writes through.
- New: `ZipWriter` writes a store into a zip while the store is being written: whole chunks or
  shards from memory or from a file, from several threads; finished entries can be read back
  before the zip is closed; a file written twice is listed once (its last copy); metadata files
  are listed first; the zip is `<name>.part` until it is complete and checked.
- New: `open_writer(path)` opens a `ZipWriter` in the layout the name asks for: `X.ome.zarr.zip`
  holds the folder `X.ome.zarr` (BioImage Archive), `X.ozx` the store at its root with RFC-9's
  comment, which is filled in from the root `zarr.json` when the zip is closed.
- New: `ZipWriter.read(key, start, end)` reads part of an entry.
- New: `zarr_zip.zarr_store.WriterStore`, a zarr-python store over a `ZipWriter`: zarr reads a
  zip while it is being written, and writes into it (`pip install "zarr-zip[zarr]"`).
- `zip_store` (and the command line) now write through `ZipWriter`.
- Requires Python 3.11 or newer.

## 0.0.1

- `ome-zarr-zip`: zip every OME-Zarr store under a folder, one zip per store, in the BioImage
  Archive layout or as NGFF RFC-9 `.ozx`; checks each zip after writing.
