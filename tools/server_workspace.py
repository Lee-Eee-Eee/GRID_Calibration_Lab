"""Prepare a private workbench workspace beside shared cali_proc data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..common.cali_contract import build_acquisition_profile
from ..workbench.datasets import DatasetCatalog
from ..workbench.presets import PresetRegistry
from ..workbench.workspace_index import WorkspaceIndex


def prepare(workspace: Path, data_root: Path, *, create_runs: bool = False) -> dict:
    workspace, data_root = workspace.expanduser().resolve(), data_root.expanduser().resolve()
    if workspace == data_root or data_root in workspace.parents:
        raise ValueError("Use a private workspace outside the shared production tree")
    workspace.mkdir(parents=True, exist_ok=True)
    for name, target in (("raw", data_root / "raw"), ("shared-products", data_root)):
        link = workspace / name
        if not target.is_dir():
            raise FileNotFoundError(target)
        if link.exists() or link.is_symlink():
            if link.resolve() != target.resolve():
                raise ValueError(f"Existing {link} points elsewhere")
        else:
            link.symlink_to(target, target_is_directory=True)
    catalog = DatasetCatalog(workspace)
    registry = PresetRegistry(workspace)
    catalog.set_defaults(data_root / "raw", data_root)
    index = WorkspaceIndex(workspace)
    result = {"workspace": str(workspace), "datasets": [], "runs": []}
    for payload in ("14B", "15B"):
        raw, products = data_root / "raw" / payload, data_root / payload
        scanned = catalog.scan(raw_dir=str(raw), product_dir=str(products), payload=payload)
        dataset = next(row for row in scanned["datasets"] if row["payload"] == payload
                       and Path(row["raw_dir"]).resolve() == raw.resolve())
        match = registry.match(dataset)
        if match["status"] == "conflict":
            raise ValueError(match["warnings"][0])
        if match["reference"]:
            selected = registry.get(match["reference"]["id"], match["reference"]["version"])
        else:
            profile = build_acquisition_profile(payload, raw)
            existing = next((row for row in registry.list(payload)["presets"]
                             if row["name"] == f"{payload} cali_proc 0.5"), None)
            selected = registry.get(existing["id"]) if existing else registry.save(
                profile=profile, name=f"{payload} cali_proc 0.5", payload=payload,
                bindings=[{"role": "raw", "path": str(raw)}, {"role": "product", "path": str(products)}],
            )
        # Re-running deployment preparation preserves the selected scientific
        # version. Publishing a changed configuration is a separate operation.
        result["datasets"].append({"payload": payload, "id": dataset["id"],
                                   "source_id": dataset["source_id"], "preset": selected["reference"]})
    index.refresh()
    if create_runs:
        # Creation needs the service's import/processor routing. Acquire its
        # normal lock first so an active service cannot be reconciled here.
        from ..workbench.server import workspace_lock
        from ..workbench.service import Workbench

        with workspace_lock(workspace):
            workbench = Workbench(workspace)
            try:
                for dataset in result["datasets"]:
                    definition = next(row for row in catalog.saved()["datasets"]
                                      if row["id"] == dataset["id"])
                    existing_runs = index.for_dataset(definition)
                    if existing_runs:
                        run_id, created = existing_runs[0]["id"], False
                    else:
                        run = workbench.create(dataset["payload"], f"{dataset['payload']} 标定",
                                               dataset_id=dataset["id"])
                        index.update_run(workbench.read(run["id"]))
                        run_id, created = run["id"], True
                    result["runs"].append({"payload": dataset["payload"], "id": run_id, "created": created})
            finally:
                workbench.close()
    result["index"] = str(index.directory / "workspace.json")
    index.refresh()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--create-runs", action="store_true")
    parser.add_argument("--refresh-index", action="store_true",
                        help="Rebuild personal run references without starting processors")
    args = parser.parse_args()
    if args.refresh_index:
        result = WorkspaceIndex(args.workspace).refresh()
        print(json.dumps({"workspace": result["workspace"], "runs": len(result["runs"]),
                          "datasets": len(result["datasets"]), "errors": result["errors"]},
                         ensure_ascii=False, indent=2))
    else:
        if args.data_root is None:
            parser.error("--data-root is required unless --refresh-index is selected")
        print(json.dumps(prepare(args.workspace, args.data_root, create_runs=args.create_runs),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
