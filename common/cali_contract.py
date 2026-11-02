"""The cali_proc data contract, confirmed configuration bridge and writers.

The existing processors own all numerical calculations. This module translates
their results into portable analysis packages and imports explicit source maps.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .utils import read_json, write_parquet

CONTRACT = "cali_proc@0.5"
SOURCE_COMMIT = "0d1f620f2573653f36211721aa13e88f0d35fbab"
RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources" / "cali_proc"
CONFIG_NAMES = ("tb_l1", "tb_l2", "ec_l1", "ec_l2")


def is_contract(profile: dict | None) -> bool:
    return (profile or {}).get("analysis_contract") == CONTRACT


def gehrels_error(counts):
    """The confirmed 14B/15B count uncertainty, before any normalization."""
    counts = np.asarray(counts, dtype=float)
    return np.where(counts >= 5, np.sqrt(np.maximum(counts, 0)),
                    1 + np.sqrt(np.maximum(counts + .75, 0)))


def config_bundle(payload: str) -> dict:
    """Return editable copies of the four confirmed configs and file table."""
    import yaml

    payload = str(payload).upper().removeprefix("GRID-")
    if payload not in ("14B", "15B"):
        raise ValueError("Confirmed cali_proc configurations are available for 14B and 15B")
    root = RESOURCE_ROOT / payload
    configs = {name: yaml.safe_load((root / f"{name}.yaml").read_text(encoding="utf-8"))
               for name in CONFIG_NAMES}
    with (root / "exp_files.csv").open(encoding="utf-8", newline="") as stream:
        files = list(csv.DictReader(stream))
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(root.iterdir()) if path.is_file()}
    return {"payload": payload, "source_commit": SOURCE_COMMIT,
            "configs": configs, "files": files, "sha256": hashes}


def _stem(value: str) -> str:
    return str(value).removesuffix(".cut").removesuffix(".event.dat").removesuffix(".dat")


def build_acquisition_profile(
    payload: str, raw_root: Path | str, *, source_payload: str | None = None,
    base_profile: dict | None = None, verify_files: bool = True,
) -> dict:
    """Use a confirmed file table, never infer a payload from a filename style.

    ``source_payload`` selects the source table explicitly when older workspace
    labels differ. The mapping is stored in the preset and travels with runs.
    """
    from ..workbench.acquisition import default_profile

    source_payload = source_payload or payload
    bundle = config_bundle(source_payload)
    cfg = bundle["configs"]
    profile = copy.deepcopy(base_profile) if base_profile is not None else default_profile(payload)
    profile.update(analysis_contract=CONTRACT, payload=payload)
    profile["source_mapping"] = {
        "output_payload": payload, "source_payload": bundle["payload"],
        "raw_root": str(raw_root), "source_commit": SOURCE_COMMIT,
        "config_sha256": bundle["sha256"],
    }
    profile["analysis_config"] = copy.deepcopy(cfg)
    profile["parser"].update(event_tag="grid1x_ft_packet", hk_tag="hk_packet",
                              event_multi_evt=38, event_multi_step=14)
    profile["parser"]["transform"].update(hk_selection="stable_plateau", stable_bias_tolerance=.3)
    profile["parser"]["clock"] = {"event_counter": "timestamp1", "frequency_hz": 100_000_000.,
                                      "shared": False, "max_internal_gap_s": 10.0}
    profile.setdefault("fitting", {})["tb_histogram"] = {
        "min": 0., "max": float(cfg["tb_l1"]["shared"]["adc_max"]),
        "bin_width": float(cfg["tb_l1"]["shared"]["bin_width"]),
    }
    # The analysis table supplies roles and energies; the file table supplies
    # physical sources. Both references must agree exactly.
    analyses: dict[str, dict] = {}
    for name, entry in cfg["tb_l1"]["measurements"].items():
        for ref in entry.get("files", [name]):
            analyses[ref] = {"kind": "tb", "stem": _stem(name),
                             "bias": float(entry["target_bias_V"]), "analysis_id": str(name)}
    sources = []
    for kind in ("source", "xray"):
        for name, entry in cfg["ec_l1"].get(kind, {}).items():
            primary = entry.get("primary_ids", entry.get("files", []))
            background = entry.get("background_ids", entry.get("background_files", []))
            if kind == "source":
                sources.append({"name": name, "aliases": [], "energies_keV": [float(entry["energy_keV"])]})
            for slot, ref in enumerate(primary):
                item = {"kind": kind, "bias": float(entry.get("selection", {}).get("hk_bias", 28.5)),
                        "analysis_id": str(name), "background_ids": list(background)}
                if kind == "xray":
                    item.update(channel=slot, energy_keV=float(entry["energy_keV"]))
                else:
                    item.update(source=name, peak_energies_keV=[float(entry["energy_keV"])])
                analyses[ref] = item
            if kind == "source":
                for ref in background:
                    analyses.setdefault(ref, {"kind": "background", "source": "bkg", "bias": 28.5})
    rows, missing, unmatched = [], [], []
    for source in bundle["files"]:
        item = analyses.get(source["id"], analyses.get(source["sci"]))
        if item is None:
            unmatched.append(source["id"])
            continue
        row = {**copy.deepcopy(item), "event": source["sci"], "hk": source["hk"],
               "acquisition_id": source["id"], "accept_residual": source.get("accept_residual")}
        if row["kind"] in ("source", "background"):
            row["stem"] = f"src_{row['source']}_{_stem(source['id'])}"
            row["exposure_minutes"] = None
        if verify_files:
            for field in ("event", "hk"):
                if not (Path(raw_root) / row[field]).is_file():
                    missing.append(row[field])
        rows.append(row)
    if not rows:
        raise ValueError("Confirmed file table did not match any configured observations")
    if missing:
        raise FileNotFoundError("Confirmed raw/HK files are missing: " + ", ".join(missing[:12]))
    profile["discovery"].update(mode="manifest", manifest=rows, rules=[], sources=sources)
    profile["source_mapping"]["excluded_file_ids"] = unmatched
    return profile


def tb_windows(profile: dict) -> dict:
    """Translate confirmed per-channel windows into the existing editor map."""
    measurements = profile.get("analysis_config", {}).get("tb_l1", {}).get("measurements", {})
    result = {}
    for row in profile.get("discovery", {}).get("manifest", []):
        if row.get("kind") != "tb":
            continue
        entry = measurements.get(str(row.get("analysis_id")), {})
        stem = row.get("stem") or _stem(row["acquisition_id"])
        result[stem] = {str(ch): {"lo": float(window[0]), "hi": float(window[1]), "manual": True}
                        for ch, window in enumerate(entry.get("fit_range", [])) if window is not None}
    return result


def _json_value(value: Any):
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_value(item) for item in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _figure(path: Path, value: Path | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    elif Path(value).suffix.lower() == path.suffix.lower():
        path.write_bytes(Path(value).read_bytes())
    else:
        import matplotlib.image as mpimg

        mpimg.imsave(path, mpimg.imread(value))


def canonical_point(record: dict, kind: str) -> dict:
    """Translate numerical names and units; retain extra diagnostic fields."""
    point = copy.deepcopy(record)
    point.pop("channel", None)
    for old, new in (("center_err", "center_std"), ("sigma_err", "sigma_std"),
                     ("temp", "temperature_C"), ("temp_err", "temperature_std_C"),
                     ("bias", "bias_V"), ("bias_err", "bias_std_V"),
                     ("E", "energy_keV"), ("resolution_err", "resolution_std")):
        if new not in point and old in point:
            point[new] = point[old]
    if "window" not in point:
        lo, hi = point.get("window_lo", point.get("lo")), point.get("window_hi", point.get("hi"))
        point["window"] = [lo, hi] if lo is not None and hi is not None else None
    if "background" not in point:
        point["background"] = {"lin + gaus": "lin", "exp + gaus": "exp", "quad + gaus": "quad",
                               "gaus + gaus": "gaus"}.get(point.get("fit_mode"))
    if "background_params" not in point and point.get("center") is not None:
        center = point["center"]
        fitted = point.get("model_parameters", {})
        if point["background"] == "lin":
            slope = point.get("slope", fitted.get("b_slope", 0.))
            intercept = point.get("intercept", fitted.get("b_intercept", 0.))
            point["background_params"] = {"b0": intercept + slope * center, "b1": slope}
        elif point["background"] == "quad":
            a = point.get("quad_a", fitted.get("b_a", 0.))
            b = point.get("quad_b", fitted.get("b_b", 0.))
            c = point.get("quad_c", fitted.get("b_c", 0.))
            point["background_params"] = {"b0": a * center**2 + b * center + c, "b1": 2 * a * center + b, "b2": a}
        elif point["background"] == "exp":
            decay = point.get("exp_decay", fitted.get("e_decay", 0.))
            if decay > 0:
                amplitude = point.get("exp_amplitude", fitted.get("e_amplitude", 0.))
                point["background_params"] = {"b0": amplitude * np.exp(-point["window"][0] / decay),
                                              "k": 1. / decay}
        elif point["background"] == "gaus" and fitted.get("bk_sigma", 0.) > 0:
            sigma = fitted["bk_sigma"]
            point["background_params"] = {
                "bg_amplitude": fitted["bk_amplitude"] / (sigma * np.sqrt(2 * np.pi)),
                "bg_center": fitted["bk_center"], "bg_sigma": sigma,
            }
    if point.get("resolution_unit") == "percent":
        for key in ("resolution", "resolution_std"):
            if point.get(key) is not None:
                point[key] /= 100.
    point["resolution_unit"] = "fraction"
    if point.get("amplitude_kind", "area") == "area" and (point.get("sigma") or 0) > 0:
        sigma = float(point["sigma"])
        divisor = sigma * np.sqrt(2 * np.pi)
        area = float(point.get("amplitude", 0))
        point["area"] = area
        point["area_std"] = point.get("amplitude_std", point.get("amplitude_err"))
        point["amplitude"] = point.get("height", area / divisor)
        if point.get("height_std") is not None:
            point["amplitude_std"] = point["height_std"]
        elif point["area_std"] is not None:
            # Missing area/sigma covariance is recorded, never called exact.
            point["amplitude_std"] = float(point["area_std"]) / divisor
            point["amplitude_uncertainty_basis"] = "area error only; area-sigma covariance unavailable"
    point["amplitude_kind"] = "height"
    quality = point.get("fit_quality", {})
    point.setdefault("redchi", quality.get("reduced_chi_square", quality.get("redchi")))
    point.setdefault("qa_status", "warning" if quality.get("suspect") else "ok")
    point.setdefault("status", "unconfirmed" if point.get("placeholder") else "fitted")
    if point["status"] == "unconfirmed" or point.get("placeholder"):
        for key in ("window", "center", "center_std", "sigma", "sigma_std", "redchi"):
            point[key] = None
        point.setdefault("reason", "no_confirmed_window")
    return _json_value(point)


def publish_l1(
    product_root: Path, payload: str, kind: str, analysis_id: str,
    records: list[dict] | dict, spectra: pd.DataFrame, meta: dict,
    fit_figure: Path | bytes | None = None,
    selection_figures: dict[str, Path | bytes] | None = None,
) -> dict:
    """Publish/merge one canonical analysis, retaining untouched channels."""
    if kind not in ("tb", "ec") or Path(analysis_id).name != analysis_id:
        raise ValueError("Invalid analysis kind or identity")
    node = Path(product_root) / payload / kind / "L1" / analysis_id
    node.mkdir(parents=True, exist_ok=True)
    points = read_json(node / "points.json") if (node / "points.json").exists() else {}
    iterable = list(records.items()) if isinstance(records, dict) else [(row["channel"], row) for row in records]
    updated_channels = sorted(int(ch) for ch, _ in iterable)
    for ch, row in iterable:
        points[str(ch)] = canonical_point(row, kind)
    if (node / "spectra.parquet").exists() and len(spectra):
        previous = pd.read_parquet(node / "spectra.parquet")
        previous = previous.loc[~previous["channel"].isin(spectra["channel"].unique())]
        spectra = pd.concat([previous, spectra], ignore_index=True)
    evidence = {**meta, "schema": f"g{payload.lower()}-{kind}-l1@1", "payload": payload,
                "analysis_contract": CONTRACT, "analysis_id": analysis_id,
                "channels": sorted(int(ch) for ch in points)}
    if (node / "meta.json").exists():
        old = read_json(node / "meta.json")
        for key in ("roles", "corr", "background_corr", "history", "time", "l0_sources"):
            if isinstance(old.get(key), dict) and isinstance(evidence.get(key), dict):
                evidence[key] = _merge_evidence(old[key], evidence[key])
        evidence["channel_evidence"] = copy.deepcopy(old.get("channel_evidence", {}))
        evidence["revisions"] = [*old.get("revisions", []),
                                 {"channels": updated_channels,
                                  "previous_meta_sha256": hashlib.sha256((node / "meta.json").read_bytes()).hexdigest()}]
    evidence.setdefault("channel_evidence", {}).update({str(ch): copy.deepcopy(meta) for ch in updated_channels})
    _write_json(node / "points.json", points)
    write_parquet(node / "spectra.parquet", spectra)
    _write_json(node / "meta.json", evidence)
    if fit_figure is not None:
        _figure(node / "fit.png", fit_figure)
    for name, value in (selection_figures or {}).items():
        if Path(name).name != name or not name.startswith("selection") or not name.endswith(".png"):
            raise ValueError("Selection evidence must use selection*.png names")
        _figure(node / name, value)
    return {"node": str(node), "meta": str(node / "meta.json"), "points": str(node / "points.json"),
            "spectra": str(node / "spectra.parquet"), "figures": [str(path) for path in sorted(node.glob("*.png"))]}


def _merge_evidence(previous: dict, changed: dict) -> dict:
    merged = copy.deepcopy(previous)
    for key, value in changed.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_evidence(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def publish_l2(
    product_root: Path, payload: str, kind: str, models: list[dict | None], meta: dict,
    figures: dict[str, Path | bytes] | None = None, timestamp: str | None = None,
) -> dict:
    """Write a timestamped model package without reusing a mutable pointer."""
    stamp = timestamp or datetime.now().strftime("%y%m%d%H%M")
    root = Path(product_root) / payload / kind / "L2"
    node = root / stamp
    if node.exists() and timestamp is None:
        stamp = datetime.now().strftime("%y%m%d%H%M%S%f")
        node = root / stamp
    node.mkdir(parents=True, exist_ok=False)
    missing_models = any(row is None or (kind == "ec" and any(row.get(key) is None for key in ("EC_low", "EC_high")))
                         for row in models)
    evidence = {**meta, "schema": f"g{payload.lower()}-{kind}-l2@1", "payload": payload,
                "analysis_contract": CONTRACT, "timestamp": stamp,
                "status": "partial" if missing_models else meta.get("status", "ready")}
    paths = []
    if kind == "tb":
        model_path = node / f"{stamp}_temp_bias_fit.json"
        _write_json(model_path, models)
        evidence["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
        meta_path = node / f"{stamp}_temp_bias_fit.meta.json"
        paths.append(model_path)
    elif kind == "ec":
        hashes = {}
        for row in models:
            if row is None:
                continue
            path = node / f"{stamp}_ec_coef_sci_ch{row['channel']}.json"
            _write_json(path, row)
            paths.append(path)
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        evidence["model_sha256"] = hashes
        meta_path = node / f"{stamp}_ec_fit.meta.json"
    else:
        raise ValueError("Model kind must be tb or ec")
    _write_json(meta_path, evidence)
    for name, value in (figures or {}).items():
        if Path(name).name != name:
            raise ValueError("Figure names must be plain filenames")
        _figure(node / name, value)
    return {"node": str(node), "timestamp": stamp, "meta": str(meta_path),
            "model_paths": [str(path) for path in paths],
            "figures": [str(path) for path in sorted(node.glob("*.png"))]}


def validate_l1(node: Path) -> dict:
    """Validate the minimum shared L1 artifact contract, not fit quality."""
    node = Path(node)
    errors = []
    for name in ("meta.json", "points.json", "spectra.parquet", "fit.png"):
        if not (node / name).is_file():
            errors.append(f"Missing {name}")
    if not list(node.glob("selection*.png")):
        errors.append("Missing selection evidence")
    if errors:
        return {"valid": False, "errors": errors}
    meta, points = read_json(node / "meta.json"), read_json(node / "points.json")
    if not str(meta.get("schema", "")).endswith("-l1@1"):
        errors.append("Invalid L1 schema")
    if not isinstance(points, dict):
        errors.append("points.json must be keyed by physical channel")
    else:
        for ch, row in points.items():
            if not ch.isdecimal():
                errors.append(f"Invalid channel {ch}")
            if row.get("status") == "unconfirmed":
                if row.get("window") is not None or not row.get("reason"):
                    errors.append(f"CH{ch}: unconfirmed window needs null and reason")
            elif any(row.get(key) is None or not isinstance(row[key], (float, int)) or not math.isfinite(row[key])
                     for key in ("center", "center_std", "sigma", "sigma_std", "redchi")):
                errors.append(f"CH{ch}: fitted result missing finite peak parameters")
    columns = set(pd.read_parquet(node / "spectra.parquet").columns)
    if not {"channel", "x", "bin_left", "bin_right", "density", "density_err"} <= columns:
        errors.append("Spectrum density or bin columns are missing")
    return {"valid": not errors, "errors": errors, "schema": meta.get("schema")}


def figure_bytes(figure) -> bytes:
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=130)
    return buffer.getvalue()
