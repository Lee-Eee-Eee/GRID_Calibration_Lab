"""Shared acquisitions remain separate from every personal trial/version."""

import copy
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from calibration_lib.common.cali_contract import build_acquisition_profile
from calibration_lib.tools import server_workspace
from calibration_lib.workbench.acquisition_store import prepare_profile
from calibration_lib.workbench.datasets import DatasetCatalog
from calibration_lib.workbench.presets import PresetRegistry
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import Workbench
from calibration_lib.workbench.workspace_index import WorkspaceIndex, source_identity


def save_state(workspace, run_id, dataset, **changes):
    root = workspace / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    state = {
        "id": run_id, "payload": "15B", "dataset": copy.deepcopy(dataset),
        "name": run_id, "updated_at": "2026-11-02T12:00:00Z",
        "acquisition_preset": {"id": "preset", "version": 1},
        "selected_tb": {"path": "/shared/15B/tb/L2/fixed.json", "sha256": "tb-version"},
        "ec_dependency": {"sha256": "tb-version"},
        "jobs": [{"id": "fit", "layer": "EC_L2", "status": "success"}],
        **changes,
    }
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def test_one_shared_dataset_maps_to_all_private_versions_after_reopen(tmp_path):
    workspace = tmp_path / "personal"
    workspace.mkdir()
    shared = tmp_path / "shared"
    raw, products = shared / "raw/15B", shared / "15B"
    (raw / "source").mkdir(parents=True)
    (products / "ec").mkdir(parents=True)
    source_json = products / "source.json"
    source_json.write_text('{"shared":true}', encoding="utf-8")
    original = source_json.read_bytes()
    catalog = DatasetCatalog(workspace)
    dataset = catalog.scan(raw_dir=str(raw), product_dir=str(products), payload="15B")["datasets"][0]
    save_state(workspace, "baseline", dataset)
    save_state(workspace, "doublet", dataset,
               acquisition_preset={"id": "preset", "version": 2},
               updated_at="2026-11-02T13:00:00Z")
    index = WorkspaceIndex(workspace)
    result = index.refresh()
    assert [row["id"] for row in result["runs"]] == ["doublet", "baseline"]
    assert [row["acquisition_preset"]["version"] for row in result["runs"]] == [2, 1]
    assert result["runs"][0]["references"]["products"] == {
        "scope": "workspace", "path": "runs/doublet/products/15B",
    }
    assert result["runs"][0]["ec_dependency"]["sha256"] == "tb-version"
    reopened = WorkspaceIndex(workspace)
    annotated = reopened.annotate_datasets(catalog.saved())["datasets"][0]
    assert annotated["product_dir"] == str(products)
    assert [row["id"] for row in annotated["personal_runs"]] == ["doublet", "baseline"]
    assert all(row["products_dir"].startswith(str(workspace)) for row in annotated["personal_runs"])
    assert source_json.read_bytes() == original
    assert sorted(p.relative_to(shared).as_posix() for p in shared.rglob("*") if p.is_file()) == [
        "15B/source.json",
    ]


def test_source_identity_ignores_output_name_and_history_choice_keeps_legacy_id(tmp_path):
    raw = tmp_path / "shared/raw/15B"
    (raw / "source").mkdir(parents=True)
    catalog = DatasetCatalog(tmp_path / "personal")
    first = catalog.resolve({"payload": "15B", "raw_dir": str(raw)})
    renamed = catalog.resolve({"payload": "15B-doublet", "raw_dir": str(raw)})
    assert first["id"] != renamed["id"]
    assert first["source_id"] == renamed["source_id"]
    # Adding a private trial as history does not create another acquisition.
    assert source_identity({**first, "product_dir": "/personal/run/products/15B"}) == first["source_id"]
    legacy = {key: value for key, value in first.items() if key != "source_id"}
    save_state(catalog.workspace, "old", legacy)
    assert WorkspaceIndex(catalog.workspace).refresh()["runs"][0]["source_id"] == first["source_id"]


