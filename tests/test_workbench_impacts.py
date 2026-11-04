"""Changes propagate from scientific consumers, with physical scope intact."""

import copy

import pytest
from calibration_lib.common.cali_contract import build_acquisition_profile, sync_source_lines
from calibration_lib.workbench.acquisition_store import prepare_profile
from calibration_lib.workbench.impacts import (
    configuration_impact,
    downstream,
    impact_for_layer,
    merge_impacts,
    scientific_fingerprint,
)


@pytest.fixture
def profile(tmp_path):
    return prepare_profile(tmp_path, build_acquisition_profile("15B", tmp_path, verify_files=False))


def change(profile, section, edit):
    result = copy.deepcopy(profile)
    edit(result["analysis_config"][section])
    return result


def test_fixed_tb_disconnects_the_ec_branch():
    assert downstream("TB_L1", pinned_tb=True) == ["TB_L1", "TB_L2"]
    assert downstream("TB_L2") == ["TB_L2", "EC_L1", "EC_L2", "EC_L3"]
    assert downstream("EC_L0", pinned_tb=True) == ["EC_L0", "EC_L1", "EC_L2", "EC_L3"]


def test_tb_window_change_only_repeats_one_physical_channel_and_its_aggregate(profile):
    before = copy.deepcopy(profile)
    result = change(profile, "tb_l1", lambda cfg: cfg["measurements"]["0C_265"]["fit_range"][1].__setitem__(0, 55.))
    plan = configuration_impact(profile, result, {"tb_binding": "pinned"})
    assert plan["roots"] == ["TB_L1"]
    assert plan["layers"] == ["TB_L1", "TB_L2"]
    assert plan["scopes"]["TB_L1"] == {"stems": ["0C_265"], "channels": [1]}
    assert plan["scopes"]["TB_L2"] == {"stems": None, "channels": [1]}
    assert profile == before


def test_following_tb_updates_all_ec_files_only_in_the_changed_channel(profile):
    result = change(profile, "tb_l1", lambda cfg: cfg["measurements"]["0C_265"]["fit_range"][2].__setitem__(0, 60.))
    plan = configuration_impact(profile, result, {"tb_binding": "follow_run"})
    assert plan["layers"] == ["TB_L1", "TB_L2", "EC_L1", "EC_L2", "EC_L3"]
    assert plan["scopes"]["EC_L1"] == {"stems": None, "channels": [2]}


def test_curve_weight_change_keeps_tb_peak_fits(profile):
    result = change(profile, "tb_l2", lambda cfg: cfg["model"].__setitem__("weights", "uniform"))
    assert configuration_impact(profile, result, {"tb_binding": "pinned"})["layers"] == ["TB_L2"]


def test_final_ec_split_change_keeps_decode_correction_and_peak_fits(profile):
    result = change(profile, "ec_l2", lambda cfg: cfg["fit"].__setitem__("split_high_keV", 60.))
    plan = configuration_impact(profile, result)
    assert plan["layers"] == ["EC_L3"]
    assert plan["changes"][0]["roots"] == ["EC_L3"]


def test_source_window_change_does_not_recorrect_or_decode(profile):
    result = change(profile, "ec_l1", lambda cfg: cfg["source"]["Co60"]["fit_range"][0].__setitem__(0, 5800.))
    plan = configuration_impact(profile, result)
    assert plan["layers"] == ["EC_L2", "EC_L3"]
    assert plan["scopes"]["EC_L2"]["channels"] == [0]
    assert plan["scopes"]["EC_L2"]["stems"] == ["src_Co60_Co60_40m_081"]


