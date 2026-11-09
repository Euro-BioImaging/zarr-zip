"""Command line: ``zarr-zip INPUT [INPUT ...] [-o OUTPUT] [--layout bia|rfc9]``."""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import (BIA_ADVISED_MAX, LAYOUTS, __version__, _store_size, bia_folder, find_stores,
               follows_bia_naming, format_size, inner_folder, iter_plan, zip_name, zip_store)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zarr-zip",
        description="Zip every Zarr store (OME-Zarr or plain) found under the inputs, one zip per store.")
    p.add_argument("inputs", nargs="+", type=Path,
                   help="Zarr or OME-Zarr stores, or folders searched for them (subfolders included)")
    p.add_argument("-o", "--output", type=Path,
                   help="folder for the zips; the subfolders below each input are kept "
                        "(default: each zip next to its store)")
    p.add_argument("--layout", choices=LAYOUTS, default="bia",
                   help="bia: X.zip holding the folder X as it is (BioImage Archive; default); "
                        "rfc9: X.ozx with the Zarr root at the zip root (NGFF RFC-9 draft)")
    p.add_argument("--rename-folder", action="store_true",
                   help="bia: rename an OME-Zarr folder X.zarr to X.ome.zarr inside the zip, giving "
                        "X.ome.zarr.zip as the BioImage Archive asks (default: keep the store's name)")
    p.add_argument("--verify", choices=("full", "list", "none"), default="full",
                   help="after writing: full = read back and check every CRC (default), "
                        "list = names and sizes only, none = no check")
    p.add_argument("-j", "--workers", type=int, default=2,
                   help="stores zipped at once (default 2; disks rarely gain from more)")
    p.add_argument("--overwrite", action="store_true", help="replace zips that exist (default: skip)")
    p.add_argument("-n", "--dry-run", action="store_true", help="list what would be zipped, write nothing")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    plan = []
    for root in args.inputs:
        if not root.exists():
            print(f"error: {root} does not exist", file=sys.stderr)
            return 2
        stores = find_stores(root)
        if not stores:
            print(f"warning: no Zarr store found under {root}", file=sys.stderr)
        plan.extend(iter_plan(stores, args.layout, args.output, root, args.rename_folder))
    if not plan:
        return 1

    todo, skipped = [], 0
    for store, target in plan:
        if args.layout == "rfc9" and store.zarr_format != 3:
            print(f"skip  {store.path}: Zarr v2 (OME-Zarr 0.4 or older) cannot be an RFC-9 zip; "
                  f"use --layout bia", file=sys.stderr)
            skipped += 1
        elif target.exists() and not args.overwrite:
            print(f"skip  {target} (exists; --overwrite replaces it)")
            skipped += 1
        else:
            todo.append((store, target))

    off_name = [store for store, _ in todo
                if not follows_bia_naming(store, args.layout, args.rename_folder)]
    if off_name:
        first = off_name[0]
        print(f"warning: {len(off_name)} zip(s) will not be named as the BioImage Archive asks "
              f"(X.ome.zarr.zip holding X.ome.zarr/ for an OME-Zarr), e.g. "
              f"{zip_name(first, args.layout)}. Pass --rename-folder to rename the folder inside "
              f"each zip, and so the zip, to {bia_folder(first)}/ and {bia_folder(first)}.zip "
              f"(the data stays the same).", file=sys.stderr)

    if args.dry_run:
        for store, target in todo:
            kind = f"OME-Zarr {store.ome_version}" if store.is_ome else f"Zarr v{store.zarr_format}"
            inner = inner_folder(store, args.layout, args.rename_folder)
            into = f", unpacks to {inner}/" if inner else ""
            print(f"would zip  {store.path}  ->  {target}  ({kind}{into})")
        print(f"{len(todo)} to zip, {skipped} skipped")
        return 0

    total = len(todo)
    failed = []
    done = [0]
    start = time.perf_counter()

    def run(item):
        store, target = item
        t0 = time.perf_counter()
        try:
            zip_store(store, target, layout=args.layout, overwrite=args.overwrite, verify=args.verify,
                      rename_folder=args.rename_folder)
        except Exception as exc:                    # noqa: BLE001 - reported, the batch goes on
            failed.append((store.path, exc))
            done[0] += 1
            print(f"[{done[0]}/{total}] FAILED {store.path}: {exc}", file=sys.stderr, flush=True)
            return
        size = target.stat().st_size
        done[0] += 1
        note = ""
        if args.layout == "bia" and size > BIA_ADVISED_MAX:
            note = "  (over 100 GB: the BioImage Archive advises smaller zips)"
        print(f"[{done[0]}/{total}] {target}  {format_size(size)}  "
              f"{time.perf_counter() - t0:.1f} s{note}", flush=True)

    # largest first, so one big store does not run alone at the end
    todo.sort(key=lambda item: _store_size(item[0]), reverse=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        list(pool.map(run, todo))

    print(f"{total - len(failed)} zipped, {skipped} skipped, {len(failed)} failed "
          f"in {time.perf_counter() - start:.0f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
