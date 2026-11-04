"""Scoped aggregates fit changed channels and carry other models unchanged."""

import hashlib
import json
from pathlib import Path

import pytest
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.tb.processors.L2_processor import TBL2Processor


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def tb_model(ch, *, native=False):
    record = {"G0": .012, "k": .0184 + ch * .0001, "V0": 24.11, "b": 54.31, "c": 23459.83,
              "G0_err": .001, "k_err": .001, "V0_err": .01, "b_err": .1, "c_err": 1., "chisquare": 2.}
    if native:
        record.update(channel=ch, payload="14B", chi2=2., chi2_reduced=2. / 3., n_points=8,
                      ndf=3, redchi=2. / 3., points=8)
    return record


def tb_inputs(root, *, native=False):
    for index in range(8):
        rows = [{"channel": ch, "center": 300. + index * 10 + ch, "center_err": 1.,
                 "temp": 10. + index, "bias": 27. + index * .1,
                 **({"analysis_contract": "cali_proc@0.5"} if native else {})} for ch in range(4)]
        put(root / "14B/tb/L1/json" / f"point{index}_fit_results.l1.json", rows)
    (root / "14B/tb/L0/parquet").mkdir(parents=True)


@pytest.mark.parametrize("native", [False, True])
def test_tb_aggregate_only_fits_selected_channel_and_preserves_other_coefficients(tmp_path, monkeypatch, native):
    tb_inputs(tmp_path, native=native)
    original = [tb_model(ch, native=native) for ch in range(4)]
    put(tmp_path / "14B/tb/L2/json/2601010000_14B_TB.l2.json", original)
    processor = TBL2Processor("14B", tmp_path)
    calls = []

    def fit(rows):
        ch = rows[0]["channel"]
        calls.append(ch)
        return {**tb_model(ch, native=True), "k": .022, "chi2": 3., "chi2_reduced": 1.}

    def plot(rows, model, path, channel):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"plot fixture")

    monkeypatch.setattr(processor, "_fit_surface", fit)
    monkeypatch.setattr(processor, "_plot_channel_fit", plot)
    result = processor.process(tmp_path / "14B/tb/L1", fit_channels=[1])
    assert result["errors"] == []
    assert calls == [1]
    assert result["channels_updated"] == [1] and result["channels_reused"] == [0, 2, 3]
    output = json.loads((tmp_path / "14B/tb/L2/json" / result["output_json"]).read_text())
    for ch in (0, 2, 3):
        assert output[ch]["k"] == original[ch]["k"]
        assert output[ch]["chisquare"] == original[ch]["chisquare"]
    if native:
        full = json.loads(Path(result["canonical"]["model_paths"][0]).read_text(encoding="utf-8"))
        assert all(full[ch] == original[ch] for ch in (0, 2, 3))


def test_tb_without_prior_aggregate_computes_every_required_channel(tmp_path, monkeypatch):
    tb_inputs(tmp_path)
    processor = TBL2Processor("14B", tmp_path)
    calls = []

    def fit(rows):
        calls.append(rows[0]["channel"])
        return tb_model(rows[0]["channel"], native=True)

    monkeypatch.setattr(processor, "_fit_surface", fit)
    monkeypatch.setattr(processor, "_plot_channel_fit", lambda *args: None)
    result = processor.process(tmp_path / "14B/tb/L1", fit_channels=[1])
    assert result["errors"] == []
    assert calls == [0, 1, 2, 3]
    assert result["channels_reused"] == []


def ec_model(ch, *, native=False):
    result = {"channel": ch}
    for field in ("EC_low", "EC_high", "resolution_low", "resolution_high"):
        result[field] = [0., 2. + ch, 1.]
        result[field + "_err"] = [.001] * 3
    if native:
        result.update(analysis_contract="cali_proc@0.5", resolution_unit="fraction",
                      resolution_coefficient_order="aE2+bE+c", energy_model="E=f(ADC)")
    return result


