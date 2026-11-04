"""Read-only layer references distinguish native EC products and result versions."""

import copy
import json
import os
from pathlib import Path

import pytest
from calibration_lib.workbench.layer_products import (
    LayerProductCatalog,
    corrected_tb_params,
    materialize_candidate,
    normalize_tb_aliases,
    tb_aliases,
)


def native_peak_fixture(root, family, center, channels=(0, 1)):
    """Small native spectrum with compatibility rows referring to that node."""
    import pandas as pd
    from calibration_lib.common.contract_import import import_contract_products

    node = root / "15B" / family / "L1/condition"
    points = {str(ch): {"channel": ch, "center": float(center + ch), "sigma": 2., "amplitude": 20.,
                         "window": [0., 400.], "energy_keV": 40., "exposure_unit": "s"}
              for ch in channels}
    write(node / "points.json", points)
    write(node / "meta.json", {"schema": f"g15b-{family}-l1@1", "kind": "xray", "measurement": "condition"})
    rows = [{"channel": ch, "x": float(center + offset), "density": float(center), "density_err": 1.,
             "bin_left": float(center + offset - 1), "bin_right": float(center + offset + 1)}
            for ch in channels for offset in (-1, 1)]
    pd.DataFrame(rows).to_parquet(node / "spectra.parquet", index=False)
    # Use the public importer in another directory, then move its small indexes
    # beside the fixture without copying any saved spectrum.
    indexed = root.parent / (root.name + "-indexes")
    imported = import_contract_products(root / "15B", indexed, "15B")
    for output in imported["outputs"]:
        source = Path(output)
        target = root / source.relative_to(indexed)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return node


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def state(workspace, run_id="current", payload="15B", **extra):
    value = {"id": run_id, "payload": payload, "name": f"Task {run_id}", "jobs": [], **extra}
    write(workspace / "runs" / run_id / "state.json", value)
    return value


def tb_point(root, payload="15B", stem="0C-285-001", channel=1):
    return write(root / payload / "tb/L1/json" / f"{stem}_fit_results.l1.json", [
        {"channel": channel, "center": 250., "center_err": .5},
    ])


def tb_model():
    return [{"channel": channel, "G0": .01, "k": .018, "V0": 24., "b": 54., "c": 23459.}
            for channel in range(4)]


def test_selected_result_revalidates_only_its_files_not_all_tasks(tmp_path, monkeypatch):
    current = state(tmp_path)
    point = tb_point(tmp_path / "runs/current/products")
    catalog = LayerProductCatalog(tmp_path)
    selected = catalog.candidates(current, "TB_L1")[0]
    def unexpected_scan(*args):
        raise AssertionError("Selected input should not enumerate every task again")
    monkeypatch.setattr(catalog, "_enumerate", unexpected_scan)
    assert catalog.get(current, "TB_L1", selected["id"])["id"] == selected["id"]
    point.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="已变化"):
        catalog.get(current, "TB_L1", selected["id"])


@pytest.mark.parametrize("family,layer,suffix", [("tb", "TB_L1", "condition_fit_results.l1.json"),
                                                ("ec", "EC_L2", "40keV_fit_results.l2.json")])
def test_partial_native_adoption_retains_peer_spectra_when_selected_by_another_task(tmp_path, family, layer, suffix):
    from calibration_lib.common.contract_import import read_saved_spectrum
    from calibration_lib.workbench.input_routing import LayerInputRouting

    current = state(tmp_path)
    state(tmp_path, "source")
    consumer = state(tmp_path, "consumer")
    current_root = tmp_path / "runs/current/products"
    source_root = tmp_path / "runs/source/products"
    old_node = native_peak_fixture(current_root, family, 100.)
    native_peak_fixture(source_root, family, 200.)
    catalog = LayerProductCatalog(tmp_path)
    selected = next(row for row in catalog.candidates(current, layer) if row.get("run_id") == "source")
    frozen = tmp_path / "runs/current/inputs/ch1/products"
    scope = {"stems": None, "channels": [1]}
    materialize_candidate(selected, frozen, "15B", scope)
    native_before = {path.name: path.read_bytes() for path in old_node.iterdir() if path.is_file()}
    source_before = {path: path.read_bytes() for path in source_root.rglob("*") if path.is_file()}
    router = LayerInputRouting()
    router.runs = tmp_path / "runs"
    router._copy_input_layer({**selected, "frozen_root": str(frozen), "scope": scope}, current_root, "15B", replace=True)
    assert {path.name: path.read_bytes() for path in old_node.iterdir() if path.is_file()} == native_before
    assert {path: path.read_bytes() for path in source_root.rglob("*") if path.is_file()} == source_before
    directory = "tb/L1/json" if family == "tb" else "ec/L2/json"
    mixed_path = current_root / "15B" / directory / suffix
    mixed = json.loads(mixed_path.read_text())
    assert {row["channel"]: row["center"] for row in mixed} == {0: 100., 1: 201.}
    # Select this mixed result from a third task. Re-importing the old native
    # node must not overwrite the adopted CH1 compatibility selection.
    catalog.invalidate()
    candidate = next(row for row in catalog.candidates(consumer, layer) if row.get("run_id") == "current")
    target = tmp_path / "runs/consumer/inputs/mixed/products"
    materialize_candidate(candidate, target, "15B")
    records = json.loads((target / "15B" / directory / suffix).read_text())
    assert {row["channel"]: row["center"] for row in records} == {0: 100., 1: 201.}
    zero, one = (next(row for row in records if row["channel"] == ch) for ch in (0, 1))
    assert str(target / "_inputs" / candidate["id"]) in zero["contract_node"]
    assert one["contract_node"] == next(row for row in mixed if row["channel"] == 1)["contract_node"]
    for row in records:
        saved = read_saved_spectrum(Path(row["contract_node"]), row["channel"], family)
        assert saved["fit"]["center"] == row["center"]
    # The private JSON snapshot continues to describe CH0 after the producer
    # rewrites its own analysis metadata and saved spectral bins.
    write(old_node / "points.json", {"0": {"center": 999.}})
    import pandas as pd

    frame = pd.read_parquet(old_node / "spectra.parquet")
    frame["density"] = 999.
    frame.to_parquet(old_node / "spectra.parquet", index=False)
    assert json.loads((Path(zero["contract_node"]) / "points.json").read_text())["0"]["center"] == 100.
    assert set(pd.read_parquet(Path(zero["contract_node"]) / "spectra.parquet")["density"]) == {100.}


