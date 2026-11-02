"""Real Co60 windows retain the high-energy photopeak's component identity."""

import json
from pathlib import Path

import numpy as np
import pytest
from calibration_lib.common.cali_contract import CONTRACT, gehrels_error
from calibration_lib.ec.processors.L2_processor import ECL2Processor

FIXTURE = json.loads((Path(__file__).parent / "fixtures/14B_Co60_native_windows.json").read_text(encoding="utf-8-sig"))
REFERENCE_CENTERS = [7684.3963283, 8115.5913630, 7812.0131520, 7373.9196030]
REFERENCE_UNCERTAINTIES = [5.6243212, 11.8132706, 5.6427011, 6.9159329]


@pytest.mark.parametrize("spectrum", FIXTURE["spectra"], ids=lambda s: f"Co60-ch{s['channel']}")
def test_real_co60_separates_target_from_overlapping_lower_line(tmp_path, spectrum):
    p = ECL2Processor("14B", tmp_path, analysis_contract=CONTRACT)
    p._background_model = "gaus"
    primary, background = (np.asarray(spectrum[f"{role}_counts"]) for role in ("primary", "background"))
    tp, tb, width = (spectrum[name] for name in ("primary_exposure_s", "background_exposure_s", "bin_width"))
    x = spectrum["x0"] + width * np.arange(len(primary))
    y = (primary / tp - background / tb) / width
    err = np.hypot(gehrels_error(primary) / tp, gehrels_error(background) / tb) / width
    lo, hi = spectrum["window"]
    # An earlier strongest-candidate initializer selected the lower-energy
    # shoulder. The configured nuisance model must identify the right peak.
    _, result, _, _ = p._select_best_fit(x, y, err, lo + .05 * (hi - lo), lo, hi, width, (hi - lo) / 4)
    ch = spectrum["channel"]
    assert result.params["g_center"].value == pytest.approx(REFERENCE_CENTERS[ch], abs=.002)
    assert result.params["g_center"].stderr == pytest.approx(REFERENCE_UNCERTAINTIES[ch], rel=.002)
    assert result.params["bk_center"].value < result.params["g_center"].value
    assert all(result.params[name].stderr is not None and result.params[name].stderr > 0
               for name in result.var_names)
    assert np.isfinite(result.covar).all()
    assert result.chisqr == pytest.approx(np.sum(((y - result.best_fit) / err)**2))


def test_explicit_center_anchor_can_select_a_different_source_line(tmp_path):
    p = ECL2Processor("14B", tmp_path, analysis_contract=CONTRACT)
    x = np.arange(6502., 8300., 4.)
    y = 150 * np.exp(-.5 * ((x - 7000) / 150)**2) + 100 * np.exp(-.5 * ((x - 7800) / 140)**2)
    result, _ = p._fit_with_mode("gaus + gaus", x, y, np.ones_like(x), 7800., 6500., 8300., 4.,
                                 center_anchor=7000.)
    assert result.params["g_center"].value == pytest.approx(7000., abs=.001)
    assert result.params["bk_center"].value == pytest.approx(7800., abs=.001)


def test_real_90kev_fit_can_be_narrower_than_peak_finder_estimate(tmp_path):
    s = json.loads((Path(__file__).parent / "fixtures/14B_90keV_ch3_native_window.json").read_text(encoding="utf-8-sig"))
    p = ECL2Processor("14B", tmp_path, analysis_contract=CONTRACT)
    p._background_model = "lin"
    a, b = np.asarray(s["primary_counts"]), np.asarray(s["background_counts"])
    ta, tb, width = s["primary_exposure_s"], s["background_exposure_s"], s["bin_width"]
    x = s["x0"] + width * np.arange(len(a))
    y = (a / ta - b / tb) / width
    err = np.hypot(gehrels_error(a) / ta, gehrels_error(b) / tb) / width
    _, fit, _, _ = p._select_best_fit(x, y, err, 475.69, *s["window"], width, 169.52294)
    assert fit.params["g_center"].value == pytest.approx(482.6697341, abs=.0005)
    assert fit.params["g_sigma"].value == pytest.approx(44.4873572, abs=.0005)
    assert fit.params["g_center"].stderr == pytest.approx(1.83689642, rel=.001)
    assert fit.params["g_sigma"].value < 169.52294 / 3


@pytest.mark.parametrize("background", [None, "lin", "exp", "quad", "gaus"])
def test_every_native_background_uses_window_bounds_instead_of_candidate_width(tmp_path, background):
    p = ECL2Processor("14B", tmp_path, analysis_contract=CONTRACT)
    p._background_model = background
    x = np.arange(102., 300., 4.)
    continuum = {None: np.zeros_like(x), "lin": 3 + .01 * x, "exp": 20 * np.exp(-x / 180),
                 "quad": 3 + .0002 * (x - 200)**2,
                 "gaus": 30 * np.exp(-.5 * ((x - 125) / 20)**2)}[background]
    y = 100 * np.exp(-.5 * ((x - 200) / 10)**2) + continuum
    _, fit, _, _ = p._select_best_fit(x, y, np.ones_like(x), 200., 100., 300., 4., 169.)
    assert fit.params["g_center"].value == pytest.approx(200., abs=.001)
    assert fit.params["g_sigma"].value == pytest.approx(10., abs=.001)
