"""Read canonical products through small private compatibility indexes.

Only JSON indexes are generated. Event, telemetry and saved spectrum tables stay
at their selected source paths and are read lazily by the workbench.
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .cali_contract import CONTRACT, _write_json
from .utils import read_json


def _payload_root(source_root: Path, source_payload: str) -> Path:
    source_root = Path(source_root).resolve()
    if (source_root / "tb").is_dir() or (source_root / "ec").is_dir():
        return source_root
    candidate = source_root / source_payload
    if not candidate.is_dir():
        raise FileNotFoundError(f"No canonical payload products in {source_root}")
    return candidate


def _compat_point(point: dict, channel: int, kind: str, node: Path) -> dict:
    row = copy.deepcopy(point)
    row["channel"] = channel
    for native, old in (("center_std", "center_err"), ("sigma_std", "sigma_err"),
                        ("temperature_C", "temp"), ("temperature_std_C", "temp_err"),
                        ("bias_V", "bias"), ("bias_std_V", "bias_err"), ("energy_keV", "E")):
        if native in point:
            row[old] = point[native]
    window = point.get("window")
    if isinstance(window, list) and len(window) == 2:
        row["window_lo"], row["window_hi"] = window
        row["lo"], row["hi"] = window
    sigma = point.get("sigma")
    if sigma is not None and sigma > 0:
        divisor = float(sigma) * np.sqrt(2 * np.pi)
        row["height"] = point.get("amplitude")
        row["amplitude"] = float(point.get("amplitude", 0)) * divisor
        value = point.get("amplitude_std")
        row["amplitude_err"] = float(value) * divisor if value is not None else None
        row["fwhm"] = float(sigma) * 2 * np.sqrt(2 * np.log(2))
    factor = 100. if kind == "ec" else 1.
    if point.get("resolution") is not None:
        row["resolution"] = factor * point["resolution"]
    if point.get("resolution_std") is not None:
        row["resolution_err"] = factor * point["resolution_std"]
    background = point.get("background")
    row.setdefault("fit_mode", {"lin": "lin + gaus", "exp": "exp + gaus", "quad": "quad + gaus"}.get(background, "gaus"))
    params = point.get("background_params", {})
    center = point.get("center") or 0.
    if background == "lin":
        row["slope"] = params.get("b1", 0.)
        row["intercept"] = params.get("b0", 0.) - row["slope"] * center
    elif background == "quad":
        row["quad_a"] = params.get("b2", 0.)
        row["quad_b"] = params.get("b1", 0.) - 2 * row["quad_a"] * center
        row["quad_c"] = params.get("b0", 0.) - params.get("b1", 0.) * center + row["quad_a"] * center**2
    row.setdefault("fit_quality", {"status": point.get("qa_status", "unknown"),
                                    "redchi": point.get("redchi"), "warning_only": True,
                                    "warnings": []})
    row.update(analysis_contract=CONTRACT, contract_node=str(node),
               chi2_reduced=point.get("redchi"),
               contract_spectrum=str(node / "spectra.parquet"), contract_meta=str(node / "meta.json"),
               native_analysis_id=node.name, ordinate="Counts/ADC" if kind == "tb" else "Counts/(s·ADC)")
    if point.get("status") == "unconfirmed":
        row.update(fit_performed=False, fit_status="not_fitted")
    return row


def import_contract_products(
    source_root: Path, product_root: Path, payload: str, *, source_payload: str | None = None,
) -> dict:
    """Build a run-local view without copying shared Parquet or image files."""
    root = _payload_root(source_root, source_payload or payload)
    destination = Path(product_root) / payload
    if root == destination.resolve():
        raise ValueError("Compatibility indexes require a separate run-local product root")
    index = {"analysis_contract": CONTRACT, "source_root": str(root), "payload": payload,
             "analyses": [], "models": [], "outputs": [], "parquet_files_copied": 0}
    ec_groups = {}
    for kind in ("tb", "ec"):
        for points_path in sorted((root / kind / "L1").glob("*/points.json")):
            node = points_path.parent
            meta = read_json(node / "meta.json")
            points = read_json(points_path)
            if not isinstance(points, dict):
                raise ValueError(f"{points_path}: canonical points must be a channel mapping")
            records = [_compat_point(row, int(ch), kind, node) for ch, row in points.items()]
            if kind == "tb":
                target = destination / "tb/L1/json" / f"{node.name}_fit_results.l1.json"
                _write_json(target, records)
                index["outputs"].append(str(target))
            else:
                for record in records:
                    channel = str(record["channel"])
                    evidence = meta.get("channel_evidence", {}).get(channel, meta)
                    correction = evidence.get("corr", {})
                    if channel in correction and isinstance(correction[channel], dict):
                        correction = correction[channel]
                    if correction and not record.get("signal_tb_correction"):
                        record["signal_tb_correction"] = copy.deepcopy(correction)
                    background = evidence.get("background_corr", {}).get(channel, correction)
                    if background and not record.get("background_tb_correction"):
                        record["background_tb_correction"] = copy.deepcopy(background)
                    source_name = record.get("source_name")
                    source_kind = meta.get("kind", record.get("source"))
                    is_source = source_kind in ("source", "src")
                    if is_source:
                        source_name = source_name or meta.get("measurement", meta.get("source_name", node.name))
                        energy_key = f"src_{source_name}"
                        observation = record.get("signal_file") or evidence.get("roles", {}).get(channel, {}).get("primary")
                        observation = str(observation or meta.get("measurement", node.name))
                        observation = observation.removesuffix("_corr.l1.parquet")
                    else:
                        energy_key = f"{record['E']:g}keV"
                        observation = str(meta.get("measurement", node.name))
                    record.update(source="src" if is_source else "x", source_name=source_name)
                    record.setdefault("signal_file", f"{node.name}_corr.l1.parquet")
                    ec_groups.setdefault((energy_key, observation), []).append(record)
            index["analyses"].append({"kind": kind, "id": node.name, "node": str(node),
                                       "channels": sorted(int(ch) for ch in points), "meta": str(node / "meta.json")})
    for (base, observation), records in ec_groups.items():
        # Energy alone is not an observation identity: repeated exposures of a
        # source (or X-ray energy) must remain independently selectable points.
        repeated = sum(key[0] == base for key in ec_groups) > 1
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(observation).name)[:60]
        suffix = hashlib.sha256(observation.encode("utf-8")).hexdigest()[:8]
        key = f"{base}__{slug}-{suffix}" if repeated else base
        for record in records:
            record["energy_key"] = key
        target = destination / "ec/L2/json" / f"{key}_fit_results.l2.json"
        _write_json(target, records)
        index["outputs"].append(str(target))
    for path in sorted((root / "tb/L2").glob("*/*_temp_bias_fit.json")):
        values = read_json(path)
        target = destination / "tb/L2/json" / f"{path.parent.name}_{payload}_TB.l2.json"
        _write_json(target, values)
        index["models"].append({"kind": "tb", "model": str(path), "meta": str(path.with_suffix(".meta.json"))})
        index["outputs"].append(str(target))
    for meta_path in sorted((root / "ec/L2").glob("*/*_ec_fit.meta.json")):
        meta = read_json(meta_path)
        models = {str(record["channel"]): record
                  for record in (read_json(path) for path in sorted(meta_path.parent.glob("*_ec_coef_sci_ch*.json")))}
        target = destination / "ec/L3/json" / f"{meta_path.parent.name}_{payload}_EC.l3.json"
        _write_json(target, {"payload": payload, "timestamp": meta_path.parent.name,
                             "analysis_contract": CONTRACT, "calibration_params": models,
                             "resolution_unit": "fraction", "resolution_coefficient_order": "aE2+bE+c",
                             "energy_split": meta.get("config", {}).get("fit", {}).get("split_low_keV", 51.),
                             "energy_split_high": meta.get("config", {}).get("fit", {}).get("split_high_keV", 55.),
                             "contract_meta": str(meta_path)})
        index["outputs"].append(str(target))
        index["models"].append({"kind": "ec", "node": str(meta_path.parent), "meta": str(meta_path)})
    _write_json(destination / "contract_import.json", index)
    return index


def read_saved_spectrum(node: Path, channel: int, kind: str) -> dict:
    """Return saved density bins and the stored model in workbench plot form."""
    node = Path(node)
    meta, points = read_json(node / "meta.json"), read_json(node / "points.json")
    point = points[str(channel)]
    frame = pd.read_parquet(node / "spectra.parquet", filters=[("channel", "=", int(channel))])
    if frame.empty:
        raise ValueError(f"No saved bins for CH{channel} in {node}")
    x, y = frame["x"].to_numpy(), frame["density"].to_numpy()
    error = frame["density_err"].to_numpy()
    width = float(np.median(frame["bin_right"] - frame["bin_left"]))
    row = _compat_point(point, channel, kind, node)
    lo, hi = float(frame["bin_left"].min()), float(frame["bin_right"].max())
    window = point.get("window") or [lo, hi]
    curve = None
    if point.get("center") is not None and (point.get("sigma") or 0) > 0 and point.get("status") != "unconfirmed":
        keep = (x >= window[0]) & (x <= window[1])
        xx = x[keep]
        gaussian = point["amplitude"] * np.exp(-.5 * ((xx - point["center"]) / point["sigma"])**2)
        background = _background(point, xx)
        curve = {"x": xx, "y": gaussian + background, "gaussian": gaussian, "background": background}
    ordinate = meta.get("ordinate", meta.get("fit", {}).get("ordinate"))
    if not isinstance(ordinate, str):
        ordinate = "Counts/ADC" if kind == "tb" else "Counts/(s·ADC)"
    roles = meta.get("channel_evidence", {}).get(str(channel), meta)
    return {"x": x, "y": y, "error": error, "errors": error, "curve": curve, "fit": row,
            "channel": int(channel), "stem": node.name, "source": "canonical",
            "source_path": str(node / "spectra.parquet"), "path": str(node / "spectra.parquet"),
            "ordinate": ordinate, "normalization_unit": point.get("exposure_unit", "s"),
            "n_events": point.get("n_events", point.get("primary_events")),
            "histogram_config": {"min": lo, "max": hi, "bin_width": width},
            "window_bounds": {"min": lo, "max": hi, "minimum_width": width * 8},
            "default_window": {"lo": window[0], "hi": window[1]},
            "metadata": roles, "contract_meta": str(node / "meta.json"),
            "contract_node": str(node), "analysis_contract": CONTRACT,
            "net": y, "signal": (frame["primary_rate"].to_numpy() / width if "primary_rate" in frame else y),
            "background": (frame["background_rate"].to_numpy() / width if "background_rate" in frame else np.zeros_like(y))}


def _background(point: dict, x: np.ndarray) -> np.ndarray:
    params, mode = point.get("background_params", {}), point.get("background")
    shifted = x - point["center"]
    if mode == "lin":
        return params.get("b0", 0.) + params.get("b1", 0.) * shifted
    if mode == "quad":
        return params.get("b0", 0.) + params.get("b1", 0.) * shifted + params.get("b2", 0.) * shifted**2
    if mode == "exp":
        return params.get("b0", 0.) * np.exp(-params.get("k", 0.) * (x - point["window"][0]))
    if mode in ("gaus", "gaus+lin") and params.get("bg_sigma", 0.) > 0:
        background = params.get("bg_amplitude", 0.) * np.exp(-.5 * ((x - params["bg_center"]) / params["bg_sigma"])**2)
        if mode == "gaus+lin":
            background += params.get("b0", 0.) + params.get("b1", 0.) * shifted
        return background
    return np.zeros_like(x, dtype=float)
