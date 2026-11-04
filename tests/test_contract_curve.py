"""Canonical packages preserve the computed EC curve and role identities."""

import json

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.cali_contract import publish_l1
from calibration_lib.common.contract_import import import_contract_products, read_saved_spectrum
from calibration_lib.common.utils import write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.ec_interactive import ECInspector
from calibration_lib.workbench.ec_peaks import catalog
from lmfit.models import ExponentialModel, GaussianModel, LinearModel, QuadraticModel


def peak(channel=0, **changes):
    return {"channel": channel, "E": 511.004, "center": 200., "center_err": .3,
            "sigma": 20., "sigma_err": .2, "lo": 100., "hi": 300.,
            "amplitude": 40000., "amplitude_err": 200., "redchi": 1.1,
            "resolution": 23.548, "resolution_unit": "percent", **changes}


def bins(channel=0):
    x = np.arange(102., 300., 4.)
    return pd.DataFrame({"channel": channel, "x": x, "bin_left": x - 2, "bin_right": x + 2,
                         "density": np.ones(len(x)), "density_err": np.ones(len(x))})


@pytest.mark.parametrize("mode,background,parameters", [
    ("lin + gaus", LinearModel(prefix="b_"), {"b_intercept": 12., "b_slope": -.015}),
    ("quad + gaus", QuadraticModel(prefix="b_"), {"b_a": .0004, "b_b": -.2, "b_c": 36.}),
    ("exp + gaus", ExponentialModel(prefix="e_"), {"e_amplitude": 20., "e_decay": 120.}),
    ("gaus + gaus", GaussianModel(prefix="bk_"),
     {"bk_amplitude": 18000., "bk_center": 150., "bk_sigma": 32.}),
])
def test_saved_ec_curve_matches_original_lmfit_components(tmp_path, mode, background, parameters):
    signal = GaussianModel(prefix="g_")
    model = signal + background
    values = {"g_amplitude": 40000., "g_center": 200., "g_sigma": 20., **parameters}
    fitted = model.make_params(**values)
    row = peak(fit_mode=mode, model_parameters={name: p.value for name, p in fitted.items()})
    output = publish_l1(tmp_path, "14B", "ec", "sample", [row], bins(), {})
    saved = read_saved_spectrum(output["node"], 0, "ec")
    x = saved["curve"]["x"]
    np.testing.assert_allclose(saved["curve"]["y"], model.eval(fitted, x=x), rtol=1e-12)
    np.testing.assert_allclose(saved["curve"]["gaussian"], signal.eval(fitted, x=x), rtol=1e-12)
    np.testing.assert_allclose(saved["curve"]["background"], background.eval(fitted, x=x), rtol=1e-12)
    points = json.loads(open(output["points"], encoding="utf-8").read())
    assert points["0"]["amplitude_kind"] == "height"
    assert points["0"]["area"] == 40000.


def test_import_preserves_role_corrections_after_partial_refit(tmp_path):
    source, destination = tmp_path / "shared", tmp_path / "private"
    for channel, model in ((0, "original"), (1, "revised")):
        publish_l1(source, "14B", "ec", "Cs137_661", [peak(channel, E=661.657)], bins(channel), {
            "kind": "source", "measurement": "Cs137",
            "corr": {"tb_model_sha256": model},
            "background_corr": {str(channel): {"tb_model_sha256": f"{model}-background"}},
        })
    import_contract_products(source, destination, "14B")
    rows = json.loads((destination / "14B/ec/L2/json/src_Cs137_fit_results.l2.json").read_text())
    assert [row["source"] for row in rows] == ["src", "src"]
    assert [row["signal_tb_correction"]["tb_model_sha256"] for row in rows] == ["original", "revised"]
    assert [row["background_tb_correction"]["tb_model_sha256"] for row in rows] == [
        "original-background", "revised-background",
    ]


def test_import_keeps_repeated_source_exposures_distinct_and_multipeaks_together(tmp_path):
    source, destination = tmp_path / "shared", tmp_path / "private"
    for exposure in ("first", "second"):
        for energy in (511.004, 1274.537):
            publish_l1(source, "14B", "ec", f"src_Na22_{exposure}_E{energy:g}",
                       [peak(E=energy, source="src", source_name="Na22",
                             signal_file=f"src_Na22_{exposure}_corr.l1.parquet")], bins(), {
                           "roles": {"0": {"primary": f"{exposure}.event.dat", "background": "bkg.event.dat"}},
                       })
    import_contract_products(source, destination, "14B")
    files = list((destination / "14B/ec/L2/json").glob("src_Na22*_fit_results.l2.json"))
    assert len(files) == 2
    groups = [json.loads(path.read_text()) for path in files]
    assert all(len(rows) == 2 for rows in groups)
    assert all({row["E"] for row in rows} == {511.004, 1274.537} for rows in groups)
    assert all(len({row["signal_file"] for row in rows}) == 1 for rows in groups)
    assert len({rows[0]["signal_file"] for rows in groups}) == 2
    assert all(row["source"] == "src" and row["source_name"] == "Na22" for rows in groups for row in rows)
    sources = catalog(ComputeAdapter(tmp_path), "14B", destination)["spectra"]
    assert len(sources) == 2
    assert [item["source_name"] for item in sources] == ["Na22", "Na22"]


def test_import_keeps_repeated_xray_measurements_distinct(tmp_path):
    source, destination = tmp_path / "shared", tmp_path / "private"
    for measurement in ("40-first", "40-second"):
        publish_l1(source, "14B", "ec", measurement, [peak(E=40.)], bins(),
                   {"kind": "xray", "measurement": measurement})
    import_contract_products(source, destination, "14B")
    files = list((destination / "14B/ec/L2/json").glob("40keV*_fit_results.l2.json"))
    assert len(files) == 2
    assert len({json.loads(path.read_text())[0]["energy_key"] for path in files}) == 2


