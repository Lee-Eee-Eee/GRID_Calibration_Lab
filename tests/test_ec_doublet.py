"""Co60 identities remain separate through config, joint fitting and preview."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.cali_contract import build_acquisition_profile, publish_l1, source_peak_entries
from calibration_lib.common.contract_import import read_saved_spectrum
from calibration_lib.common.utils import write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.workbench.acquisition_store import freeze_profile, load_profile, prepare_profile
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.ec_peaks import catalog
from lmfit.models import GaussianModel, LinearModel

ENERGIES = [1173.228, 1332.492]


def doublet():
    return {"fit_mode": "doublet", "background": "lin", "fit_range": [[5800., 8500.]] * 4,
            "peaks": [{"energy_keV": ENERGIES[0], "center_anchor": [6682.] * 4},
                      {"energy_keV": ENERGIES[1], "center_anchor": [7638.] * 4}]}


def test_doublet_yaml_preset_roundtrip_syncs_energies_and_initial_editor_windows(tmp_path):
    import yaml

    profile = build_acquisition_profile("15B", tmp_path, verify_files=False)
    profile["analysis_config"]["ec_l1"]["source"]["Co60"].update(doublet())
    profile["analysis_config_text"] = {name: yaml.safe_dump(value)
                                       for name, value in profile["analysis_config"].items()}
    restored = load_profile(freeze_profile(tmp_path / "run", prepare_profile(tmp_path, profile), 2))
    source = next(row for row in restored["discovery"]["sources"] if row["name"] == "Co60")
    assert source["energies_keV"] == ENERGIES
    measurement = next(row for row in restored["discovery"]["manifest"] if row.get("source") == "Co60")
    assert measurement["peak_energies_keV"] == ENERGIES
    products = tmp_path / "products"
    write_parquet(products / "15B/ec/L1/parquet/src_Co60_40m_corr.l1.parquet",
                  pd.DataFrame({"amp_corr": [1.]}),
                  metadata={"source_name": "Co60", "peak_energies_keV": [1332.492]}, meta_key="tb_correction")
    lines = catalog(ComputeAdapter(tmp_path, acquisition=restored), "15B", products)["spectra"][0]["lines"]
    assert [row["energy_keV"] for row in lines] == ENERGIES
    assert [row["centers"]["2"] for row in lines] == [6682., 7638.]
    assert all(row["windows"]["2"] == {"lo": 5800., "hi": 8500.} for row in lines)
    assert profile["discovery"]["sources"] != restored["discovery"]["sources"]


def test_nonjoint_multiple_lines_never_inherit_old_single_peak_window():
    entry = {"energy_keV": 1332.492, "fit_range": [[6000., 7400.]] * 4,
             "background": "lin", "peaks": [{"energy_keV": value} for value in ENERGIES]}
    lines = source_peak_entries(entry)
    assert all("fit_range" not in line for line in lines)
    assert all(line["background"] == "lin" for line in lines)
    with pytest.raises(ValueError, match="Duplicate"):
        source_peak_entries({"peaks": [{"energy_keV": 1173.228}, {"energy_keV": 1173.228}]})
    invalid = doublet()
    invalid["peaks"][1]["center_anchor"][2] = None
    with pytest.raises(ValueError, match="independent center anchors"):
        source_peak_entries(invalid)


def test_joint_fitter_recovers_both_identities_without_energy_ratio_constraint(tmp_path):
    x = np.arange(5802., 8500., 4.)
    model = GaussianModel(prefix="low_") + GaussianModel(prefix="high_") + LinearModel(prefix="b_")
    truth = model.make_params(low_amplitude=150., low_center=6712., low_sigma=190.,
                              high_amplitude=90., high_center=7695., high_sigma=210.,
                              b_intercept=.015, b_slope=-1.e-6)
    errors = np.full(len(x), .002)
    y = model.eval(truth, x=x) + np.random.default_rng(42).normal(0., errors)
    proc = ECL2Processor("15B", tmp_path, analysis_contract="cali_proc@0.5")
    results = []
    for energy, anchor in zip(ENERGIES, (6682., 7638.), strict=True):
        proc._active_doublet = {"centers": [6682., 7638.], "energies_keV": ENERGIES,
                                "target_energy_keV": energy}
        proc._background_model = "lin"
        mode, fitted, _, _ = proc._select_best_fit(x, y, errors, anchor, 5800., 8500., 4., center_anchor=anchor)
        assert mode == "gaus + gaus + lin"
        assert fitted.redchi == pytest.approx(1., abs=.15)
        assert fitted.scale_covar is False
        assert fitted.params["g_center"].value == pytest.approx(6712. if energy == ENERGIES[0] else 7695., abs=3.)
        assert fitted.params["g_center"].stderr > 0
        results.append(fitted)
    np.testing.assert_allclose(results[0].best_fit, results[1].best_fit, rtol=1e-6, atol=1e-9)
    assert results[0].params["g_center"].value == pytest.approx(results[1].params["bk_center"].value, abs=1e-4)
    assert results[1].params["g_center"].value == pytest.approx(results[0].params["bk_center"].value, abs=1e-4)
    indices = [results[0].var_names.index(name) for name in ("g_center", "bk_center")]
    assert np.isfinite(results[0].covar[np.ix_(indices, indices)]).all()


def test_joint_saved_curve_includes_companion_and_continuum(tmp_path):
    model = GaussianModel(prefix="g_") + GaussianModel(prefix="bk_") + LinearModel(prefix="b_")
    fitted = model.make_params(g_amplitude=150., g_center=6712., g_sigma=190.,
                              bk_amplitude=90., bk_center=7695., bk_sigma=210.,
                              b_intercept=.015, b_slope=-1.e-6)
    x = np.arange(5802., 8500., 4.)
    row = {"channel": 0, "E": 1173.228, "center": 6712., "center_err": 2., "sigma": 190.,
           "sigma_err": 1., "lo": 5800., "hi": 8500., "amplitude": 150., "amplitude_err": 2.,
           "fit_mode": "gaus + gaus + lin", "fit_group": "Co60:CH0:doublet",
           "model_parameters": {name: value.value for name, value in fitted.items()}}
    bins = pd.DataFrame({"channel": 0, "x": x, "bin_left": x - 2, "bin_right": x + 2,
                         "density": model.eval(fitted, x=x), "density_err": .002})
    saved = publish_l1(tmp_path, "15B", "ec", "Co60_1173", [row], bins, {})
    restored = read_saved_spectrum(saved["node"], 0, "ec")
    np.testing.assert_allclose(restored["curve"]["y"], model.eval(fitted, x=x), rtol=1e-12)
    assert json.loads(open(saved["points"], encoding="utf-8").read())["0"]["fit_group"] == row["fit_group"]


def test_real_15b_co60_density_recovers_independent_doublet_reference(tmp_path):
    data = json.loads((Path(__file__).parent / "fixtures/co60_doublet_15b_ch0_density.json").read_text())
    x, y, errors = (np.asarray(data[key]) for key in ("x", "density", "density_err"))
    assert data["provenance"]["bin_width"] == 4.
    proc = ECL2Processor("15B", tmp_path, analysis_contract="cali_proc@0.5")
    proc._background_model = "lin"
    reference = [(1173.228, 6682., 6712.570608317, 4.627431876),
                 (1332.492, 7638., 7694.781735037, 5.855936713)]
    curves = []
    for energy, anchor, center, uncertainty in reference:
        proc._active_doublet = {"centers": [6682., 7638.], "energies_keV": ENERGIES,
                                "target_energy_keV": energy}
        _, fitted, _, _ = proc._select_best_fit(x, y, errors, anchor, 5800., 8500., 4., center_anchor=anchor)
        assert fitted.params["g_center"].value == pytest.approx(center, abs=1.e-3)
        assert fitted.params["g_center"].stderr == pytest.approx(uncertainty, rel=1.e-4)
        assert fitted.redchi == pytest.approx(.9089165149, rel=1.e-8)
        curves.append(fitted.best_fit)
    np.testing.assert_allclose(curves[0], curves[1], rtol=1.e-10, atol=1.e-14)