def test_mixed_compatibility_index_does_not_revive_removed_native_channels(tmp_path):
    source = tmp_path / "runs/source/products"
    native_peak_fixture(source, "tb", 100., channels=(0, 1, 2))
    compatible = source / "15B/tb/L1/json/condition_fit_results.l1.json"
    records = json.loads(compatible.read_text())
    write(compatible, [row for row in records if row["channel"] == 0])
    current = state(tmp_path)
    state(tmp_path, "source")
    candidate = next(row for row in LayerProductCatalog(tmp_path).candidates(current, "TB_L1")
                     if row.get("run_id") == "source")
    target = tmp_path / "runs/current/inputs/subset/products"
    materialize_candidate(candidate, target, "15B")
    actual = json.loads((target / "15B/tb/L1/json/condition_fit_results.l1.json").read_text())
    assert [row["channel"] for row in actual] == [0]


def test_lists_shared_current_and_other_tasks_only_for_same_physical_payload(tmp_path):
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared/15B"
    current = state(workspace, dataset={"source_payload": "15B", "product_dir": str(shared)})
    state(workspace, "other")
    state(workspace, "wrong", payload="14B")
    tb_point(shared.parent)
    tb_point(workspace / "runs/current/products", stem="current")
    tb_point(workspace / "runs/other/products", stem="other")
    tb_point(workspace / "runs/wrong/products", payload="14B", stem="wrong")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    rows = LayerProductCatalog(workspace).candidates(current, "TB_L1")
    assert {row["origin"] for row in rows} == {"shared", "current", "task"}
    assert {row["run_id"] for row in rows} == {None, "current", "other"}
    assert all(row["payload"] == "15B" for row in rows)
    assert all(row["channels"] == [1] for row in rows)
    assert all(row["files"][0]["relative"].startswith("15B/tb/L1/") for row in rows)
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*.json")}


def test_mutable_result_identity_changes_without_reading_parquet_arrays(tmp_path, monkeypatch):
    current = state(tmp_path)
    path = tmp_path / "runs/current/products/15B/tb/L0/parquet/test.l0.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a real parquet; listing must not read it")
    catalog = LayerProductCatalog(tmp_path)
    original_read = type(path).read_bytes

    def no_parquet_read(self):
        if self.suffix == ".parquet":
            raise AssertionError("Product listing must not read event arrays")
        return original_read(self)

    monkeypatch.setattr(type(path), "read_bytes", no_parquet_read)
    first = catalog.candidates(current, "TB_L0")[0]
    assert catalog.get(current, "TB_L0", first["id"])["id"] == first["id"]
    timestamp = path.stat().st_mtime_ns
    os.utime(path, ns=(timestamp + 1000000, timestamp + 1000000))
    catalog.invalidate()
    second = catalog.candidates(current, "TB_L0")[0]
    assert first["id"] != second["id"]
    with pytest.raises(ValueError, match="重新选择"):
        catalog.get(current, "TB_L0", first["id"])


def test_history_only_includes_successful_jobs_and_preserves_versions(tmp_path):
    jobs = [{"id": name, "layer": "TB_L1", "status": status, "finished_at": f"2026-11-03T0{i}:00:00Z"}
            for i, (name, status) in enumerate([
                ("first", "succeeded"), ("second", "succeeded"), ("bad", "failed"),
                ("running", "running"), ("queued", "queued"),
            ])]
    current = state(tmp_path, jobs=jobs)
    for job in jobs:
        tb_point(tmp_path / "runs/current/jobs" / job["id"] / "products", stem=job["id"])
    rows = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")
    assert {row["job_id"] for row in rows} == {"first", "second"}
    assert len({row["id"] for row in rows}) == 2
    assert rows[0]["created_at"] == "2026-11-03T01:00:00Z"


def test_manifest_or_figures_alone_are_not_candidates(tmp_path):
    current = state(tmp_path)
    for layer in ("tb/L0", "tb/L1", "tb/L2", "ec/L0", "ec/L1", "ec/L2", "ec/L3"):
        root = tmp_path / "runs/current/products/15B" / layer
        write(root / "json/manifest.json", {"success": True, "layer": layer})
        write(root / "figures/example.png", {})
    catalog = LayerProductCatalog(tmp_path)
    for layer in ("TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3"):
        assert catalog.candidates(current, layer) == []