def test_joint_doublet_updates_both_lines_and_covariance_as_one_group(profile):
    before = copy.deepcopy(profile)
    entry = before["analysis_config"]["ec_l1"]["source"]["Co60"]
    entry.update(fit_mode="doublet", background="lin", fit_range=[[5800, 8500]] * 4,
                 peaks=[{"energy_keV": 1173.228, "center_anchor": [6700] * 4},
                        {"energy_keV": 1332.492, "center_anchor": [7700] * 4}])
    entry.pop("energy_keV")
    after = copy.deepcopy(before)
    after["analysis_config"]["ec_l1"]["source"]["Co60"]["peaks"][0]["center_anchor"][1] = 6750
    plan = configuration_impact(before, after)
    assert plan["layers"] == ["EC_L2", "EC_L3"]
    change = plan["changes"][0]
    assert change["joint_fit"]
    assert change["energies_keV"] == [1173.228, 1332.492]
    assert change["channels"] == [1]


def test_ec_bin_width_change_keeps_corrected_event_input(profile):
    result = change(profile, "ec_l1", lambda cfg: cfg["shared"].__setitem__("bin_width", 8.))
    plan = configuration_impact(profile, result)
    assert plan["layers"] == ["EC_L2", "EC_L3"]
    assert scientific_fingerprint(profile, "EC_L1") == scientific_fingerprint(result, "EC_L1")


@pytest.mark.parametrize("field,value", [("ref", {"temp_C": 22., "bias_V": 28.5}),
                                         ("clock", {"frequency_hz": 99_000_000.})])
def test_ec_reference_or_exposure_settings_start_at_correction(profile, field, value):
    result = change(profile, "ec_l1", lambda cfg: cfg["shared"].__setitem__(field, value))
    assert configuration_impact(profile, result)["layers"] == ["EC_L1", "EC_L2", "EC_L3"]


def test_xml_field_conversion_changes_both_decoded_branches(profile):
    result = copy.deepcopy(profile)
    result["parser"]["transform"]["base_divisor"] = 5.
    plan = configuration_impact(profile, result, {"tb_binding": "pinned"})
    assert plan["layers"] == ["TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3"]
    assert plan["roots"] == ["TB_L0", "EC_L0"]


def test_xml_source_path_whitespace_attribute_order_and_provenance_do_not_trigger_fits(profile):
    result = copy.deepcopy(profile)
    for role in ("event", "hk"):
        result["xml"][role]["snapshot_path"] = "/another/snapshot.xml"
        result["xml"][role]["source_path"] = "/another/original.xml"
        result["xml"][role]["text"] = result["xml"][role]["text"].replace("><", ">\n<")
    result["analysis_config"]["ec_l1"]["provenance"] = "new documentation"
    result["analysis_config"]["tb_l2"]["model"]["initial_defaults_source"] = "a new comment"
    result["source_mapping"]["config_sha256"] = {"unrelated": "changed"}
    plan = configuration_impact(profile, result)
    assert plan["layers"] == []
    assert plan["metadata_only"]
    assert scientific_fingerprint(profile, "TB_L0") == scientific_fingerprint(result, "TB_L0")


def test_native_source_energy_sync_never_invalidates_raw_unpacking(profile):
    result = change(profile, "ec_l1", lambda cfg: cfg["source"]["Co60"].__setitem__("energy_keV", 1173.228))
    sync_source_lines(result)
    plan = configuration_impact(profile, result)
    assert plan["layers"] == ["EC_L2", "EC_L3"]
    assert scientific_fingerprint(profile, "EC_L0") == scientific_fingerprint(result, "EC_L0")


def test_manifest_pair_change_affects_only_that_branch_and_shared_background_consumers(profile):
    result = copy.deepcopy(profile)
    row = next(row for row in result["discovery"]["manifest"] if row["kind"] == "background")
    old_event = row["event"]
    row["hk"] = "replacement.hk"
    plan = configuration_impact(profile, result, {"tb_binding": "pinned"})
    assert plan["roots"] == ["EC_L0", "EC_L2"]
    assert plan["layers"] == ["EC_L0", "EC_L1", "EC_L2", "EC_L3"]
    assert len(plan["scopes"]["EC_L2"]["stems"]) >= 4
    assert old_event.endswith("Bkg_30m_084.event.dat")


