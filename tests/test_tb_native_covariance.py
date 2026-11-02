"""Real 14B spectra: free native linear continuum and absolute fit covariance.

The counts are CRC-valid 4 ADC bins from the configured windows of the two
captures below, distilled from the server regression run 20261102-092805-
e93e8e578290. No event data or historical fit is needed to run these regressions.
"""

from pathlib import Path

import numpy as np
import pytest
from calibration_lib.common.cali_contract import CONTRACT
from calibration_lib.tb.processors.L1_processor import TBL1Processor

REAL_SPECTRA = [
    {
        "capture": "m20C-290-107", "channel": 2, "x0": 338.,
        "seed": (466., 56.97358872444968), "window": (335.3, 620.7),
        "center": 465.1901837458, "center_std": .5321401037,
        "counts": [134, 162, 166, 170, 244, 230, 286, 298, 352, 401, 424, 478, 492, 547, 613,
                   647, 783, 803, 814, 851, 874, 926, 1036, 1117, 1166, 1108, 1163, 1158,
                   1153, 1200, 1251, 1158, 1229, 1302, 1242, 1225, 1240, 1280, 1151, 1122,
                   1058, 1046, 957, 942, 880, 861, 766, 750, 679, 677, 608, 594, 566, 467,
                   466, 436, 424, 370, 307, 262, 267, 249, 231, 183, 182, 136, 137, 124,
                   120, 110, 91],
    },
    {
        "capture": "m30C-287-117", "channel": 1, "x0": 290.,
        "seed": (450., 53.295467074528275), "window": (288.3, 611.7),
        "center": 451.7231310679, "center_std": .3330343889,
        "counts": [42, 52, 55, 53, 69, 75, 83, 111, 112, 117, 132, 188, 209, 226, 251, 294,
                   284, 388, 405, 466, 512, 582, 589, 659, 744, 816, 907, 926, 982, 1069,
                   1107, 1182, 1222, 1313, 1310, 1386, 1395, 1431, 1473, 1552, 1408, 1440,
                   1458, 1359, 1388, 1392, 1347, 1280, 1264, 1126, 1186, 1032, 1112, 970,
                   868, 854, 753, 706, 651, 628, 608, 513, 498, 434, 383, 346, 326, 284,
                   271, 231, 210, 201, 169, 150, 112, 111, 118, 96, 80, 57, 58],
    },
]


@pytest.mark.parametrize("spectrum", REAL_SPECTRA, ids=lambda s: f"{s['capture']}-ch{s['channel']}")
def test_native_linear_continuum_matches_real_peak_and_absolute_covariance(tmp_path, spectrum):
    processor = TBL1Processor("14B", tmp_path, analysis_contract=CONTRACT)
    processor._active_bin_width = 4.
    processor._native_background = "lin"
    x = spectrum["x0"] + 4 * np.arange(len(spectrum["counts"]))
    y = np.asarray(spectrum["counts"]) / 4.
    seed, sigma = spectrum["seed"]
    lo, hi = spectrum["window"]
    result, _, mode, _ = processor._select_best_fit(
        x, y, seed, max(lo, seed - 1.5 * sigma), min(hi, seed + 1.5 * sigma), sigma,
    )
    assert mode == "lin + gaus"
    assert result.params["b_intercept"].value < 0
    assert result.params["g_center"].value == pytest.approx(spectrum["center"], abs=.0005)
    assert result.params["g_center"].stderr == pytest.approx(spectrum["center_std"], rel=.001)
    assert all(result.params[name].stderr > 0 for name in result.var_names)

    # Independently calculate (Jᵀ W J)⁻¹ in physical parameter coordinates.
    # Absolute counting errors must not be multiplied by the residual redchi.
    area, center, width = (result.params[f"g_{name}"].value for name in ("amplitude", "center", "sigma"))
    gaussian = area * np.exp(-.5 * ((x - center) / width)**2) / (width * np.sqrt(2 * np.pi))
    derivatives = {
        "g_amplitude": gaussian / area,
        "g_center": gaussian * (x - center) / width**2,
        "g_sigma": gaussian * ((x - center)**2 / width**3 - 1 / width),
        "b_slope": x, "b_intercept": np.ones_like(x),
    }
    jacobian = np.column_stack([derivatives[name] for name in result.var_names]) / processor._fit_errors(y)[:, None]
    covariance = np.linalg.inv(jacobian.T @ jacobian)
    np.testing.assert_allclose(np.diag(result.covar), np.diag(covariance), rtol=.002)


def test_legacy_linear_continuum_retains_existing_bound():
    processor = TBL1Processor("14B", Path("."))
    x = np.arange(100., 300., 4.)
    y = 30 * np.exp(-.5 * ((x - 200) / 20)**2) + 4
    result, _ = processor._fit_lin_gaus(x, y, 200., 160., 240., 20.)
    assert result.params["b_intercept"].min == 0.
