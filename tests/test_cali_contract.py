import base64
import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.cali_contract import (
    CONTRACT,
    build_acquisition_profile,
    canonical_point,
    config_bundle,
    publish_l1,
    publish_l2,
    tb_windows,
    validate_l1,
)
from calibration_lib.common.contract_import import import_contract_products, read_saved_spectrum
from calibration_lib.common.utils import write_parquet
from calibration_lib.tb.processors.L1_processor import TBL1Processor

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6"
    "WQAAAABJRU5ErkJggg=="
)


def spectrum(channel, value=10.):
    return pd.DataFrame({"channel": [channel] * 2, "x": [2., 6.],
                         "bin_left": [0., 4.], "bin_right": [4., 8.],
                         "counts": [value, value], "err": [3., 3.],
                         "density": [value / 4, value / 4], "density_err": [.75, .75]})


def record(channel, center=200.):
    return {"channel": channel, "center": center, "center_err": .3, "sigma": 20., "sigma_err": .2,
            "window_lo": 120., "window_hi": 280., "amplitude": 40000., "amplitude_err": 200.,
            "height_std": 5., "resolution": 10., "resolution_unit": "percent", "redchi": 1.1,
            "temp": 25., "bias": 28.5}


def test_explicit_source_mapping_uses_confirmed_table_not_output_label(tmp_path):
    profile = build_acquisition_profile("LOCAL-14B", tmp_path, source_payload="15B", verify_files=False)
    assert profile["analysis_contract"] == CONTRACT
    assert profile["source_mapping"]["source_payload"] == "15B"
    assert profile["source_mapping"]["output_payload"] == "LOCAL-14B"
    assert profile["analysis_config"]["tb_l1"]["payload"] == "15B"
    assert len(profile["discovery"]["manifest"]) == 103
    assert len(tb_windows(profile)) == 55
    source = next(row for row in profile["discovery"]["manifest"] if row["kind"] == "source")
    assert source["stem"].startswith("src_Na22_")
    assert source["exposure_minutes"] is None
    assert profile["fitting"]["tb_histogram"]["bin_width"] == 4.
    # A new root is not accepted merely because its filenames look familiar.
    with pytest.raises(FileNotFoundError, match="Confirmed raw/HK"):
        build_acquisition_profile("14B", tmp_path)


def test_bundle_is_independent_and_keeps_both_low_high_model_configs():
    first, second = config_bundle("14B"), config_bundle("14B")
    first["configs"]["ec_l1"]["source"].clear()
    assert len(second["configs"]["ec_l1"]["source"]) == 3
    assert second["configs"]["ec_l2"]["fit"]["split_low_keV"] == 51.
    assert second["configs"]["ec_l2"]["fit"]["split_high_keV"] == 55.
    assert set(second["sha256"]) == {"tb_l1.yaml", "tb_l2.yaml", "ec_l1.yaml", "ec_l2.yaml", "exp_files.csv"}


def test_point_unit_conversion_does_not_confuse_area_and_peak_height():
    converted = canonical_point(record(0), "ec")
    assert converted["center_std"] == .3
    assert converted["window"] == [120., 280.]
    assert converted["resolution"] == .1
    assert converted["amplitude"] == pytest.approx(40000 / (20 * np.sqrt(2 * np.pi)))
    assert converted["amplitude_std"] == 5.
    assert converted["area"] == 40000.
    assert converted["amplitude_kind"] == "height"


def test_partial_analysis_replaces_selected_physical_channel_only(tmp_path):
    args = (tmp_path, "14B", "tb", "observation")
    first = publish_l1(*args, [record(0), record(1)], pd.concat([spectrum(0), spectrum(1)]),
                       {"l0_source": "original", "history": []}, PNG, {"selection.png": PNG})
    publish_l1(*args, [record(1, 220.)], spectrum(1, 40.), {"l0_source": "original", "history": []})
    points = json.loads((tmp_path / "14B/tb/L1/observation/points.json").read_text())
    assert points["0"]["center"] == 200.
    assert points["1"]["center"] == 220.
    data = pd.read_parquet(first["spectra"])
    assert data.loc[data.channel == 0, "counts"].tolist() == [10., 10.]
    assert data.loc[data.channel == 1, "counts"].tolist() == [40., 40.]
    assert validate_l1(first["node"])["valid"]
    meta = json.loads((tmp_path / "14B/tb/L1/observation/meta.json").read_text())
    assert meta["schema"] == "g14b-tb-l1@1"
    assert len(meta["revisions"]) == 1