@pytest.mark.parametrize("band,expected_used", [("low", [20., 40.]), ("high", [100., 200.])])
def test_partial_ec_overview_renders_available_band_only(tmp_path, band, expected_used):
    root = tmp_path / "products"
    params = {"EC_low": None, "EC_high": None}
    params[f"EC_{band}"] = [0., .2, 0.]
    write_json(root / "14B/ec/L3/json/2611021234_14B_EC.l3.json", {
        "analysis_contract": "cali_proc@0.5", "energy_split": 51., "energy_split_high": 75.,
        "calibration_params": {"0": params},
    })
    points = [dict(peak(E=energy), id=f"point-{energy}", quality_pass=True)
              for energy in (20., 40., 52., 100., 200.)]
    for row in points:
        row["center"] = row["E"] / .2
    view = ECInspector(ComputeAdapter(tmp_path)).overview("14B", root, 0, points=points)
    assert [row["E"] for row in view["points"] if row["used"]] == expected_used
    assert view["energy_split_high"] == 75.
    assert view["diagnostics"]["n_used"] == len(expected_used)
    assert len(view["curve"]["x"]) == 250
    assert any(value is not None for value in view["curve"]["y"])
    assert all(row["predicted_energy"] is None for row in view["points"] if row["E"] not in expected_used)
    assert all(value is not None for value in view["curve"]["y"])
    assert [segment["band"] for segment in view["curve"]["segments"]] == [band]
    assert view["curve"]["x"][0] == min(expected_used) / .2
    assert view["curve"]["x"][-1] == max(expected_used) / .2


def test_native_ec_curve_never_reenters_low_energy_polynomial_at_high_adc(tmp_path):
    # Coefficients and observation endpoints from the saved 14B server run.
    # Its low polynomial falls to 2.68 keV at the final high-energy ADC value;
    # choosing a band by its predicted energy drew the false vertical tail.
    params = {
        "EC_low": [-2.2776709050921455e-05, .1748298450475885, 4.178292070750394],
        "EC_high": [-2.641377846003503e-07, .1744770344975735, 6.233783171360028],
    }
    root = tmp_path / "products"
    write_json(root / "14B/ec/L3/json/2611021234_14B_EC.l3.json", {
        "analysis_contract": "cali_proc@0.5", "energy_split": 51., "energy_split_high": 55.,
        "calibration_params": {"0": params},
    })
    values = [(15., 79.41793498481704), (49., 264.48975034911064),
              (52., 267.), (55., 280.3650489604722), (1332.492, 7684.39632830319)]
    points = [dict(peak(E=energy, center=center), id=f"point-{energy}", quality_pass=True)
              for energy, center in values]
    points.append(dict(peak(E=2000., center=200_000.), id="excluded-tail", quality_pass=True))
    write_json(root / "14B/ec/L3/json/L3_manifest.json", {"excluded_points": ["excluded-tail"]})
    view = ECInspector(ComputeAdapter(tmp_path)).overview("14B", root, 0, points=points)
    low, high = view["curve"]["segments"]
    assert low["band"] == "low" and high["band"] == "high"
    assert low["x"][-1] == values[1][1]
    assert high["x"][0] == values[3][1]
    assert high["x"][-1] == values[4][1]
    np.testing.assert_allclose(high["y"], np.polyval(params["EC_high"], high["x"]), rtol=1e-14)
    assert high["y"][-1] > 1300
    assert np.all(np.diff(high["y"]) > 0)
    assert view["curve"]["x"].count(None) == view["curve"]["y"].count(None) == 1
    assert view["curve"]["y"][250] is None


def test_native_ec_curve_needs_two_distinct_used_adc_values(tmp_path):
    root = tmp_path / "products"
    write_json(root / "14B/ec/L3/json/2611021234_14B_EC.l3.json", {
        "analysis_contract": "cali_proc@0.5", "energy_split": 51., "energy_split_high": 55.,
        "calibration_params": {"0": {"EC_low": [0., .2, 0.], "EC_high": None}},
    })
    points = [dict(peak(E=20., center=100.), id="one", quality_pass=True)]
    view = ECInspector(ComputeAdapter(tmp_path)).overview("14B", root, 0, points=points)
    assert view["curve"] == {"x": [], "y": [], "segments": []}


def test_prepared_cache_separates_native_density_from_legacy_bin_rate(tmp_path):
    signal = pd.DataFrame({"channel": 0, "amp_corr": np.linspace(180., 220., 100),
                           "utc": np.linspace(0., 4., 100),
                           "timestamp1": np.linspace(0, 200_000_000, 100).astype(np.int64), "crc_check": True})
    background = signal.assign(amp_corr=np.linspace(100., 140., 100))
    signal_path, background_path = tmp_path / "signal.parquet", tmp_path / "background.parquet"
    write_parquet(signal_path, signal)
    write_parquet(background_path, background)
    inspector = ECInspector(ComputeAdapter(tmp_path))
    legacy = ECL2Processor("14B", tmp_path)
    native = ECL2Processor("14B", tmp_path, analysis_contract="cali_proc@0.5")
    old = inspector._prepared(legacy, signal_path, 0, background_path, 0, 4.)[0]
    new = inspector._prepared(native, signal_path, 0, background_path, 0, 4.)[0]
    expected = native._prepare_spectrum(signal, background, 4.)
    np.testing.assert_allclose(new["spec"], expected["spec"])
    assert new["primary_exposure_s"] == 2.
    # 4 s legacy bin rates versus 2 s native rates per ADC, with 4 ADC bins.
    assert np.sum(new["spec"]) == pytest.approx(np.sum(old["spec"]) / 2.)