def test_native_ec_peak_and_model_paths_do_not_become_corr_inputs(tmp_path):
    current = state(tmp_path)
    root = tmp_path / "runs/current/products/15B/ec"
    write(root / "L1/Co60/points.json", {"0": {"center": 6700., "energy_keV": 1173.228}})
    write(root / "L1/Co60/meta.json", {"schema": "g15b-ec-l1@1", "measurement": "Co60"})
    (root / "L1/Co60/spectra.parquet").write_bytes(b"bins")
    write(root / "L2/2611031200/2611031200_ec_coef_sci_ch0.json", {"channel": 0, "EC_low": {"A": 1}})
    write(root / "L2/2611031200/2611031200_ec_fit.meta.json", {"schema": "g15b-ec-l2@1"})
    catalog = LayerProductCatalog(tmp_path)
    assert catalog.candidates(current, "EC_L1") == []
    peaks = catalog.candidates(current, "EC_L2")
    assert len(peaks) == 1
    assert peaks[0]["channels"] == [0]
    assert peaks[0]["analysis_contract"] == "cali_proc@0.5"
    assert peaks[0]["layout"] == "native"
    assert all("/ec/L1/Co60/" in row["relative"] for row in peaks[0]["files"])
    models = catalog.candidates(current, "EC_L3")
    assert len(models) == 1
    assert models[0]["version"] == "2611031200"
    assert models[0]["channels"] == [0]


def test_native_tb_model_dates_are_independent_and_compat_duplicates_hidden(tmp_path):
    current = state(tmp_path)
    root = tmp_path / "runs/current/products/15B/tb/L2"
    for stamp in ("2611021200", "2611031200"):
        write(root / stamp / f"{stamp}_temp_bias_fit.json", tb_model())
        write(root / stamp / f"{stamp}_temp_bias_fit.meta.json", {"schema": "g15b-tb-l2@1"})
        write(root / "json" / f"{stamp}_15B_TB.l2.json", tb_model())
    rows = LayerProductCatalog(tmp_path).candidates(current, "TB_L2")
    assert len(rows) == 2
    assert {row["version"] for row in rows} == {"2611021200", "2611031200"}
    assert all(row["channels"] == [0, 1, 2, 3] for row in rows)


def test_compatibility_ec_fit_and_corrected_events_are_separate_layers(tmp_path):
    current = state(tmp_path)
    root = tmp_path / "runs/current/products/15B/ec"
    event = root / "L1/parquet/40keV_ch2_corr.l1.parquet"
    event.parent.mkdir(parents=True)
    event.write_bytes(b"corrected events")
    write(root / "L2/json/40keV_fit_results.l2.json", [{"channel": 2, "center": 220., "E": 40.}])
    write(root / "L3/json/261103_15B_EC.l3.json", {"calibration_params": {"2": {"EC_low": {}}}})
    catalog = LayerProductCatalog(tmp_path)
    corr = catalog.candidates(current, "EC_L1")[0]
    assert corr["channels"] == [2]
    assert corr["observations"] == ["40keV_ch2"]
    assert len(catalog.candidates(current, "EC_L2")) == 1
    assert len(catalog.candidates(current, "EC_L3")) == 1


def test_profile_is_original_snapshot_and_current_input_state_does_not_change(tmp_path):
    profile = tmp_path / "runs/current/acquisition/profile.json"
    write(profile, {"analysis_contract": "cali_proc@0.5", "analysis_config": {"tb_l1": {"payload": "15B"}}})
    current = state(tmp_path, acquisition={"path": str(profile), "revision": 4})
    tb_point(tmp_path / "runs/current/products")
    before = copy.deepcopy(current)
    candidate = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")[0]
    assert candidate["acquisition"] == current["acquisition"]
    assert candidate["profile"]["analysis_contract"] == "cali_proc@0.5"
    assert current == before


def test_renamed_task_and_arbitrary_shared_payload_root_keep_physical_identity(tmp_path):
    shared = tmp_path / "shared/custom-products"
    current = state(tmp_path, dataset={"source_payload": "15B", "product_dir": str(shared)})
    state(tmp_path, "renamed", payload="15B-doublet", dataset={"source_payload": "15B"})
    tb_point(shared.parent, payload="custom-products")
    tb_point(tmp_path / "runs/renamed/products", payload="15B-doublet")
    rows = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")
    assert {row["source_payload"] for row in rows} == {"15B"}
    assert {row["storage_payload"] for row in rows} == {"custom-products", "15B-doublet"}


def test_invalid_layer_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown layer"):
        LayerProductCatalog(tmp_path).candidates({"id": "current", "payload": "15B"}, "EC_L4")


def test_raw_only_dataset_does_not_inherit_default_products(tmp_path):
    current = state(tmp_path, dataset={"raw_dir": "/shared/raw/15B", "source_payload": "15B"})
    tb_point(tmp_path / "products")
    assert LayerProductCatalog(tmp_path).candidates(current, "TB_L1") == []