def test_saved_index_lists_and_updates_without_traversing_products_or_shared_mount(tmp_path, monkeypatch):
    dataset = {"id": "saved", "raw_dir": "/unmounted/shared/raw/15B", "product_dir": "/unmounted/15B"}
    index = WorkspaceIndex(tmp_path)
    state = save_state(tmp_path, "trial", dataset)

    def no_walk(*args, **kwargs):
        raise AssertionError("Index summary must not traverse product/data files")

    monkeypatch.setattr(Path, "rglob", no_walk)
    index.update_run(state)
    selected = WorkspaceIndex(tmp_path).for_dataset(dataset)
    assert selected[0]["id"] == "trial"
    assert selected[0]["latest_jobs"]["EC_L2"]["status"] == "success"
    # Selecting the private products directory can also resolve its owner.
    assert index.for_dataset({"product_dir": selected[0]["products_dir"]})[0]["id"] == "trial"


def test_explicit_server_defaults_survive_no_argument_scan(tmp_path):
    raw = tmp_path / "shared/raw/15B"
    products = tmp_path / "shared/15B"
    (raw / "source").mkdir(parents=True)
    (products / "ec").mkdir(parents=True)
    catalog = DatasetCatalog(tmp_path / "personal")
    catalog.set_defaults(raw.parent, products.parent)
    result = DatasetCatalog(catalog.workspace).scan()
    assert result["defaults"] == {"raw_dir": str(raw.parent), "product_dir": str(products.parent)}
    assert len(result["datasets"]) == 1
    assert result["datasets"][0]["raw_dir"] == str(raw)
    assert result["datasets"][0]["product_dir"] == str(products)


def test_refresh_discovers_old_runs_and_removes_deleted_run_references(tmp_path):
    index = WorkspaceIndex(tmp_path)
    state = save_state(tmp_path, "old", {"raw_dir": "/shared/15B"})
    assert index.refresh()["runs"][0]["id"] == state["id"]
    (tmp_path / "runs/old/state.json").unlink()
    assert index.refresh()["runs"] == []


