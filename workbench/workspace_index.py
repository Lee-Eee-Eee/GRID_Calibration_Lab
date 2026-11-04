"""Small personal-workspace references linking shared inputs and saved runs.

Only saved JSON in this workspace is inspected. Event tables and external data
directories are never scanned when listing the index.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def source_identity(dataset):
    """Identify the acquisition independently of its display label/history choice.

    DatasetCatalog has already normalized these paths. Avoid resolve() here:
    browsing a saved index must also work while a shared mount is unavailable.
    """
    path = (dataset or {}).get("raw_dir") or (dataset or {}).get("product_dir")
    if not path:
        return None
    role = "raw" if dataset.get("raw_dir") else "product"
    normalized = os.path.normcase(os.path.normpath(str(path)))
    return hashlib.sha256(f"{role}:{normalized}".encode()).hexdigest()[:20]


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class WorkspaceIndex:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.runs = self.workspace / "runs"
        self.directory = self.runs / "_index"

    @staticmethod
    def _write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _reference(self, value):
        if not value:
            return None
        # Do not resolve references through shared-data symlinks.
        path = Path(value)
        if not path.is_absolute():
            path = self.workspace / path
        try:
            return {"scope": "workspace", "path": path.relative_to(self.workspace).as_posix()}
        except ValueError:
            return {"scope": "external", "path": str(path)}

    def update_run(self, state):
        """Update one small summary; no product traversal or source access."""
        run_id = state["id"]
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Invalid run ID")
        root = self.runs / run_id
        dataset = state.get("dataset") or {}
        jobs = {}
        for job in state.get("jobs", []):
            layer = job.get("layer")
            if layer:
                jobs[layer] = {key: job.get(key) for key in (
                    "id", "layer", "status", "created_at", "finished_at", "config_revision",
                )}
        record = {
            "id": run_id,
            "name": state.get("name"),
            "payload": state["payload"],
            "dataset_id": dataset.get("id"),
            "source_id": dataset.get("source_id") or source_identity(dataset),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "source": {key: dataset.get(key) for key in (
                "raw_dir", "product_dir", "source_payload", "profile",
            )},
            "products_dir": str(root / "products" / state["payload"]),
            "references": {name: self._reference(path) for name, path in (
                ("state", root / "state.json"),
                ("products", root / "products" / state["payload"]),
                ("imports", root / "imports.json"),
                ("contract_import", root / "contract-import.json"),
                ("revisions", root / "revisions"),
                ("jobs", root / "jobs"),
            )},
            "acquisition_preset": copy.deepcopy(state.get("acquisition_preset")),
            "acquisition": copy.deepcopy(state.get("acquisition")),
            "config_revision": state.get("config_revision", 0),
            "tb_binding": state.get("tb_binding"),
            "selected_tb": copy.deepcopy(state.get("selected_tb")),
            "ec_dependency": copy.deepcopy(state.get("ec_dependency")),
            "dirty_layers": list(state.get("dirty_layers", [])),
            "latest_jobs": jobs,
        }
        self._write(self.directory / "runs" / f"{run_id}.json", record)
        return record

    def get(self):
        """Return current registered inputs, versions and run references."""
        datasets = _read(self.runs / "datasets.json", {"datasets": [], "defaults": {}})
        presets = _read(self.runs / "_presets" / "index.json", {"presets": [], "bindings": []})
        runs, errors = [], []
        for path in sorted((self.directory / "runs").glob("*.json")):
            try:
                record = _read(path, None)
                if record:
                    runs.append(record)
            except (OSError, ValueError, KeyError) as exc:
                errors.append({"path": str(path), "message": str(exc)})
        runs.sort(key=lambda row: (row.get("updated_at") or "", row["id"]), reverse=True)
        return {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "datasets": datasets.get("datasets", []),
            "defaults": datasets.get("defaults", {}),
            "presets": presets.get("presets", []),
            "bindings": presets.get("bindings", []),
            "runs": runs,
            "errors": errors,
        }

    def for_dataset(self, dataset):
        """Keep every matching trial; never replace the shared product choice."""
        source_id = dataset.get("source_id") or source_identity(dataset)
        product = dataset.get("product_dir")
        return [row for row in self.get()["runs"] if (
            source_id and row.get("source_id") == source_id
            or product and row.get("products_dir") == product
        )]

    def annotate_datasets(self, catalog):
        records = self.get()["runs"]
        for dataset in catalog["datasets"]:
            source_id = dataset.get("source_id") or source_identity(dataset)
            dataset["source_id"] = source_id
            dataset["personal_runs"] = [row for row in records if (
                source_id and row.get("source_id") == source_id
                or dataset.get("product_dir") == row.get("products_dir")
            )]
        return catalog

    def refresh(self):
        """Rebuild references from private state files, including old runs."""
        errors = []
        valid = set()
        for path in sorted(self.runs.glob("*/state.json")):
            try:
                state = _read(path, {})
                if state.get("id") != path.parent.name:
                    raise ValueError("Run directory and state ID differ")
                self.update_run(state)
                valid.add(state["id"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({"path": str(path), "message": str(exc)})
        # Removed trial folders should disappear from the personal catalog.
        for path in (self.directory / "runs").glob("*.json"):
            if path.stem not in valid and not (self.runs / path.stem / "state.json").exists():
                path.unlink()
        result = self.get()
        result["errors"].extend(errors)
        result["refreshed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write(self.directory / "workspace.json", result)
        return result