def test_materializes_native_l0_with_exact_clock_amp_and_hk_for_analysis(tmp_path):
    import numpy as np
    import pandas as pd
    from calibration_lib.common.native_time import apply_conditions, event_span, read_analysis_events
    from calibration_lib.common.utils import read_parquet_metadata, write_parquet

    shared = tmp_path / "shared/15B"
    node = shared / "tb/L0/0C-285-001"
    events = pd.DataFrame({"channel": [0, 1, 2, 3] * 3, "amp": np.arange(12.) - 2.,
                           "timestamp1": np.repeat(np.array([0, 100000000, 200000000], dtype=np.uint64), 4),
                           "source_idx": np.arange(12), "crc_check": True})
    telemetry = pd.DataFrame({**{f"bias_ch{ch}": [28.5, 28.5] for ch in range(4)},
                              **{f"sipm_temp_ch{ch}": [24., 26.] for ch in range(4)}})
    write_parquet(node / "events.parquet", events)
    write_parquet(node / "hk.parquet", telemetry)
    write(node / "meta.json", {"schema": "g15b-l0@1", "payload": "15B",
                                "time_axis": {"events": "timestamp1", "frequency_hz": 100000000.}})
    current = state(tmp_path, dataset={"product_dir": str(shared), "source_payload": "15B"})
    source_bytes = {path: path.read_bytes() for path in shared.rglob("*") if path.is_file()}
    candidate = LayerProductCatalog(tmp_path).candidates(current, "TB_L0")[0]
    target = tmp_path / "private/job/products"
    materialize_candidate(candidate, target, "LOCAL-15B")
    output = target / "LOCAL-15B/tb/L0/parquet/0C-285-001.l0.parquet"
    result = read_analysis_events(output)
    pd.testing.assert_frame_equal(result, events[["channel", "amp", "crc_check", "timestamp1", "source_idx"]])
    assert result["timestamp1"].dtype == np.dtype("uint64")
    assert event_span(result, channel=0)["span_s"] == 2.
    native = read_parquet_metadata(output, "native")
    selected, conditions = apply_conditions(result, native, 28.5)
    assert conditions["channels"]["0"]["temp"] == 25.
    assert len(selected) == len(events)
    assert read_parquet_metadata(output, "acquisition")["input_product_id"] == candidate["id"]
    assert {path: path.read_bytes() for path in shared.rglob("*") if path.is_file()} == source_bytes


def test_native_model_materialization_uses_only_selected_timestamp(tmp_path):
    current = state(tmp_path)
    source = tmp_path / "runs/current/products/15B/tb/L2"
    for stamp in ("2611021200", "2611031200"):
        write(source / stamp / f"{stamp}_temp_bias_fit.json", tb_model())
        write(source / stamp / f"{stamp}_temp_bias_fit.meta.json", {"schema": "g15b-tb-l2@1"})
    candidate = next(row for row in LayerProductCatalog(tmp_path).candidates(current, "TB_L2")
                     if row["version"] == "2611021200")
    target = tmp_path / "private/products"
    result = materialize_candidate(candidate, target, "15B")
    assert result["candidate_id"] == candidate["id"]
    assert (target / "15B/tb/L2/json/2611021200_15B_TB.l2.json").is_file()
    assert not list(target.rglob("*2611031200*"))


def test_materialization_copies_json_and_keeps_source_unchanged(tmp_path):
    current = state(tmp_path)
    source = tb_point(tmp_path / "runs/current/products")
    original = source.read_bytes()
    candidate = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")[0]
    target = tmp_path / "private/products"
    materialize_candidate(candidate, target, "15B")
    output = target / candidate["files"][0]["relative"]
    assert output.read_bytes() == original
    output.write_text("[]", encoding="utf-8")
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="独立任务"):
        materialize_candidate(candidate, candidate["product_root"], "15B")


def test_materialization_rejects_escaping_paths(tmp_path):
    candidate = {"product_root": str(tmp_path / "shared"), "source_payload": "15B", "id": "example",
                 "files": [{"relative": "15B/../../bad.json", "path": "/shared/bad.json"}]}
    with pytest.raises(ValueError, match="Invalid input product path"):
        materialize_candidate(candidate, tmp_path / "private/products", "15B")


def test_shared_product_does_not_claim_current_task_producing_profile(tmp_path):
    profile = tmp_path / "runs/current/acquisition/profile.json"
    write(profile, {"analysis_contract": "cali_proc@0.5", "revision": "new-current-config"})
    shared = tmp_path / "shared/15B"
    current = state(tmp_path, acquisition={"path": str(profile), "revision": 4},
                    dataset={"product_dir": str(shared), "source_payload": "15B"})
    tb_point(shared.parent)
    candidate = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")[0]
    assert candidate["acquisition"] is None
    assert "profile" not in candidate


def test_current_result_references_immutable_job_files_and_producing_config(tmp_path):
    producer = {"id": "first-fit", "layer": "TB_L1", "status": "succeeded",
                "acquisition": {"path": str(tmp_path / "v1/profile.json"), "sha256": "v1"}}
    write(tmp_path / "v1/profile.json", {"analysis_contract": "cali_proc@0.5", "version_name": "v1"})
    path = tb_point(tmp_path / "runs/current/products")
    snapshot = tb_point(tmp_path / "runs/current/jobs/first-fit/products")
    relative = path.relative_to(tmp_path / "runs/current/products").as_posix()
    current = state(tmp_path, jobs=[producer], acquisition={"path": "/new/profile.json", "sha256": "v2"},
                    generated={"TB_L1": [{"path": relative, "job_id": "first-fit"}]})
    rows = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")
    candidate = next(row for row in rows if "job_id" not in row)
    assert candidate["files"][0]["path"] == str(snapshot)
    assert candidate["acquisition"]["sha256"] == "v1"
    target = tmp_path / "private/products"
    materialize_candidate(candidate, target, "15B")
    original = snapshot.read_bytes()
    path.write_text("[]", encoding="utf-8")
    assert (target / relative).read_bytes() == original


def test_short_listing_cache_is_invalidated_and_get_always_revalidates(tmp_path):
    current = state(tmp_path)
    path = tb_point(tmp_path / "runs/current/products")
    catalog = LayerProductCatalog(tmp_path)
    first = catalog.candidates(current, "TB_L1")[0]
    write(path, [{"channel": 0, "center": 1000., "center_err": 2.}])
    assert catalog.candidates(current, "TB_L1")[0]["id"] == first["id"]
    with pytest.raises(ValueError, match="重新选择"):
        catalog.get(current, "TB_L1", first["id"])
    current_id = catalog.candidates(current, "TB_L1")[0]["id"]
    assert current_id != first["id"]
    path.unlink()
    catalog.invalidate()
    assert catalog.candidates(current, "TB_L1") == []