def test_shared_background_selection_fans_out_to_all_three_sources(profile):
    result = change(profile, "ec_l1", lambda cfg: cfg["source"]["Co60"]["background_selection"].__setitem__("hk_bias", 28.3))
    plan = configuration_impact(profile, result)
    assert plan["layers"] == ["EC_L1", "EC_L2", "EC_L3"]
    stems = plan["scopes"]["EC_L1"]["stems"]
    assert any("Na22" in stem for stem in stems)
    assert any("Cs137" in stem for stem in stems)
    assert any("Co60" in stem for stem in stems)
    assert any("bkg" in stem for stem in stems)


def test_changed_file_rule_can_reuse_actual_unchanged_file_pairs():
    before = {"discovery": {"mode": "rules", "rules": [{"kind": "tb", "event_regex": "old"}]}}
    after = {"discovery": {"mode": "rules", "rules": [{"kind": "tb", "event_regex": "new"}]}}
    rows = [{"kind": "tb", "stem": "one", "event": "/raw/a.dat", "hk": "/raw/a.hk"}]
    assert configuration_impact(before, after, old_inventory=rows, new_inventory={"rows": rows})["layers"] == []
    assert configuration_impact(before, after)["roots"] == ["TB_L0"]


def test_actual_changed_filename_conditions_keep_decode_but_update_analysis():
    before = {"analysis_contract": "cali_proc@0.5", "discovery": {"mode": "rules", "rules": []}}
    rows_before = [{"kind": "tb", "stem": "one", "event": "/raw/a.dat", "hk": "/raw/a.hk", "temp": 20., "bias": 28.5}]
    rows_after = [{**rows_before[0], "temp": 25.}]
    plan = configuration_impact(before, before, {"tb_binding": "pinned"},
                                old_inventory=rows_before, new_inventory=rows_after)
    assert plan["layers"] == ["TB_L1", "TB_L2"]
    assert plan["scopes"]["TB_L1"]["stems"] == ["one"]


def test_absolute_discovery_paths_still_find_relative_background_dependencies(profile):
    rows_before = copy.deepcopy(profile["discovery"]["manifest"])
    for row in rows_before:
        row["event"] = "/shared/raw/15B/" + row["event"]
        row["hk"] = "/shared/raw/15B/" + row["hk"]
    rows_after = copy.deepcopy(rows_before)
    next(row for row in rows_after if row["kind"] == "background")["hk"] = "/replacement.hk"
    plan = configuration_impact(profile, profile, old_inventory=rows_before, new_inventory=rows_after)
    stems = plan["scopes"]["EC_L2"]["stems"]
    assert any("Na22" in stem for stem in stems)
    assert any("Cs137" in stem for stem in stems)
    assert any("Co60" in stem for stem in stems)


def test_stage_fingerprint_keeps_l0_when_downstream_models_change(profile):
    result = change(profile, "ec_l2", lambda cfg: cfg["fit"].__setitem__("split_high_keV", 70.))
    for layer in ("TB_L0", "EC_L0", "EC_L1"):
        assert scientific_fingerprint(profile, layer) == scientific_fingerprint(result, layer)
    assert scientific_fingerprint(profile, "EC_L3") != scientific_fingerprint(result, "EC_L3")


def test_merging_scopes_retains_whole_aggregate_and_union_of_changed_channels():
    state = {"tb_binding": "pinned"}
    plan = merge_impacts(impact_for_layer("TB_L1", stems=["one"], channels=[0], state=state),
                         impact_for_layer("TB_L1", stems=["two"], channels=[2], state=state))
    assert plan["scopes"]["TB_L1"] == {"stems": ["one", "two"], "channels": [0, 2]}
    assert plan["scopes"]["TB_L2"] == {"stems": None, "channels": [0, 2]}
    assert plan["roots"] == ["TB_L1"]
