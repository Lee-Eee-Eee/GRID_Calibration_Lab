"""Result identity follows computation, not task labels or source locations."""

import copy

import pytest
from calibration_lib.workbench.result_reuse import compute_key, find_matching_result


def key(layer="TB_L1", config=None, profile=None, inputs=None, tb=None):
    return compute_key(layer, config or {}, profile or {}, inputs or {"files": [{"relative": "one.l0.parquet", "sha256": "input-a"}]}, tb)


def test_key_is_stable_for_reordered_dictionary_and_scope_sets():
    one = {"only": ["two", "one"], "fit_channels": [2, 1], "windows": {"one": {"1": {"lo": 30, "hi": 80}}}}
    two = {"fit_channels": [1, 2, 1], "windows": {"one": {"1": {"hi": 80, "lo": 30}}}, "only": ["one", "two"]}
    assert key(config=one) == key(config=two)


def test_reading_and_display_changes_outside_layer_do_not_change_computation_key():
    before = {"analysis_config": {"ec_l2": {"fit": {"split_high_keV": 55}}}}
    after = copy.deepcopy(before)
    after["analysis_config"]["ec_l2"]["fit"]["split_high_keV"] = 60
    assert key(profile=before, config={"name": "old", "plot_scale": "log"}) == key(profile=after, config={"name": "new", "plot_scale": "linear"})
    assert key("EC_L3", profile=before) != key("EC_L3", profile=after)


def test_local_window_and_numerical_input_change_key():
    config = {"windows": {"one": {"0": {"lo": 30., "hi": 80.}}}}
    changed = copy.deepcopy(config)
    changed["windows"]["one"]["0"]["lo"] = 35.
    assert key(config=config) != key(config=changed)
    assert key(config=config) != key(config=config, inputs={"files": [{"relative": "one.l0.parquet", "sha256": "new-input"}]})


def test_unselected_file_or_channel_window_does_not_change_target_fit_key():
    config = {"only": ["one"], "fit_channels": [1], "windows": {"one": {"1": {"lo": 30., "hi": 80.}},
                                                                    "other": {"1": {"lo": 40., "hi": 90.}}}}
    changed = copy.deepcopy(config)
    changed["windows"]["other"]["1"]["lo"] = 45.
    changed["windows"]["one"]["0"] = {"lo": 1., "hi": 50.}
    assert key(config=config) == key(config=changed)


def test_same_content_and_basename_at_shared_or_personal_paths_matches():
    shared = {"id": "shared-candidate", "origin": "shared", "files": [{"path": "/shared/one.l0.parquet", "sha256": "same", "mtime_ns": 1}]}
    personal = {"id": "personal-candidate", "origin": "task", "run_id": "task-b", "files": [{"path": "/home/member/one.l0.parquet", "sha256": "same", "mtime_ns": 5}]}
    assert key(inputs=shared) == key(inputs=personal)


def test_unhashed_input_stat_change_is_not_ignored():
    one = {"files": [{"path": "/shared/one.l0.parquet", "mtime_ns": 1, "size": 100}]}
    two = {"files": [{"path": "/shared/one.l0.parquet", "mtime_ns": 2, "size": 100}]}
    assert key(inputs=one) != key(inputs=two)
    assert key(inputs={"source_path": "/raw/one.dat"}) != key(inputs={"source_path": "/raw/two.dat"})


def test_ec_model_content_changes_but_model_filename_does_not():
    one = {"path": "/a/old.json", "sha256": "model-a", "filename": "old.json"}
    two = {"path": "/b/new.json", "sha256": "model-a", "filename": "new.json"}
    assert key("EC_L1", tb=one) == key("EC_L1", tb=two)
    assert key("EC_L1", tb=one) != key("EC_L1", tb={**two, "sha256": "model-b"})
    assert key("TB_L1", tb=one) == key("TB_L1", tb={**two, "sha256": "model-b"})


def test_joint_source_fit_includes_companion_peak_window_in_same_channel():
    config = {"ec_point_id": "15B:ec:src_Co60:1:1173.228", "ec_windows": {
        "15B:ec:src_Co60:1:1173.228": {"lo": 5800, "hi": 8500},
        "15B:ec:src_Co60:1:1332.492": {"lo": 5800, "hi": 8500},
    }}
    changed = copy.deepcopy(config)
    changed["ec_windows"]["15B:ec:src_Co60:1:1332.492"]["hi"] = 8600
    assert key("EC_L2", config=config) != key("EC_L2", config=changed)


def test_aggregate_masks_and_retained_model_affect_key():
    assert key("TB_L2", config={"excluded": []}) != key("TB_L2", config={"excluded": ["14B:one:1"]})
    assert key("EC_L3", config={"ec_excluded": []}) != key("EC_L3", config={"ec_excluded": ["14B:ec:40keV:1:40"]})
    assert key("TB_L2", config={"fit_channels": [1]}, inputs={"aggregate_seed": "old"}) != key("TB_L2", config={"fit_channels": [1]}, inputs={"aggregate_seed": "other"})


def test_find_only_returns_complete_successful_key_match_and_detaches_record():
    jobs = [{"id": str(index), "layer": "TB_L1", "status": status, "computational_key": "match",
             "finished_at": f"2026-11-03T10:00:0{index}Z", "outputs": [{"path": "result.json"}]}
            for index, status in enumerate(["succeeded", "partial", "failed", "succeeded"])]
    jobs.append({"id": "legacy", "layer": "TB_L1", "status": "succeeded", "outputs": [{"path": "old.json"}]})
    state = {"jobs": jobs}
    match = find_matching_result(state, "match", "TB_L1")
    assert match["id"] == "3"
    match["outputs"].clear()
    assert jobs[3]["outputs"]
    assert find_matching_result(state, "match", "EC_L1") is None
    assert find_matching_result(state, "another", "TB_L1") is None


@pytest.mark.parametrize("layer", ["EC_corr", "TB_L9"])
def test_unknown_internal_layer_is_rejected(layer):
    with pytest.raises(ValueError, match="Unknown layer"):
        key(layer)