def correction_params():
    return {str(ch): {key: value for key, value in row.items() if key != "channel"}
            for ch, row in enumerate(tb_model())}


def corr_file(path, metadata):
    import pandas as pd
    from calibration_lib.common.utils import write_parquet

    write_parquet(path, pd.DataFrame({"channel": [0, 0], "amp_corr": [100., 110.]}),
                  metadata=metadata, meta_key="tb_correction")
    return path


def test_corrected_tb_params_uses_footers_and_reports_actual_model(tmp_path, monkeypatch):
    import pandas as pd

    source = tmp_path / "15B/ec/L1/parquet"
    metadata = {"tb_params_used": correction_params(), "tb_model_sha256": "original-model",
                "tb_corrfile": "original.json", "ref_temp_C": 25., "ref_bias_V": 28.5,
                "mode": "role_mean_constant"}
    corr_file(source / "src_Co60_40m_corr.l1.parquet", metadata)
    corr_file(source / "src_bkg_40m_corr.l1.parquet", metadata)

    def no_array_reads(*args, **kwargs):
        raise AssertionError("TB provenance must only read Parquet footer metadata")

    monkeypatch.setattr(pd, "read_parquet", no_array_reads)
    result = corrected_tb_params(tmp_path)
    assert result["channels"] == [0, 1, 2, 3]
    assert result["params"] == [correction_params()[str(ch)] for ch in range(4)]
    assert result["source"]["tb_model_sha256"] == "original-model"
    assert result["source"]["ref_temp_C"] == 25.
    assert len(result["source"]["files"]) == 2
    assert result["complete"]


def test_conflicting_tb_coefficients_or_reference_conditions_are_identified(tmp_path):
    source = tmp_path / "15B/ec/L1/parquet"
    metadata = {"tb_params_used": correction_params(), "ref_temp_C": 25.}
    first = corr_file(source / "source_corr.l1.parquet", metadata)
    changed = copy.deepcopy(metadata)
    changed["tb_params_used"]["1"]["k"] = .022
    second = corr_file(source / "background_corr.l1.parquet", changed)
    with pytest.raises(ValueError, match="CH1"):
        corrected_tb_params(tmp_path)
    assert corrected_tb_params(tmp_path, scope={"channels": [0]})["channels"] == [0]
    changed = copy.deepcopy(metadata)
    changed["ref_temp_C"] = 20.
    corr_file(second, changed)
    with pytest.raises(ValueError, match="ref_temp_C"):
        corrected_tb_params({"files": [{"path": str(first)}, {"path": str(second)}]})


def test_native_peak_channel_evidence_preserves_physical_channel_pairing(tmp_path):
    primary = {"channel_models": correction_params(), "ref_bias_V": 28.5}
    altered_unused = copy.deepcopy(primary)
    altered_unused["channel_models"]["0"]["k"] = .023
    node = tmp_path / "15B/ec/L1/Co60"
    write(node / "meta.json", {"channel_evidence": {
        "0": {"corr": primary, "background_corr": {"0": primary}},
        "1": {"corr": altered_unused, "background_corr": {"1": altered_unused}},
    }})
    write(node / "points.json", {"0": {"center": 6700.}, "1": {"center": 7400.}})
    result = corrected_tb_params(tmp_path)
    assert result["channels"] == [0, 1]
    assert result["params"][0]["k"] == .018
    assert result["params"][1]["k"] == .018
    broken = copy.deepcopy(primary)
    broken["channel_models"]["1"]["k"] = .024
    write(node / "meta.json", {"channel_evidence": {"1": {
        "corr": primary, "background_corr": {"1": broken},
    }}})
    with pytest.raises(ValueError, match="CH1"):
        corrected_tb_params(tmp_path)


def test_tb_evidence_is_scoped_and_does_not_silently_accept_unknown_tables(tmp_path):
    source = tmp_path / "15B/ec/L1/parquet"
    first = corr_file(source / "selected_corr.l1.parquet", {"tb_params_used": correction_params()})
    second = corr_file(source / "unknown_corr.l1.parquet", {})
    all_files = corrected_tb_params(tmp_path)
    assert not all_files["complete"]
    assert all_files["missing_files"] == [str(second)]
    selected = corrected_tb_params(tmp_path, scope={"stems": ["selected"], "channels": [2]})
    assert selected["complete"]
    assert selected["channels"] == [2]
    assert selected["source"]["files"] == [str(first)]
    assert corrected_tb_params(second) is None


def test_ec_candidate_reports_producing_job_tb_and_shared_does_not_inherit_it(tmp_path):
    shared = tmp_path / "shared/15B"
    producer = {"id": "corr-job", "status": "succeeded", "layer": "EC_L1",
                "tb_version": {"sha256": "producer-tb", "path": "/old/TB.json"}}
    path = corr_file(tmp_path / "runs/current/products/15B/ec/L1/parquet/source_corr.l1.parquet",
                     {"tb_params_used": correction_params()})
    corr_file(tmp_path / "runs/current/jobs/corr-job/products/15B/ec/L1/parquet/source_corr.l1.parquet",
              {"tb_params_used": correction_params()})
    corr_file(shared / "ec/L1/parquet/source_corr.l1.parquet", {"tb_params_used": correction_params()})
    current = state(tmp_path, jobs=[producer], ec_dependency={"sha256": "new-selected-tb"},
                    dataset={"product_dir": str(shared), "source_payload": "15B"},
                    generated={"EC_L1": [{"path": path.relative_to(tmp_path / "runs/current/products").as_posix(),
                                            "job_id": "corr-job"}]})
    rows = LayerProductCatalog(tmp_path).candidates(current, "EC_L1")
    assert all(row["tb_version"]["sha256"] == "producer-tb" for row in rows if row["origin"] == "current")
    assert all("tb_version" not in row for row in rows if row["origin"] == "shared")