def ec_inputs(root, *, native=False):
    model = root / "tb-model.json"
    model.write_text("model bytes", encoding="utf-8")
    sha = hashlib.sha256(model.read_bytes()).hexdigest()
    binding = {"tb_model": str(model), "tb_model_sha256": sha, "ref_temp_C": 25., "ref_bias_V": 28.5}
    for energy in (40., 43., 46., 49., 55., 70., 100., 200.):
        rows = [{"channel": ch, "E": energy, "center": energy * 5 + ch,
                 "center_err": .3, "sigma": 3., "sigma_err": .2, "resolution": 4., "resolution_err": .1,
                 "rsquared": .99, "data_type": "xray",
                 **({"analysis_contract": "cali_proc@0.5", "signal_tb_correction": binding,
                     "background_tb_correction": binding} if native else {})}
                for ch in range(4)]
        put(root / "14B/ec/L2/json" / f"{energy:g}keV_fit_results.l2.json", rows)
    return binding


@pytest.mark.parametrize("native", [False, True])
def test_ec_aggregate_only_fits_selected_channel_with_unchanged_other_models(tmp_path, monkeypatch, native):
    binding = ec_inputs(tmp_path, native=native)
    prior = {str(ch): ec_model(ch, native=native) for ch in range(4)}
    meta = {"reference": {**binding, "tb_model_sha256": "old-tb-for-unmodified-channels"},
            "inputs": [{"file": "old", "channel": ch, "energy_keV": 40.} for ch in range(4)]}
    meta_path = tmp_path / "previous.meta.json"
    put(meta_path, meta)
    put(tmp_path / "14B/ec/L3/json/2601010000_14B_EC.l3.json",
        {"calibration_params": prior, "analysis_contract": "cali_proc@0.5" if native else None,
         "contract_meta": str(meta_path)})
    processor = ECL3Processor("14B", tmp_path)
    calls = []

    def fit(ch, *args):
        results = args[-1]
        calls.append(ch)
        results["calibration_params"][str(ch)] = ec_model(ch, native=native)
        results["calibration_params"][str(ch)]["EC_low"] = [.01, 3., 5.]

    monkeypatch.setattr(processor, "_fit_channel_native" if native else "_fit_channel", fit)
    cfg = {"fit_channels": [1], **({"analysis_contract": "cali_proc@0.5"} if native else {})}
    result = processor.process(tmp_path / "14B/ec/L2", cfg)
    assert result["errors"] == []
    assert calls == [1]
    assert result["channels_updated"] == [1] and result["channels_reused"] == [0, 2, 3]
    assert all(result["calibration_params"][str(ch)] == prior[str(ch)] for ch in (0, 2, 3))
    if native:
        assert result["reference_by_channel"]["1"]["tb_model_sha256"] == binding["tb_model_sha256"]
        assert result["reference_by_channel"]["0"]["tb_model_sha256"] == "old-tb-for-unmodified-channels"


def test_ec_without_prior_aggregate_computes_all_channels(tmp_path, monkeypatch):
    ec_inputs(tmp_path)
    processor = ECL3Processor("14B", tmp_path)
    calls = []

    def fit(ch, *args):
        calls.append(ch)
        args[-1]["calibration_params"][str(ch)] = ec_model(ch)

    monkeypatch.setattr(processor, "_fit_channel", fit)
    result = processor.process(tmp_path / "14B/ec/L2", {"fit_channels": [1]})
    assert result["errors"] == []
    assert calls == [0, 1, 2, 3]


def test_native_ec_must_recompute_when_prior_is_a_legacy_model(tmp_path, monkeypatch):
    ec_inputs(tmp_path, native=True)
    put(tmp_path / "14B/ec/L3/json/2601010000_14B_EC.l3.json",
        {"calibration_params": {str(ch): ec_model(ch) for ch in range(4)}})
    processor = ECL3Processor("14B", tmp_path)
    calls = []

    def fit(ch, *args):
        calls.append(ch)
        args[-1]["calibration_params"][str(ch)] = ec_model(ch, native=True)

    monkeypatch.setattr(processor, "_fit_channel_native", fit)
    result = processor.process(tmp_path / "14B/ec/L2", {"fit_channels": [1], "analysis_contract": "cali_proc@0.5"})
    assert result["errors"] == []
    assert calls == [0, 1, 2, 3]
