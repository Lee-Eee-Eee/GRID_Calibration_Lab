"""Regression boundaries between canonical configuration and existing UI jobs."""

import json

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.cali_contract import build_acquisition_profile
from calibration_lib.common.utils import write_json, write_parquet
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from calibration_lib.workbench.acquisition_store import freeze_profile, load_profile, prepare_profile
from calibration_lib.workbench.compute import ComputeAdapter


def test_unconfirmed_saved_window_does_not_replace_confirmed_configuration(tmp_path):
    profile = build_acquisition_profile("14B", tmp_path, verify_files=False)
    adapter = ComputeAdapter(tmp_path, acquisition=profile)
    output = tmp_path / "runs" / "example" / "products"
    write_json(output / "14B/tb/L1/json/30C-265-165_fit_results.l1.json", [
        {"channel": 3, "center": None, "window_lo": None, "window_hi": None,
         "status": "unconfirmed", "reason": "no_confirmed_window"},
    ])
    windows = json.loads(adapter._windows("14B", {}, output).read_text(encoding="utf-8"))
    assert "3" not in windows["30C-265-165"]
    assert windows["30C-265-165"]["0"]["lo"] > 0


def test_four_analysis_configs_survive_preset_snapshot_and_version_read(tmp_path):
    profile = build_acquisition_profile("15B", tmp_path, verify_files=False)
    profile["analysis_config"]["ec_l2"]["fit"]["split_high_keV"] = 56.0
    reference = freeze_profile(tmp_path, profile, 1)
    assert {row["name"] for row in reference["analysis_files"]} == {
        "tb_l1.yaml", "tb_l2.yaml", "ec_l1.yaml", "ec_l2.yaml",
    }
    restored = load_profile(reference)
    assert restored["analysis_config"]["ec_l2"]["fit"]["split_high_keV"] == 56.0
    assert restored["analysis_contract"] == "cali_proc@0.5"
    assert restored["source_mapping"]["source_payload"] == "15B"


def test_analysis_editor_accepts_yaml_and_freezes_edited_value(tmp_path):
    import yaml

    profile = build_acquisition_profile("14B", tmp_path, verify_files=False)
    configs = profile["analysis_config"]
    configs["ec_l2"]["fit"]["split_high_keV"] = 56.0
    configs["tb_l1"]["shared"]["bin_width"] = 8.0
    profile["analysis_config_text"] = {key: yaml.safe_dump(value) for key, value in configs.items()}
    prepared = prepare_profile(tmp_path, profile)
    assert "analysis_config_text" not in prepared
    restored = load_profile(freeze_profile(tmp_path, prepared, 2))
    assert restored["analysis_config"]["ec_l2"]["fit"]["split_high_keV"] == 56.0
    assert restored["fitting"]["tb_histogram"]["bin_width"] == 8.0


def test_native_tb_fit_uses_configured_background(tmp_path):
    processor = TBL1Processor("14B", tmp_path, analysis_contract="cali_proc@0.5")
    processor._native_background = "lin"
    x = np.arange(100., 300., 4.)
    y = 700 * np.exp(-.5 * ((x - 200) / 20) ** 2) + 10 + .1 * x
    result, _, mode, _ = processor._select_best_fit(x, y, 200., 100., 300., 20.)
    assert mode == "lin + gaus"
    assert result.params["g_center"].value == pytest.approx(200., abs=.05)


def test_native_tb_spectrum_details_show_selected_hk_not_whole_l0_mean(tmp_path):
    profile = build_acquisition_profile("14B", tmp_path, verify_files=False)
    adapter = ComputeAdapter(tmp_path, acquisition=profile)
    products = tmp_path / "run/products"
    l0 = products / "14B/tb/L0"
    node = l0 / "0C-265-131"
    hk = pd.DataFrame({"source_idx": [0, 1, 2],
                       **{f"bias_ch{ch}": [0., 26.5, 26.5] for ch in range(4)},
                       **{f"sipm_temp_ch{ch}": [99., 24., 26.] for ch in range(4)}})
    write_parquet(node / "hk.parquet", hk)
    events = pd.DataFrame({"channel": [0, 0, 0], "amp": [90., 100., 110.], "crc_check": True})
    write_parquet(l0 / "parquet/0C-265-131.l0.parquet", events,
                  metadata={"temp": [99.] * 4, "bias": [0.] * 4}, meta_key="hk_data",
                  schema_metadata={"native": {"l0_node": str(node)},
                                   "acquisition": {"observation": {"bias": 26.5}}})
    result = adapter.spectrum("14B", products, "0C-265-131", 0)
    assert result["hk_data"]["temp"][0] == 25.
    assert result["hk_data"]["bias"][0] == 26.5