def test_native_l0_actual_source_names_map_to_profile_compatible_stems(tmp_path):
    import numpy as np
    import pandas as pd
    from calibration_lib.common.cali_contract import build_acquisition_profile
    from calibration_lib.common.utils import read_parquet_metadata, write_parquet

    profile = build_acquisition_profile("15B", tmp_path / "unmounted-raw", verify_files=False)
    profile_path = write(tmp_path / "private/profile.json", profile)
    shared = tmp_path / "shared/15B"
    targets = []
    for kind in ("tb", "source", "xray"):
        row = next(row for row in profile["discovery"]["manifest"]
                   if row["kind"] == kind and (kind != "source" or row["source"] == "Co60"))
        family = "tb" if kind == "tb" else "ec"
        basename = row["event"].rsplit("/", 1)[-1]
        node = shared / family / "L0" / basename
        write_parquet(node / "events.parquet", pd.DataFrame({"channel": [0, 1], "amp": [100., 110.],
                                                             "timestamp1": np.array([0, 100000000], dtype=np.uint64)}))
        write_parquet(node / "hk.parquet", pd.DataFrame({**{f"bias_ch{ch}": [28.5] for ch in range(4)},
                                                         **{f"sipm_temp_ch{ch}": [25.] for ch in range(4)}}))
        write(node / "meta.json", {"schema": "g15b-l0@1", "inputs": {"sci": row["event"]}})
        if kind == "xray":
            expected = f"{float(row['energy_keV']):g}keV_ch{row['channel']}"
        else:
            expected = row["stem"]
        targets.append((family, basename, expected, row))
    current = state(tmp_path, acquisition={"path": str(profile_path)},
                    dataset={"product_dir": str(shared), "source_payload": "15B"})
    catalog = LayerProductCatalog(tmp_path)
    for family, basename, expected, row in targets:
        layer = "TB_L0" if family == "tb" else "EC_L0"
        candidate = next(value for value in catalog.candidates(current, layer) if value["origin"] == "shared")
        assert expected in candidate["observations"]
        assert candidate["observation_aliases"][basename] == expected
        target = tmp_path / "private/job" / family / "products"
        materialize_candidate(candidate, target, "15B", profile=profile)
        compatible = target / "15B" / family / "L0/parquet" / f"{expected}.l0.parquet"
        assert compatible.is_file()
        observation = read_parquet_metadata(compatible, "acquisition")["observation"]
        assert observation["stem"] == expected
        assert observation["event"] == row["event"]
        if row["kind"] == "source":
            assert observation["source"] == "Co60"
        assert not (compatible.parent / f"{basename}.l0.parquet").exists()


def test_scoped_native_l0_reads_only_selected_source_and_required_background(tmp_path, monkeypatch):
    import numpy as np
    import pandas as pd
    from calibration_lib.common import native_time
    from calibration_lib.common.cali_contract import build_acquisition_profile
    from calibration_lib.common.utils import write_parquet

    profile = build_acquisition_profile("15B", tmp_path / "raw", verify_files=False)
    shared = tmp_path / "shared/15B"
    rows = [row for row in profile["discovery"]["manifest"]
            if row["kind"] == "background" or row.get("source") in ("Co60", "Cs137")]
    for row in rows:
        node = shared / "ec/L0" / row["event"].rsplit("/", 1)[-1]
        write_parquet(node / "events.parquet", pd.DataFrame({"channel": [0, 1], "amp": [100., 110.],
                                                             "timestamp1": np.array([0, 100000000], dtype=np.uint64)}))
        write_parquet(node / "hk.parquet", pd.DataFrame({**{f"bias_ch{ch}": [28.5] for ch in range(4)},
                                                         **{f"sipm_temp_ch{ch}": [25.] for ch in range(4)}}))
        write(node / "meta.json", {"schema": "g15b-l0@1"})
    current = state(tmp_path, dataset={"product_dir": str(shared), "source_payload": "15B"})
    candidate = LayerProductCatalog(tmp_path).candidates(current, "EC_L0")[0]
    source = next(row for row in rows if row.get("source") == "Co60")
    background = next(row for row in rows if row["kind"] == "background")
    original = native_time.read_analysis_events
    read_nodes = []

    def track_reads(path):
        read_nodes.append(path.parent.name)
        return original(path)

    monkeypatch.setattr(native_time, "read_analysis_events", track_reads)
    target = tmp_path / "private/products"
    result = materialize_candidate(candidate, target, "15B", scope={"stems": [source["stem"]]}, profile=profile)
    assert set(read_nodes) == {source["event"].rsplit("/", 1)[-1], background["event"].rsplit("/", 1)[-1]}
    assert result["materialized_stems"] == sorted([source["stem"], background["stem"]])
    assert len(list((target / "15B/ec/L0/parquet").glob("*.parquet"))) == 2
    assert (target / "15B/ec/L0" / next(row for row in rows if row.get("source") == "Cs137")["event"].rsplit("/", 1)[-1] / "events.parquet").exists()