def test_timestamped_model_hash_and_existing_version_are_preserved(tmp_path):
    models = [dict(channel=0, G0=.01, k=.02, V0=24., b=50., c=20000.)]
    result = publish_l2(tmp_path, "14B", "tb", models, {"inputs": []}, timestamp="2611021234")
    model = tmp_path / "14B/tb/L2/2611021234/2611021234_temp_bias_fit.json"
    evidence = json.loads((model.with_suffix(".meta.json")).read_text())
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    assert evidence["model_sha256"] == digest
    with pytest.raises(FileExistsError):
        publish_l2(tmp_path, "14B", "tb", models, {}, timestamp="2611021234")
    assert hashlib.sha256(model.read_bytes()).hexdigest() == digest
    assert result["model_paths"] == [str(model)]


def test_native_tb_fit_is_density_with_area_in_counts(tmp_path):
    generator = np.random.default_rng(615)
    pulses = generator.normal(200., 20., 40000)
    processor = TBL1Processor("14B", tmp_path, analysis_contract=CONTRACT)
    fit = processor._fit_channel("sample", 0, pulses, {"sample": {"0": {"lo": 120., "hi": 280.}}},
                                 False, histogram_config={"min": 0., "max": 400., "bin_width": 4.})
    assert fit.ordinate == "Counts/ADC"
    assert np.sum(fit.hist) * 4 == len(pulses)
    assert fit.center == pytest.approx(200., abs=.4)
    assert fit.sigma == pytest.approx(20., abs=.5)
    assert fit.amplitude == pytest.approx(40000., rel=.02)
    assert fit.height == pytest.approx(40000 / (np.sqrt(2 * np.pi) * 20), rel=.03)
    assert fit.height_std > 0
    assert fit.center_err > 0


def test_contract_import_keeps_shared_spectra_in_place_and_converts_units(tmp_path):
    source, private = tmp_path / "shared", tmp_path / "run/products"
    publish_l1(source, "15B", "tb", "sample", [record(0)], spectrum(0),
               {"l0_source": "events", "history": []}, PNG, {"selection.png": PNG})
    publish_l1(source, "15B", "ec", "Cs137_661", [{**record(1), "E": 661.657}], spectrum(1),
               {"kind": "source", "measurement": "Cs137", "history": []}, PNG,
               {"selection_primary.png": PNG})
    shared_table = source / "15B/ec/L1/Cs137_661/spectra.parquet"
    digest = hashlib.sha256(shared_table.read_bytes()).hexdigest()
    index = import_contract_products(source, private, "LOCAL", source_payload="15B")
    assert index["parquet_files_copied"] == 0
    assert not list(private.rglob("*.parquet"))
    assert hashlib.sha256(shared_table.read_bytes()).hexdigest() == digest
    rows = json.loads((private / "LOCAL/ec/L2/json/src_Cs137_fit_results.l2.json").read_text())
    assert rows[0]["native_analysis_id"] == "Cs137_661"
    assert rows[0]["center_err"] == .3
    assert rows[0]["resolution"] == 10.
    assert rows[0]["amplitude"] == pytest.approx(40000.)
    assert rows[0]["contract_spectrum"] == str(shared_table)
    saved = read_saved_spectrum(shared_table.parent, 1, "ec")
    assert saved["y"].tolist() == [2.5, 2.5]
    assert saved["histogram_config"]["bin_width"] == 4.