def test_prepare_preserves_newer_preset_and_does_not_start_service(tmp_path, monkeypatch):
    shared, workspace = tmp_path / "shared", tmp_path / "personal"
    for payload in ("14B", "15B"):
        (shared / "raw" / payload / "source").mkdir(parents=True)
        (shared / payload / "ec").mkdir(parents=True)
    # Some Windows hosts disallow symbolic link creation even for user data.
    try:
        probe = tmp_path / "probe"
        probe.symlink_to(shared, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("Symbolic links unavailable on this host")
    monkeypatch.setattr(server_workspace, "build_acquisition_profile", lambda payload, raw:
                        build_acquisition_profile(payload, raw, verify_files=False))
    first = server_workspace.prepare(workspace, shared)
    registry = PresetRegistry(workspace)
    ref = next(row for row in first["datasets"] if row["payload"] == "15B")["preset"]
    profile = registry.get(ref["id"], 1)["profile"]
    profile["analysis_config"]["ec_l2"]["fit"]["split_high_keV"] = 56.0
    second = registry.save(profile=profile, preset_id=ref["id"], bindings=[
        {"role": "raw", "path": str(shared / "raw/15B")},
        {"role": "product", "path": str(shared / "15B")},
    ])
    prepared_again = server_workspace.prepare(workspace, shared)
    selected = next(row for row in prepared_again["datasets"] if row["payload"] == "15B")["preset"]
    assert selected["version"] == 2
    assert selected["sha256"] == second["reference"]["sha256"]
    assert registry.get(ref["id"])["preset"]["latest_version"] == 2
    assert registry.get(ref["id"], 1)["profile"]["analysis_config"]["ec_l2"]["fit"]["split_high_keV"] == 55.0
    assert not list((workspace / "runs").glob("*/state.json"))


def test_http_index_and_directory_match_keep_two_versions_and_restart_without_source_scan(tmp_path, monkeypatch):
    workspace, shared = tmp_path / "personal", tmp_path / "shared"
    workspace.mkdir()
    raw, products = shared / "raw/DETECTOR-A", shared / "DETECTOR-A"
    (raw / "Temp_Vbias").mkdir(parents=True)
    (raw / "X_new").mkdir()
    (raw / "source").mkdir()
    (raw / "Temp_Vbias/20C_285_001.event.dat").write_bytes(b"events")
    (raw / "Temp_Vbias/20C_285_ecu_001.hk").write_bytes(b"hk")
    (products / "tb/L0/parquet").mkdir(parents=True)
    profile = prepare_profile(workspace, payload_profile="14B")
    profile["parser"]["conditions"].update(mode="fixed", temp=20., bias=28.5)
    service = Workbench(workspace)
    server = None
    try:
        selected = service.scan_datasets(raw_dir=str(raw), product_dir=str(products))["datasets"][0]
        initial = service.create(dataset_id=selected["id"])
        published = service.save_acquisition_preset(initial["id"], {"name": "Detector", "profile": profile})
        version_one = service.create(dataset_id=selected["id"])
        profile["parser"]["conditions"]["temp"] = 22.
        service.save_acquisition_preset(initial["id"], {
            "profile": profile, "preset_id": published["reference"]["id"], "reason": "New conditions",
        })
        version_two = service.create(dataset_id=selected["id"])
        annotated = service.scan_datasets(raw_dir=str(raw), product_dir=str(products))["datasets"][0]
        versions = {row["id"]: row["acquisition_preset"]["version"] for row in annotated["personal_runs"]}
        assert versions[version_one["id"]] == 1
        assert versions[version_two["id"]] == 2
        assert annotated["product_dir"] == str(products)
        assert annotated["source_id"] == selected["source_id"]
        assert annotated["acquisition_preset_match"]["reference"]["version"] == 2
        prior_products = service.root(version_one["id"]) / "products" / version_one["payload"]
        (prior_products / "tb").mkdir(exist_ok=True)
        product_selection = service.scan_datasets(product_dir=str(prior_products))["datasets"][0]
        assert product_selection["acquisition_preset_match"]["reference"]["version"] == 1
        assert [row["id"] for row in product_selection["personal_runs"]] == [version_one["id"]]

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/workspace-index")
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        assert response.status == 200
        records = {row["id"]: row for row in result["runs"]}
        assert records[version_one["id"]]["acquisition_preset"]["version"] == 1
        assert records[version_two["id"]]["acquisition_preset"]["version"] == 2
        assert records[version_one["id"]]["source_id"] == records[version_two["id"]]["source_id"]
    finally:
        if server:
            server.shutdown()
            thread.join()
            server.server_close()
        service.close()

    original_iterdir, original_glob = Path.iterdir, Path.glob

    def forbid_shared_iterator(path):
        if path.is_relative_to(shared):
            raise AssertionError("Reopening index must not enumerate shared data")
        return original_iterdir(path)

    def forbid_shared_glob(path, pattern):
        if path.is_relative_to(shared):
            raise AssertionError("Reopening index must not scan shared events")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "iterdir", forbid_shared_iterator)
    monkeypatch.setattr(Path, "glob", forbid_shared_glob)
    reopened = Workbench(workspace)
    try:
        result = reopened.bootstrap()
        selected_again = next(row for row in result["datasets"] if row["id"] == selected["id"])
        assert {row["id"] for row in selected_again["personal_runs"]} >= {
            version_one["id"], version_two["id"],
        }
        records = {row["id"]: row for row in reopened.personal_index()["runs"]}
        assert records[version_one["id"]]["acquisition_preset"]["version"] == 1
        assert records[version_two["id"]]["acquisition_preset"]["version"] == 2
    finally:
        reopened.close()
