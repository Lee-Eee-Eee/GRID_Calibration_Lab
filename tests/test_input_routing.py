"""Selected layer inputs reach jobs and retain independent result revisions."""

import copy
import json
import time

import pandas as pd
import pytest
from calibration_lib.common.cali_contract import build_acquisition_profile
from calibration_lib.common.utils import write_parquet
from calibration_lib.workbench.acquisition_store import load_profile, prepare_profile
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.service import Workbench


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def params():
    return {"G0": .012, "k": .0184, "V0": 24.11, "b": 54.31, "c": 23459.83,
            "G0_err": .001, "k_err": .001, "V0_err": .01, "b_err": .1, "c_err": 1., "chisquare": 1.}


def peaks(center=300.):
    return [{"channel": ch, "center": center + ch, "center_err": 1., "sigma": 10., "sigma_err": 1.,
             "amplitude": 500., "amplitude_err": 1., "fwhm": 23.55, "resolution": .08, "resolution_err": .01,
             "temp": 20., "temp_err": .1, "bias": 28.5, "bias_err": .01,
             "window_lo": 250., "window_hi": 350., "fit_mode": "gaus", "rsquared": .99}
            for ch in range(4)]


@pytest.fixture
def service(tmp_path):
    put(tmp_path / "products/14B/tb/L2/json/2601010000_14B_TB.l2.json", [params()] * 4)
    put(tmp_path / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json", peaks())
    result = Workbench(tmp_path)
    yield result
    result.close()


def candidate(service, run_id, layer, origin="shared", *, source_run=None):
    rows = service.layer_inputs(run_id, layer)["results"]
    return next(row for row in rows if row["origin"] == origin
                and (source_run is None or row["run_id"] == source_run))


def settled(service, run_id, job):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        latest = service.job(run_id, job["id"])
        if latest["status"] not in ("queued", "running"):
            return latest
        time.sleep(.02)
    pytest.fail("Input-routing job did not complete")


def test_adopting_existing_result_creates_no_compute_job_and_keeps_source(service):
    run = service.create("14B", "review")
    item = candidate(service, run["id"], "TB_L1")
    old = (service.workspace / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json").read_bytes()
    result = service.apply_layer_input(run["id"], {"layer": "TB_L1", "mode": "result", "candidate_id": item["id"]})
    state = service.read(run["id"])
    assert state["jobs"] == [] and result["jobs"] == []
    assert "TB_L1" not in state["dirty_layers"]
    assert "TB_L2" in state["dirty_layers"]
    assert state["adopted_results"]["TB_L1"]["id"] == item["id"]
    assert (service.workspace / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json").read_bytes() == old


def test_other_task_binding_persists_and_cuts_recompute_from_local_upstream(service):
    source = service.create("14B", "source fit")
    run = service.create("14B", "surface comparison")
    item = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": item["id"]})
    state = service.read(run["id"])
    assert state["input_bindings"]["TB_L2"]["data"]["run_id"] == source["id"]
    assert service._routed_layers(state, ["TB_L0"]) == ["TB_L0", "TB_L1"]
    service.close()
    reopened = Workbench(service.workspace)
    try:
        saved = reopened.layer_inputs(run["id"], "TB_L2")["slots"][0]["selected"]
        assert saved["id"] == item["id"]
        assert saved["run_name"] == "source fit"
    finally:
        reopened.close()


def test_bound_l1_content_reaches_processor_even_after_source_task_changes(service, monkeypatch):
    source = service.create("14B", "source fit")
    other_path = service.root(source["id"]) / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json"
    put(other_path, peaks(450.))
    run = service.create("14B", "consume chosen revision")
    item = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": item["id"]})
    put(other_path, peaks(650.))

    def execute(payload, layer, output, config, tb):
        actual = json.loads((output / "14B/tb/L1/json/20C_285_001_fit_results.l1.json").read_text())
        assert actual[0]["center"] == 450.
        model = [params()] * 4
        model[0] = {**model[0], "k": .02}
        put(output / "14B/tb/L2/json/new_14B_TB.l2.json", model)
        return {"success": True}

    monkeypatch.setattr(service.adapter, "execute", execute)
    job = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L2"}))
    assert job["status"] == "succeeded", job.get("error")
    assert (service.root(run["id"]) / "jobs" / job["id"] / "inputs.json").is_file()


def test_partial_channel_result_adoption_retains_other_channel_values(service):
    source = service.create("14B", "new peak fit")
    put(service.root(source["id"]) / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json", peaks(500.))
    run = service.create("14B", "use one channel")
    item = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    service.apply_layer_input(run["id"], {"layer": "TB_L1", "mode": "result", "candidate_id": item["id"],
                                           "scope": {"channels": [1]}})
    rows = json.loads((service.root(run["id"]) / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json").read_text())
    assert {row["channel"]: row["center"] for row in rows} == {0: 300., 1: 501., 2: 302., 3: 303.}


def test_adopting_one_channel_cannot_clear_another_channels_pending_window(service):
    run = service.create("14B", "independent channel changes")
    service.patch(run["id"], {"windows": {"20C_285_001": {"2": {"lo": 255., "hi": 345.}}}})
    item = candidate(service, run["id"], "TB_L1")
    service.apply_layer_input(run["id"], {"layer": "TB_L1", "mode": "result", "candidate_id": item["id"],
                                           "scope": {"channels": [1]}})
    state = service.read(run["id"])
    assert state["pending_windows"] == ["14B:20C_285_001:2"]
    assert "TB_L1" in state["dirty_layers"]
    with pytest.raises(ValueError, match="TB L1"):
        service.submit(run["id"], {"layer": "TB_L2"})


def test_tb_input_observation_scope_does_not_leak_tb_stems_into_ec_updates(service):
    run = service.create("14B")
    item = candidate(service, run["id"], "TB_L1")
    plan = service.preview_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": item["id"],
                                                   "scope": {"observations": item["observations"], "channels": [1]}})
    assert plan["scopes"]["TB_L2"]["stems"] is None
    assert plan["scopes"]["EC_L1"] == {"stems": None, "channels": [1]}


def prepared_run(service):
    run = service.create("14B", "native config test")
    profile = prepare_profile(service.workspace, build_acquisition_profile("14B", service.workspace / "missing", verify_files=False))
    service.save_acquisition(run["id"], profile)
    state = service.read(run["id"])
    state.update(dirty_layers=[], dirty_scopes={}, acquisition_pending_layers=[],
                 acquisition_products={"TB_L1": ["one", "two"], "TB_L2": ["aggregate"]},
                 acquisition_l1_channels={"one": [0, 1, 2, 3], "two": [0, 1, 2, 3]})
    service.save(state)
    return run["id"], profile


def test_metadata_only_preset_save_retains_coverage_and_prior_l0_signature(service):
    run_id, profile = prepared_run(service)
    before = service.read(run_id)
    profile["analysis_config"]["tb_l2"]["provenance"] = "member description"
    service.save_acquisition(run_id, profile)
    after = service.read(run_id)
    assert after["dirty_layers"] == []
    assert after["acquisition_products"] == before["acquisition_products"]
    assert after["acquisition_l1_channels"] == before["acquisition_l1_channels"]
    assert after["acquisition_pending_layers"] == []
    profile_view = load_profile(after["acquisition"])
    accepted = service._accepted_input_profiles(after, profile_view)
    assert before["acquisition"]["sha256"] in accepted["TB_L0"]


def test_cosmetic_save_keeps_previously_pending_scoped_update(service):
    run_id, profile = prepared_run(service)
    service.patch(run_id, {"windows": {"20C_285_001": {"1": {"lo": 255., "hi": 345.}}}})
    pending = service.preview_updates(run_id)
    profile["analysis_config"]["tb_l2"]["provenance"] = "member note"
    service.save_acquisition(run_id, profile)
    after = service.preview_updates(run_id)
    assert after["layers"] == pending["layers"]
    assert after["scopes"] == pending["scopes"]


def test_partial_input_scope_does_not_silently_fit_another_channel(service):
    run = service.create("14B")
    state = service.read(run["id"])
    state["input_bindings"] = {"TB_L1": {"data": {"layer": "TB_L0", "scope": {
        "stems": ["20C_285_001"], "channels": [1]}}}}
    service.save(state)
    with pytest.raises(ValueError, match="CH1"):
        service.submit(run["id"], {"layer": "TB_L1", "stem": "20C_285_001", "fit_channels": [0]})
    assert service.read(run["id"])["jobs"] == []


def test_selected_input_never_fills_missing_files_from_default_history(tmp_path):
    unrelated = tmp_path / "products/14B/tb/L0/parquet/unselected.l0.parquet"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unselected history")
    adapter = ComputeAdapter(tmp_path)
    adapter.input_layer_roots = {"TB_L0": str(tmp_path / "runs/task/inputs/frozen")}
    target = tmp_path / "runs/task/jobs/new/products"
    assert adapter._hydrate("14B", target, "TB_L0") == []
    assert not (target / "14B/tb/L0/parquet/unselected.l0.parquet").exists()


def test_partial_external_binding_only_cuts_its_covered_channel(service):
    source = service.create("14B", "fixed channel source")
    run = service.create("14B", "mixed inputs")
    item = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": item["id"], "scope": {"channels": [1]}})
    for channel, expected in ((1, False), (0, True)):
        state = service.read(run["id"])
        state.update(dirty_layers=[], dirty_scopes={}, last_impact={"layers": []})
        service.save(state)
        result = service.patch(run["id"], {"windows": {"20C_285_001": {str(channel): {"lo": 255., "hi": 345.}}}})
        assert ("TB_L2" in result["dirty_layers"]) is expected


def test_downstream_preset_change_can_still_consume_old_l0_metadata(service):
    run_id, profile = prepared_run(service)
    before = service.read(run_id)
    profile["analysis_config"]["ec_l2"]["fit"]["split_high_keV"] = 60.
    service.save_acquisition(run_id, profile)
    after = service.read(run_id)
    assert after["dirty_layers"] == ["EC_L3"]
    assert after["acquisition_l1_channels"] == before["acquisition_l1_channels"]
    output = service.root(run_id) / "jobs/check/products"
    write_parquet(output / "14B/tb/L0/parquet/one.l0.parquet", pd.DataFrame({"amp": [1.], "channel": [0]}),
                  schema_metadata={"acquisition": {"profile_sha256": before["acquisition"]["sha256"]}})
    service.adapter_for(after)._verify_acquisition_inputs("14B", output, "TB_L0", ["one"])


@pytest.mark.parametrize("fail", [False, True])
def test_apply_and_update_chain_finishes_or_stops_at_first_failure(service, monkeypatch, fail):
    run = service.create("14B", "update chain")
    item = candidate(service, run["id"], "TB_L1")
    calls = []

    def execute(payload, layer, output, config, tb):
        calls.append(layer)
        if fail:
            return {"success": False, "errors": ["surface input is incomplete"]}
        if layer == "TB_L2":
            put(output / "14B/tb/L2/json/new_14B_TB.l2.json", [params()] * 4)
        elif layer == "EC_L1":
            target = output / "14B/ec/L1/parquet/source_corr.l1.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"corrected-input")
        elif layer == "EC_L2":
            put(output / "14B/ec/L2/json/70keV_fit_results.l2.json", [{"channel": 0, "center": 300., "E": 70.}])
        else:
            put(output / "14B/ec/L3/json/final.l3.json", {"calibration_params": []})
        return {"success": True}

    monkeypatch.setattr(service.adapter, "execute", execute)
    result = service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": item["id"],
                                                     "action": "apply_and_update"})
    assert result["jobs"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = service.read(run["id"])
        if state["update_chain"]["status"] != "running":
            break
        time.sleep(.02)
    assert state["update_chain"]["status"] == ("failed" if fail else "succeeded"), state["update_chain"]
    assert calls == (["TB_L2"] if fail else ["TB_L2", "EC_L1", "EC_L2", "EC_L3"])


def test_repeated_matching_success_reuses_result_and_keeps_consumed_downstream_clean(service, monkeypatch):
    run = service.create("14B", "identical inputs")
    calls = []

    def execute(payload, layer, output, config, tb):
        calls.append(layer)
        model = [{**params(), "k": .02}] * 4
        put(output / "14B/tb/L2/json/new_14B_TB.l2.json", model)
        return {"success": True}

    monkeypatch.setattr(service.adapter, "execute", execute)
    first = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L2"}))
    assert first["status"] == "succeeded", first.get("error")
    state = service.read(run["id"])
    # Represent the EC branch having already consumed this complete TB model.
    state.update(dirty_layers=[], dirty_scopes={}, ec_dependency=copy.deepcopy(state["tb_version"]), ec_scope="all")
    service.save(state)
    second = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L2"}))
    assert second["status"] == "succeeded", second.get("error")
    assert second["reused_from"] == first["id"]
    assert second["reuse_unchanged"]
    assert calls == ["TB_L2"]
    assert service.read(run["id"])["dirty_layers"] == []


def test_changing_bound_product_bytes_requires_new_computation(service, monkeypatch):
    source = service.create("14B", "upstream source")
    path = service.root(source["id"]) / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json"
    put(path, peaks(400.))
    run = service.create("14B", "different upstream revisions")
    calls = []

    def execute(payload, layer, output, config, tb):
        input_rows = json.loads((output / "14B/tb/L1/json/20C_285_001_fit_results.l1.json").read_text())
        calls.append(input_rows[0]["center"])
        put(output / "14B/tb/L2/json/new_14B_TB.l2.json", [params()] * 4)
        return {"success": True}

    monkeypatch.setattr(service.adapter, "execute", execute)
    first_input = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": first_input["id"]})
    first = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L2"}))
    assert first["status"] == "succeeded", first.get("error")
    put(path, peaks(450.))
    service.layer_product_catalog.invalidate()
    second_input = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    assert second_input["id"] != first_input["id"]
    service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": second_input["id"]})
    second = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L2"}))
    assert second["status"] == "succeeded", second.get("error")
    assert not second.get("reused_from")
    assert calls == [400., 450.]


def test_restart_marks_running_update_chain_failed_and_allows_continuing(service, monkeypatch):
    run = service.create("14B", "recover a continuous update")

    def execute(payload, layer, output, config, tb):
        put(output / "14B/tb/L2/json/new_14B_TB.l2.json", [params()] * 4)
        return {"success": True}

    monkeypatch.setattr(service.adapter, "execute", execute)
    first = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L2"}))
    assert first["status"] == "succeeded", first.get("error")
    output_path = service.root(run["id"]) / "products/14B/tb/L2/json/new_14B_TB.l2.json"
    old_bytes = output_path.read_bytes()
    state = service.read(run["id"])
    interrupted = {**copy.deepcopy(first), "id": "interrupted", "status": "running", "outputs": [], "error": None}
    state["jobs"].append(interrupted)
    scope = {"stems": None, "channels": None}
    state.update(dirty_layers=["TB_L2"], dirty_scopes={"TB_L2": scope},
                 last_impact={"layers": ["TB_L2"], "scopes": {"TB_L2": scope}},
                 update_chain={"status": "running", "position": 1, "current_job_id": "interrupted",
                               "steps": [{"layer": "TB_L2", "scope": scope}]})
    service.save(state)
    service.close()
    reopened = Workbench(service.workspace)
    try:
        restored = reopened.read(run["id"])
        assert restored["update_chain"]["status"] == "failed"
        assert "重启" in restored["update_chain"]["error"]
        assert reopened.job(run["id"], "interrupted")["status"] == "failed"
        assert reopened.job(run["id"], first["id"])["status"] == "succeeded"
        assert output_path.read_bytes() == old_bytes
        monkeypatch.setattr(reopened.adapter, "execute", execute)
        result = reopened.start_updates(run["id"])
        assert result["jobs"]
        finished = settled(reopened, run["id"], result["jobs"][0])
        assert finished["status"] == "succeeded", finished.get("error")
        deadline = time.monotonic() + 5
        while reopened.read(run["id"])["update_chain"]["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.02)
        assert reopened.read(run["id"])["update_chain"]["status"] == "succeeded"
    finally:
        reopened.close()


def test_catalog_keeps_other_task_results_when_its_state_exceeds_four_mib(service):
    source = service.create("14B", "long calibration history")
    run = service.create("14B", "find prior results")
    state = service.read(source["id"])
    state["revisions"].append({"revision": 1, "reason": "x" * (4 * 1024 * 1024 + 1024), "config": state["config"]})
    service.save(state)
    assert (service.root(source["id"]) / "state.json").stat().st_size > 4 * 1024 * 1024
    service.layer_product_catalog.invalidate()
    item = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    assert item["run_name"] == "long calibration history"


def test_channel_only_binding_replaces_complete_job_point_set_but_keeps_other_channels(service, monkeypatch):
    source = service.create("14B", "selected CH1 point set")
    source_path = service.root(source["id"]) / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json"
    put(source_path, peaks(450.))
    run = service.create("14B", "current task has an extra observation")
    extra = service.root(run["id"]) / "products/14B/tb/L1/json/current_only_fit_results.l1.json"
    put(extra, peaks(800.))
    original = extra.read_bytes()
    item = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": item["id"],
                                           "scope": {"channels": [1]}})
    inspected = []

    def execute(payload, layer, output, config, tb):
        incoming = json.loads((output / "14B/tb/L1/json/20C_285_001_fit_results.l1.json").read_text())
        retained = json.loads((output / "14B/tb/L1/json/current_only_fit_results.l1.json").read_text())
        assert {row["channel"]: row["center"] for row in incoming} == {0: 300., 1: 451., 2: 302., 3: 303.}
        assert {row["channel"]: row["center"] for row in retained} == {0: 800., 2: 802., 3: 803.}
        ch1_points = [(path.name, row["center"])
                      for path in (output / "14B/tb/L1/json").glob("*_fit_results.l1.json")
                      for row in json.loads(path.read_text()) if row["channel"] == 1]
        assert ch1_points == [("20C_285_001_fit_results.l1.json", 451.)]
        inspected.append(True)
        put(output / "14B/tb/L2/json/scoped_14B_TB.l2.json", [params()] * 4)
        return {"success": True}

    monkeypatch.setattr(service.adapter, "execute", execute)
    job = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L2"}))
    assert job["status"] == "succeeded", job.get("error")
    assert inspected == [True]
    assert extra.read_bytes() == original


@pytest.mark.parametrize("stems", [None, ["not-listed-in-the-source"], ["0C-285-137"]])
def test_channel_wide_binding_cuts_unknown_and_aliased_observations_only_for_selected_channel(service, stems):
    source = service.create("14B", "external CH1")
    run = service.create("14B", "independent local CH0")
    item = candidate(service, run["id"], "TB_L1", "task", source_run=source["id"])
    service.apply_layer_input(run["id"], {"layer": "TB_L2", "candidate_id": item["id"],
                                           "scope": {"channels": [1]}})
    state = service.read(run["id"])
    binding = state["input_bindings"]["TB_L2"]["data"]
    assert binding["scope"] == {"stems": None, "channels": [1]}
    for channel, propagates in ((1, False), (0, True)):
        incoming = {"stems": stems, "channels": [channel]}
        assert service._binding_covers(binding, incoming) is not propagates
        routed = service._routed_layers(state, ["TB_L1"], scopes={"TB_L1": incoming})
        assert "TB_L1" in routed
        assert ("TB_L2" in routed) is propagates


def test_tb_setting_alias_normalization_preserves_canonical_windows_and_maps_point_ids(service):
    alias, canonical = "0C_285", "0C-285-137"
    alias_window = {"lo": 10., "hi": 90.}
    canonical_window = {"lo": 110., "hi": 190.}
    state = {"config": {
        "windows": {alias: {"0": alias_window, "1": alias_window},
                    canonical: {"1": canonical_window, "2": canonical_window},
                    "unrelated": {"3": alias_window}},
        "excluded": [f"14B:{alias}:1", f"14B:{canonical}:1", "14B:unrelated:2"],
        "only": [alias, canonical, "unrelated"],
        "ec_windows": {"14B:ec:40keV:1:40": canonical_window},
        "ec_excluded": ["14B:ec:40keV:1:40"],
    }, "pending_windows": [f"14B:{alias}:0", f"14B:{canonical}:0", "14B:unrelated:3"]}
    profile = {"discovery": {"manifest": [
        {"kind": "tb", "analysis_id": alias, "stem": canonical},
        {"kind": "xray", "analysis_id": "unrelated", "stem": "40keV_ch0"},
    ]}}
    original_profile = copy.deepcopy(profile)
    service._normalize_tb_settings(state, profile)
    assert alias not in state["config"]["windows"]
    assert state["config"]["windows"][canonical] == {"0": alias_window, "1": canonical_window, "2": canonical_window}
    assert state["config"]["windows"]["unrelated"] == {"3": alias_window}
    assert state["config"]["excluded"] == sorted([f"14B:{canonical}:1", "14B:unrelated:2"])
    assert state["pending_windows"] == sorted([f"14B:{canonical}:0", "14B:unrelated:3"])
    assert state["config"]["only"] == sorted([canonical, "unrelated"])
    assert state["config"]["ec_windows"] == {"14B:ec:40keV:1:40": canonical_window}
    assert state["config"]["ec_excluded"] == ["14B:ec:40keV:1:40"]
    assert profile == original_profile
    normalized = copy.deepcopy(state)
    service._normalize_tb_settings(state, profile)
    assert state == normalized