@pytest.mark.parametrize("layer", ["TB_L2", "EC_L3"])
def test_aggregate_job_catalog_excludes_seed_models_and_does_not_relabel_baseline(tmp_path, layer):
    profile_path = write(tmp_path / "native-profile.json", {"analysis_contract": "cali_proc@0.5"})
    reference = {"path": str(profile_path), "sha256": "native-profile"}
    job = {"id": "new-fit", "status": "succeeded", "layer": layer,
           "acquisition": reference, "finished_at": "2026-11-03T23:00:00Z"}
    root = tmp_path / "runs/current/jobs/new-fit/products/15B"
    current_root = tmp_path / "runs/current/products/15B"
    if layer == "TB_L2":
        seeded = root / "tb/L2/json/old_15B_TB.l2.json"
        baseline = current_root / "tb/L2/json/old_15B_TB.l2.json"
        native = root / "tb/L2/2611032300/2611032300_temp_bias_fit.json"
        write(seeded, tb_model())
        write(baseline, tb_model())
        write(native, tb_model())
        write(native.with_suffix(".meta.json"), {"schema": "g15b-tb-l2@1"})
    else:
        seeded = root / "ec/L3/json/old_15B_EC.l3.json"
        baseline = current_root / "ec/L3/json/old_15B_EC.l3.json"
        native = root / "ec/L2/2611032300/2611032300_ec_coef_sci_ch1.json"
        write(seeded, {"calibration_params": {"1": {"EC_low": {"A": 1.}}}})
        write(baseline, {"calibration_params": {"1": {"EC_low": {"A": 1.}}}})
        write(native, {"channel": 1, "EC_low": {"A": 2.}})
        write(native.parent / "2611032300_ec_fit.meta.json", {"schema": "g15b-ec-l2@1"})
    job["outputs"] = [{"path": native.relative_to(root.parent).as_posix()}]
    current = state(tmp_path, jobs=[job], acquisition=reference)
    rows = LayerProductCatalog(tmp_path).candidates(current, layer)
    job_rows = [row for row in rows if row.get("job_id") == "new-fit"]
    assert len(job_rows) == 1
    assert job_rows[0]["layout"] == "native"
    assert all("old_15B" not in record["relative"] for record in job_rows[0]["files"])
    assert job_rows[0]["analysis_contract"] == "cali_proc@0.5"
    baseline_row = next(row for row in rows if "job_id" not in row)
    assert baseline_row["analysis_contract"] is None
    assert baseline_row["acquisition"] is None


def test_aggregate_job_meta_only_output_does_not_claim_seeded_model(tmp_path):
    root = tmp_path / "runs/current/jobs/only-figure/products"
    model = write(root / "15B/tb/L2/old/old_temp_bias_fit.json", tb_model())
    meta = write(model.with_suffix(".meta.json"), {"schema": "g15b-tb-l2@1"})
    current = state(tmp_path, jobs=[{"id": "only-figure", "layer": "TB_L2", "status": "succeeded",
                                    "outputs": [{"path": meta.relative_to(root).as_posix()}]}])
    assert LayerProductCatalog(tmp_path).candidates(current, "TB_L2") == []


def test_mixed_ec_peak_observations_are_exact_consumed_groups_not_exposure_paths(tmp_path):
    current = state(tmp_path)
    root = tmp_path / "runs/current/products/15B/ec"
    write(root / "L2/json/src_Co60_fit_results.l2.json", [
        {"channel": 1, "center": 6700., "E": 1173.228,
         "signal_file": "/server/15B/ec/L1/parquet/src_Co60_40m_corr.l1.parquet", "native_analysis_id": "Co60_1173"},
        {"channel": 1, "center": 7700., "E": 1332.492,
         "signal_file": "src_Co60_40m_corr.l1.parquet", "native_analysis_id": "Co60_1332"},
    ])
    write(root / "L2/json/40keV_fit_results.l2.json", [
        {"channel": 0, "center": 220., "E": 40., "signal_file": "/server/40keV_ch0_300s_corr.l1.parquet"},
    ])
    for name, energy in (("Co60_1173", 1173.228), ("Co60_1332", 1332.492)):
        write(root / "L1" / name / "points.json", {"1": {"center": 6700., "energy_keV": energy,
                                                            "source_name": "Co60", "signal_file": "src_Co60_40m_corr.l1.parquet"}})
        write(root / "L1" / name / "meta.json", {"kind": "source", "measurement": "src_Co60_40m"})
    candidate = LayerProductCatalog(tmp_path).candidates(current, "EC_L2")[0]
    assert candidate["observations"] == ["40keV", "src_Co60"]


def test_native_only_joint_source_lines_share_one_consumed_group(tmp_path):
    current = state(tmp_path)
    root = tmp_path / "runs/current/products/15B/ec/L1"
    for name, energy in (("Co60_1173", 1173.228), ("Co60_1332", 1332.492)):
        write(root / name / "points.json", {"0": {"center": 6700., "energy_keV": energy,
                                                   "source_name": "Co60", "signal_file": "/remote/src_Co60_40m_corr.l1.parquet"}})
        write(root / name / "meta.json", {"schema": "g15b-ec-l1@1", "kind": "source", "measurement": "Co60_40m"})
    write(root / "X_40" / "points.json", {"0": {"center": 220., "energy_keV": 40.,
                                                 "signal_file": "/remote/40keV_ch0_300s_corr.l1.parquet"}})
    write(root / "X_40" / "meta.json", {"kind": "xray", "measurement": "40"})
    candidate = LayerProductCatalog(tmp_path).candidates(current, "EC_L2")[0]
    assert candidate["observations"] == ["40keV", "src_Co60"]
    target = tmp_path / "private/products"
    materialize_candidate(candidate, target, "15B")
    assert {path.name.removesuffix("_fit_results.l2.json") for path in (target / "15B/ec/L2/json").glob("*_fit_results.l2.json")} == set(candidate["observations"])