def test_partial_publish_retains_other_channels_model_bindings(tmp_path):
    common = (tmp_path, "14B", "ec", "source_511")
    publish_l1(*common, [record(0)], spectrum(0),
               {"corr": {"0": {"tb_model": "old", "tb_hash": "a"}}, "status": "partial"})
    result = publish_l1(*common, [record(1)], spectrum(1),
                        {"corr": {"1": {"tb_model": "new", "tb_hash": "b"}}})
    meta = json.loads(open(result["meta"], encoding="utf-8").read())
    assert meta["corr"]["0"]["tb_model"] == "old"
    assert meta["corr"]["1"]["tb_model"] == "new"
    assert meta["channel_evidence"]["0"]["corr"]["0"]["tb_hash"] == "a"
    assert meta["revisions"][-1]["channels"] == [1]
    package = publish_l2(tmp_path, "14B", "ec", [{"channel": 0, "EC_low": [0., .1, 0.], "EC_high": None}], {})
    assert json.loads(open(package["meta"], encoding="utf-8").read())["status"] == "partial"


@pytest.mark.parametrize("target,temperature,samples", [(28.5, 25., 2), (27.5, 5., 1)])
def test_tb_l1_uses_selected_native_hk_and_publishes_complete_analysis(tmp_path, target, temperature, samples):
    root = tmp_path / "products"
    l0 = root / "14B/tb/L0"
    canonical_l0 = l0 / "sample"
    generator = np.random.default_rng(23)
    n = 6000
    events = pd.DataFrame({"amp": generator.normal(200, 20, 4 * n),
                           "channel": np.repeat(np.arange(4), n), "crc_check": True,
                           "timestamp1": np.arange(4 * n, dtype=np.int64) * 100_000})
    hk = pd.DataFrame({"source_idx": [0, 1, 2, 3],
                       **{f"bias_ch{ch}": [27.5, 28.5, 28.5, 27.8] for ch in range(4)},
                       **{f"sipm_temp_ch{ch}": [5., 24., 26., 80.] for ch in range(4)}})
    write_parquet(canonical_l0 / "hk.parquet", hk)
    native = {"analysis_contract": CONTRACT, "l0_node": str(canonical_l0),
              "time_axis": {"events": "timestamp1", "frequency_hz": 1e8, "shared": False}}
    old_hk = {"temp": [99.] * 4, "bias": [27.] * 4, "temp_err": [0.] * 4, "bias_err": [0.] * 4}
    write_parquet(l0 / "parquet/sample.l0.parquet", events, metadata=old_hk, meta_key="hk_data",
                  schema_metadata={"native": native, "acquisition": {"analysis_contract": CONTRACT,
                                   "observation": {"bias": 28.5}}})
    windows = tmp_path / "windows.json"
    windows.write_text(json.dumps({"sample": {"0": {"lo": 120., "hi": 280.}}}))
    result = TBL1Processor("14B", root).process(l0, {"fit_channels": [0], "auto_window": False,
                                               "payload_contract_config": {"tb_l1": {"shared": {"conditions": {"tolerance_V": .1}},
                                                   "measurements": {"sample": {"target_bias_V": target}}}},
                                               "fit_windows_config": str(windows)})
    assert not result["errors"]
    package = result["outputs"][0]["canonical"]
    assert validate_l1(package["node"])["valid"]
    points = json.loads(open(package["points"], encoding="utf-8").read())
    assert points["0"]["temperature_C"] == temperature
    assert points["0"]["bias_V"] == target
    assert points["0"]["hk_samples"] == samples
    assert points["0"]["selection"]["selected_source_idx"] == ([1, 2] if target == 28.5 else [0])
    assert points["0"]["exposure_s"] == pytest.approx((4 * n - 1) / 1000.)


def test_unconfirmed_channel_keeps_null_parameters_during_publication():
    from calibration_lib.common.cali_contract import canonical_point
    point = canonical_point({"channel": 3, "status": "unconfirmed", "reason": "no_confirmed_window",
                             "window": None, "center": None, "sigma": None}, "tb")
    assert point["status"] == "unconfirmed"
    assert point["center"] is None and point["sigma"] is None and point["window"] is None
    assert point["reason"] == "no_confirmed_window"
