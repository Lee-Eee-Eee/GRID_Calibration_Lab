"""Physical-channel, exposure and statistical regression for the new EC contract."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.cali_contract import validate_l1
from calibration_lib.common.utils import DataLayout, read_json, write_parquet
from calibration_lib.ec.processors.L1_processor import ECL1Processor
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor


def frame(amplitudes, seconds, channel=0):
    amplitudes = np.asarray(amplitudes, dtype=float)
    return pd.DataFrame({"channel": channel, "amp_corr": amplitudes, "amp": amplitudes,
                         "utc": 0., "timestamp1": (np.asarray(seconds) * 100_000_000).astype(np.uint64),
                         "crc_check": True, "temp": 25., "bias": 28.5})


def processor(tmp_path):
    return ECL2Processor("14B", tmp_path, analysis_contract="cali_proc@0.5")


def test_native_spectrum_uses_independent_exposures_and_bin_density(tmp_path):
    peak = frame([2, 2, 2, 2, 6], [0, 1, 2, 3, 4])
    background = frame([2, 6], [0, 2])
    result = processor(tmp_path)._prepare_spectrum(peak, background, 4)
    assert result["primary_exposure_s"] == 4
    assert result["background_exposure_s"] == 2
    assert result["spec"][0] == pytest.approx(4 / 4 / 4)
    assert result["bkg_spec"][0] == pytest.approx(1 / 2 / 4)
    assert result["net"][0] == pytest.approx(.125)
    expected_error = np.hypot((1 + np.sqrt(4.75)) / 16, (1 + np.sqrt(1.75)) / 8)
    assert result["net_err"][0] == pytest.approx(expected_error)
    assert result["primary_rate"][0] == pytest.approx(1)
    assert result["ordinate"] == "counts/(s*ADC)"


def test_native_spectrum_keeps_configured_adc_range_and_rare_tail(tmp_path):
    proc = ECL2Processor("14B", tmp_path, analysis_contract="cali_proc@0.5",
                         analysis_config={"shared": {"adc_max": 16000}})
    signal = frame([2.] * 2000 + [15002.], np.linspace(0, 5, 2001))
    result = proc._prepare_spectrum(signal, frame([2., 2.], [0., 2.]), 4)
    assert result["bin_right"][-1] == 16000.
    assert np.sum(result["spec"]) * 5 * 4 == pytest.approx(2001)
    assert result["spec"][3750] == pytest.approx(1 / 5 / 4)


@pytest.mark.parametrize("seconds, message", [([0, 1, .5], "回退"), ([0, 1, 15], "间隙"), ([0, 0, 0], "非正")])
def test_native_exposure_does_not_sort_reset_or_invent_seconds(tmp_path, seconds, message):
    with pytest.raises(ValueError, match=message):
        processor(tmp_path)._prepare_spectrum(frame([2, 2, 2], seconds), frame([2, 2], [0, 1]), 4)


def test_native_exposure_requires_timestamp_even_when_utc_exists(tmp_path):
    missing = frame([2, 2], [0, 1]).drop(columns="timestamp1")
    with pytest.raises(ValueError, match="timestamp1"):
        processor(tmp_path)._prepare_spectrum(missing, frame([2, 2], [0, 1]), 4)


def test_background_rotation_selects_same_physical_channel(tmp_path):
    channel_map = {}
    for slot in range(4):
        physical = pd.concat([frame([100 * slot + ch] * 3, [0, 1, 2], ch) for ch in range(4)])
        channel_map[slot] = {"df": physical[physical.channel == slot], "full_df": physical,
                             "parquet": f"40keV_ch{slot}.parquet"}
    selected_slot, background = processor(tmp_path)._choose_background_channel(0, channel_map)
    assert selected_slot == 1
    assert set(background["df"].channel) == {0}
    assert background["df"].amp_corr.tolist() == [100, 100, 100]
    assert background["parquet"] == "40keV_ch1.parquet"


def test_missing_rotated_file_is_not_replaced_by_spectral_similarity(tmp_path):
    channel_map = {ch: {"df": frame([100, 100], [0, 1], ch)} for ch in (0, 2, 3)}
    with pytest.raises(ValueError, match="slot CH1"):
        processor(tmp_path)._choose_background_channel(0, channel_map)


def test_native_correction_retains_finite_negative_amplitudes(tmp_path):
    proc = ECL1Processor("14B", tmp_path, analysis_contract="cali_proc@0.5")
    params = {ch: {"G0": .01, "k": .02, "V0": 24., "b": 50., "c": 20000.} for ch in range(4)}
    result = proc._apply_correction(frame([-1, 2], [0, 1]), params)
    assert result.amp_corr.tolist() == pytest.approx([-1, 2])


def test_odr_keeps_energies_fixed_and_reports_adc_objective():
    center = np.linspace(100, 1000, 8)
    energy = .00002 * center**2 + .12 * center + 2
    energy += np.asarray([.05, -.1, .15, -.03, .07, -.05, .02, -.02])
    errors = np.linspace(.4, 1., 8)
    result = ECL3Processor._fit_energy_odr(center, energy, errors)
    np.testing.assert_array_equal(result["fixed_energy_keV"], energy)
    adjusted = np.asarray(result["adjusted_center_ADC"])
    np.testing.assert_allclose(np.polyval(result["coefficients"], adjusted), energy, rtol=1e-7)
    assert result["redchi"] == pytest.approx(np.sum(((adjusted - center) / errors)**2) / 5)
    assert result["scale_covar"] is False
    assert np.isfinite(result["covariance"]).all()


def test_resolution_model_uses_fraction_and_a_e_squared_order():
    energy = np.asarray([20., 30., 40., 60., 100., 200., 500., 1000.])
    true = np.asarray([.001, .2, 4.])
    resolution = np.sqrt(np.polyval(true, energy)) / energy
    result = ECL3Processor._fit_resolution_direct(energy, resolution, np.full(8, .002))
    np.testing.assert_allclose(result["coefficients"], true, rtol=1e-5, atol=1e-8)
    assert result["scale_covar"] is False


def test_native_bands_preserve_gap_points_and_do_not_copy_one_fit(tmp_path, monkeypatch):
    proc = ECL3Processor("14B", tmp_path, analysis_contract="cali_proc@0.5")
    monkeypatch.setattr(proc, "_plot_native_model", lambda *args: None)
    energies = [15, 20, 30, 40, 51, 53, 55, 70, 100, 200]
    rows = [{"channel": 0, "E": float(e), "center": float(2 * e + 5 if e < 51 else e + 50),
             "center_err": .5, "resolution": 100 * np.sqrt(.001 * e**2 + .2 * e + 4) / e,
             "resolution_err": .2, "file": f"{e}keV_fit_results.l2"} for e in energies]
    result = {"errors": [], "calibration_params": {}, "fit_weight_fallbacks": [], "output_figures": []}
    proc._fit_channel_native(0, rows, "2611021200", {}, result)
    model = result["calibration_params"]["0"]
    assert model["EC_low"] != model["EC_high"]
    np.testing.assert_allclose(model["EC_low"], [0, .5, -2.5], atol=1e-6)
    np.testing.assert_allclose(model["EC_high"], [0, 1, -50], atol=1e-6)
    used = result["band_fits"]["0"]["low"]["used"] + result["band_fits"]["0"]["high"]["used"]
    assert all(":51" not in value and ":53" not in value for value in used)
    assert len(used) == 8


def test_native_source_fit_publishes_actual_density_and_selection(tmp_path):
    rng = np.random.default_rng(42)
    layout = DataLayout(tmp_path, "14B")
    source_name, background_name = "src_Cs137_measurementA", "src_bkg_measurementB"
    for stem, source in ((source_name, True), (background_name, False)):
        node = tmp_path / "14B" / "ec" / "L0" / stem
        hk = pd.DataFrame({"source_idx": range(3), **{
            f"{kind}_ch{ch}": [28.4, 28.5, 28.6] if kind == "bias" else [20, 21, 22]
            for ch in range(4) for kind in ("bias", "sipm_temp")}})
        write_parquet(node / "hk.parquet", hk)
        samples = rng.normal(100, 7, 1500) if source else rng.uniform(0, 200, 200)
        data = frame(samples, np.linspace(0, 2 if source else 4, len(samples)))
        correction = {"peak_energies_keV": [661.657], "source_name": "Cs137" if source else "bkg",
                      "conditions": {"selection": {"selected_source_idx": [0, 1, 2]}}}
        acquisition = {"native": {"l0_node": str(node)}}
        write_parquet(layout.get_ec_l1_parquet(f"{stem}_corr.l1.parquet"), data,
                      metadata=correction, meta_key="tb_correction", schema_metadata={"acquisition": acquisition})
    proc = processor(tmp_path)
    result = proc.process(layout.get_ec_l1_dir(), {
        "fit_channels": [0], "background_stem": background_name,
        "ec_windows": {"14B:ec:src_Cs137:0:661.657": {"lo": 65., "hi": 135.}},
    })
    assert not result["errors"]
    assert result["n_processed"] == 1
    package = result["canonical"][0]
    assert validate_l1(Path(package["node"]))["valid"]
    point = read_json(Path(package["points"]))["0"]
    assert point["center"] == pytest.approx(100, abs=1)
    assert point["primary_exposure_s"] == 2
    assert point["background_exposure_s"] == 4
    assert point["resolution_unit"] == "fraction"
    assert point["amplitude_kind"] == "height"