def test_tb_peak_scope_uses_fit_stem_or_native_analysis_id_without_signal_paths(tmp_path):
    current = state(tmp_path)
    root = tmp_path / "runs/current/products/15B/tb/L1"
    write(root / "json/0C_285_fit_results.l1.json", [{"channel": 1, "center": 220.,
                                                    "signal_file": "/remote/raw/0C_285_039.event.dat",
                                                    "native_analysis_id": "legacy-alias"}])
    write(root / "other-node/points.json", {"1": {"center": 220., "signal_file": "/remote/old-event.dat"}})
    write(root / "other-node/meta.json", {"measurement": "/remote/raw/old-event.dat"})
    catalog = LayerProductCatalog(tmp_path)
    assert catalog.candidates(current, "TB_L1")[0]["observations"] == ["0C_285"]
    (root / "json/0C_285_fit_results.l1.json").unlink()
    catalog.invalidate()
    assert catalog.candidates(current, "TB_L1")[0]["observations"] == ["other-node"]


def test_materialized_native_tb_alias_uses_consumer_stem_and_preserves_native_spectrum(tmp_path):
    shared = tmp_path / "shared/14B"
    node = shared / "tb/L1/0C_285"
    write(node / "points.json", {str(ch): {"center": 200. + ch, "sigma": 10., "amplitude": 40.}
                                  for ch in range(4)})
    write(node / "meta.json", {"schema": "g14b-tb-l1@1", "measurement": "0C_285"})
    (node / "spectra.parquet").write_bytes(b"original saved spectrum")
    current = state(tmp_path, payload="14B", dataset={"product_dir": str(shared), "source_payload": "14B"})
    profile = {"discovery": {"manifest": [{"kind": "tb", "analysis_id": "0C_285", "stem": "0C-285-137"}]}}
    before = {path: path.read_bytes() for path in shared.rglob("*") if path.is_file()}
    candidate = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")[0]
    output = tmp_path / "private/products"
    materialized = materialize_candidate(candidate, output, "14B", profile=profile)
    canonical = output / "14B/tb/L1/json/0C-285-137_fit_results.l1.json"
    assert canonical.is_file()
    assert not (canonical.parent / "0C_285_fit_results.l1.json").exists()
    records = json.loads(canonical.read_text())
    assert {row["channel"] for row in records} == {0, 1, 2, 3}
    for record in records:
        assert record["native_analysis_id"] == "0C_285"
        assert Path(record["contract_node"]).name == "0C_285"
        assert Path(record["contract_spectrum"]).read_bytes() == b"original saved spectrum"
    assert materialized["observation_aliases"] == {"0C_285": "0C-285-137"}
    index = json.loads((output / "14B/contract_import.json").read_text())
    assert str(canonical) in index["outputs"]
    assert all(Path(path).is_file() for path in materialized["files"])
    assert before == {path: path.read_bytes() for path in shared.rglob("*") if path.is_file()}


def test_tb_alias_merge_preserves_canonical_channels_and_fills_only_missing_channels(tmp_path):
    root = tmp_path / "products"
    directory = root / "14B/tb/L1/json"
    profile = {"discovery": {"manifest": [{"kind": "tb", "analysis_id": "0C_285", "stem": "0C-285-137"}]}}
    alias = directory / "0C_285_fit_results.l1.json"
    canonical = directory / "0C-285-137_fit_results.l1.json"
    write(alias, [{"channel": ch, "center": 200. + ch} for ch in range(4)])
    write(canonical, [{"channel": 1, "center": 999., "reason": "current local fit"}])
    assert normalize_tb_aliases(root, "14B", profile) == {"0C_285": "0C-285-137"}
    values = json.loads(canonical.read_text())
    assert [row["channel"] for row in values] == [0, 1, 2, 3]
    assert values[1]["center"] == 999.
    assert values[0]["center"] == 200.
    original = canonical.read_bytes()
    write(alias, [{"channel": ch, "center": -100.} for ch in range(4)])
    normalize_tb_aliases(root, "14B", profile)
    assert canonical.read_bytes() == original
    assert not alias.exists()


def test_tb_aliases_uses_known_producer_profile_before_consumer_fallback(tmp_path):
    shared = tmp_path / "shared/14B"
    tb_point(shared.parent, payload="14B", stem="0C_285")
    current = state(tmp_path, payload="14B", dataset={"product_dir": str(shared), "source_payload": "14B"})
    candidate = LayerProductCatalog(tmp_path).candidates(current, "TB_L1")[0]
    producer = {"discovery": {"manifest": [{"kind": "tb", "analysis_id": "0C_285", "stem": "0C-285-222"}]}}
    consumer = {"discovery": {"manifest": [{"kind": "tb", "analysis_id": "0C_285", "stem": "0C-285-137"}]}}
    candidate["profile"] = producer
    output = tmp_path / "private/products"
    result = materialize_candidate(candidate, output, "14B", profile=consumer)
    assert result["observation_aliases"] == {"0C_285": "0C-285-222"}
    assert (output / "14B/tb/L1/json/0C-285-222_fit_results.l1.json").is_file()
    assert not (output / "14B/tb/L1/json/0C-285-137_fit_results.l1.json").exists()
    assert tb_aliases(consumer) == {"0C_285": "0C-285-137"}
    assert normalize_tb_aliases(tmp_path / "no-files", "14B", consumer) == tb_aliases(consumer)
