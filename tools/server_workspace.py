"""Prepare a private workbench workspace beside shared cali_proc data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..common.cali_contract import build_acquisition_profile
from ..workbench.service import Workbench


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
    workbench = Workbench(workspace)
    result = {"workspace": str(workspace), "datasets": [], "runs": []}
    try:
        for payload in ("14B", "15B"):
            raw, products = data_root / "raw" / payload, data_root / payload
            catalog = workbench.scan_datasets(raw_dir=str(raw), product_dir=str(products), payload=payload)
            dataset = next(row for row in catalog["datasets"] if row["payload"] == payload
                           and Path(row["raw_dir"]).resolve() == raw.resolve())
            profile = build_acquisition_profile(payload, raw)
            existing = next((row for row in workbench.acquisition_presets(payload)["presets"]
                             if row["name"] == f"{payload} cali_proc 0.5"), None)
            preset = workbench.register_acquisition_preset({
                "profile": profile, "name": f"{payload} cali_proc 0.5", "payload": payload,
                "preset_id": existing["id"] if existing else None,
                "bindings": [{"role": "raw", "path": str(raw)}, {"role": "product", "path": str(products)}],
            })
            result["datasets"].append({"payload": payload, "id": dataset["id"], "preset": preset})
            if create_runs:
                run = workbench.create(payload, f"{payload} 新规范回归", dataset_id=dataset["id"])
                result["runs"].append({"payload": payload, "id": run["id"]})
        return result
    finally:
        workbench.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--create-runs", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args.workspace, args.data_root, create_runs=args.create_runs),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
