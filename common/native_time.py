"""Native FT584 evidence, explicit condition selection and input publication."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .utils import read_parquet, write_json, write_parquet

CONTRACT = "cali_proc@0.5"
FREQUENCY_HZ = 100_000_000.0


def read_analysis_events(path: Path) -> pd.DataFrame:
    """Project fit/correction columns; complete engineering events remain in L0."""
    import pyarrow.parquet as pq

    names = pq.read_schema(path).names
    columns = [name for name in ("channel", "amp", "amp_corr", "crc_check", "utc", "timestamp1", "source_idx",
                                 "temp", "bias") if name in names]
    return pq.read_table(path, columns=columns).to_pandas()


def event_span(frame: pd.DataFrame, channel: int | None = None, *, require_ec: bool = True,
               global_extrema: bool = False) -> dict[str, Any]:
    """Measure one native-order acquisition span without sorting or inventing time."""
    if require_ec and global_extrema:
        raise ValueError("EC exposure requires per-channel native first/last time")
    selected = frame if channel is None else frame.loc[frame["channel"].eq(channel)]
    if "timestamp1" not in selected:
        raise ValueError("缺少原生 timestamp1；请用新规范重新解包 L0")
    raw = selected["timestamp1"].to_numpy()
    if raw.size < 2:
        raise ValueError(f"CH{channel}: 原生计数不足，无法计算曝光")
    if not np.issubdtype(raw.dtype, np.integer):
        raise ValueError("timestamp1 必须保留原生整数，不能用浮点或行号替代")
    first, last = int(raw[0]), int(raw[-1])
    monotonic = bool(np.all(raw[1:] >= raw[:-1]))
    forward = raw[1:] >= raw[:-1]
    deltas = raw[1:][forward] - raw[:-1][forward]
    span = (last - first) / FREQUENCY_HZ
    first_last_span = span
    if global_extrema:
        span = (int(raw.max()) - int(raw.min())) / FREQUENCY_HZ
    max_gap = float(deltas.max() / FREQUENCY_HZ) if deltas.size else 0.
    valid = monotonic and span > 0 and max_gap <= 10.0
    record = {
        "counter": "timestamp1", "frequency_hz": FREQUENCY_HZ,
        "first": first, "last": last, "n_events": len(raw),
        "span_s": span if span > 0 else None,
        "monotonic": monotonic, "max_gap_s": max_gap,
        "ec_exposure_eligible": valid, "scope": "acquisition" if channel is None else "physical_channel",
        "basis": "native-order timestamp1 first/last acquisition span; not live time",
    }
    if global_extrema:
        record.update(basis="timestamp1 global min/max acquisition span; not live time",
                      min_counter=int(raw.min()), max_counter=int(raw.max()), first_last_span_s=first_last_span)
    if require_ec and not valid:
        reason = "计数回退/重置" if not monotonic else "非正计数跨度" if span <= 0 else "内部时间间隙超过 10 s"
        raise ValueError(f"CH{channel}: {reason}，无法用该跨度归一化曝光")
    return record


def select_conditions(hk: pd.DataFrame, target_bias: float, tolerance: float = .3) -> dict[str, Any]:
    """Select the confirmed four-channel HK plateau independently of SCI time."""
    if not np.isfinite(target_bias) or not np.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("HK 目标偏压与容差必须有效")
    voltage = np.column_stack([hk[f"bias_ch{channel}"].to_numpy(dtype=float) for channel in range(4)])
    temperature = np.column_stack([hk[f"sipm_temp_ch{channel}"].to_numpy(dtype=float) for channel in range(4)])
    mean_voltage = voltage.mean(axis=1)
    keep = np.isfinite(voltage).all(axis=1) & np.isfinite(temperature).all(axis=1)
    keep &= np.abs(mean_voltage - float(target_bias)) <= tolerance + 1e-12
    keep &= (np.abs(voltage - mean_voltage[:, None]) <= tolerance + 1e-12).all(axis=1)
    if not np.any(keep):
        raise ValueError(f"HK 中没有 {target_bias:g}±{tolerance:g} V 的有效四通道平台")
    channels = {}
    for channel in range(4):
        temp, bias = temperature[keep, channel], voltage[keep, channel]
        channels[str(channel)] = {
            "temp": float(temp.mean()), "temp_err": float(temp.std(ddof=1)) if temp.size > 1 else 0.,
            "bias": float(bias.mean()), "bias_err": float(bias.std(ddof=1)) if bias.size > 1 else 0.,
            "temp_n": int(temp.size), "bias_n": int(bias.size),
            "temp_n_raw": len(hk), "bias_n_raw": len(hk),
        }
    identities = hk["source_idx"].to_numpy() if "source_idx" in hk else np.arange(len(hk))
    return {
        "channels": channels,
        "selection": {
            "method": "four_channel_mean_bias_plateau", "target_bias_V": float(target_bias),
            "tolerance_V": float(tolerance), "selected_source_idx": identities[keep].astype(int).tolist(),
            "input_hk_rows": len(hk), "selected_hk_rows": int(keep.sum()),
            "mapping": "constant", "sci_hk_aligned": False,
        },
    }


def apply_conditions(frame: pd.DataFrame, native_meta: dict, target_bias: float = 28.5,
                     tolerance: float = .3) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply explicitly selected role/platform means at the L0→L1 boundary."""
    hk_path = Path(native_meta["l0_node"]) / "hk.parquet"
    conditions = select_conditions(read_parquet(hk_path), target_bias, tolerance)
    selected = frame.copy()
    for name in ("temp", "bias"):
        selected[name] = selected["channel"].map({int(ch): item[name] for ch, item in conditions["channels"].items()})
    return selected, conditions


def _source(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def publish_native_input(frame: pd.DataFrame, metadata: dict, product_root: Path,
                         payload: str, family: str, stem: str) -> dict[str, Any]:
    """Write faithful EXP and unselected workflow L0 from the single parser pass."""
    tables = frame.attrs.pop("native_tables", None)
    if tables is None:
        return {}
    events, telemetry = tables["events"], tables["telemetry"]
    exp_node = Path(product_root) / payload / "EXP" / tables["group"] / stem
    l0_node = Path(product_root) / payload / family / "L0" / stem
    exp_meta = {
        "schema": "exp-engineering@2", "payload": payload, "analysis_contract": CONTRACT,
        "source": _source(Path(metadata["event_path"])), "hk_source": _source(Path(metadata["hk_path"])),
        "time_axis": {"events": "timestamp1", "telemetry": "utc_time",
                      "tables": {"events": "timestamp1", "telemetry": "utc_time"}, "frequency_hz": FREQUENCY_HZ,
                      "shared": False, "timestamp2_frequency_hz": None},
        "conversion": {"event_counter_hz": FREQUENCY_HZ, "measurement_counter": "timestamp1",
                         "measurement_frequency_hz": FREQUENCY_HZ,
                         "sci_packet_bytes": tables["diagnostics"]["science"]["packet_bytes"],
                         "sci_events_per_packet": metadata["acquisition"]["parser"].get("event_multi_evt", 38),
                         "hk_packet_bytes": tables["diagnostics"]["hk"]["packet_bytes"],
                       "amp": "data_max-data_base/4"},
        "rows": {"events": len(events), "telemetry": len(telemetry)},
        "diagnostics": tables["diagnostics"], "reader": "calibration_lib XML decoder",
        "issues_file": "issues.parquet", "xml": metadata["acquisition"]["xml"],
    }
    write_parquet(exp_node / "events.parquet", events)
    write_parquet(exp_node / "telemetry.parquet", telemetry)
    write_parquet(exp_node / "issues.parquet", tables["issues"])
    write_json(exp_node / "meta.json", exp_meta)
    valid = events["crc_check"].eq(True) & events["channel"].isin(range(4)) & np.isfinite(events["amp"])
    clean_events = events.loc[valid].copy()
    finite_hk = np.isfinite(telemetry[[f"{name}_ch{ch}" for ch in range(4)
                                     for name in ("bias", "sipm_temp")]].to_numpy(dtype=float)).all(axis=1)
    clean_hk = telemetry.loc[telemetry["crc_check"].eq(True) & finite_hk].copy()
    cleaning = {
        "events": {"input_rows": len(events), "bad_crc": int((~events["crc_check"].eq(True)).sum()),
                   "invalid_channel": int((~events["channel"].isin(range(4))).sum()),
                   "nonfinite_amplitude": int((~np.isfinite(events["amp"])).sum()), "kept_rows": len(clean_events)},
        "hk": {"input_rows": len(telemetry), "bad_crc": int((~telemetry["crc_check"].eq(True)).sum()),
               "nonfinite_required_engineering": int((~finite_hk).sum()), "kept_rows": len(clean_hk)},
        "rules": ["CRC-true, physical channel 0..3, finite required engineering",
                  "Finite negative amplitudes retained; no platform/time/role selection in L0"],
    }
    spans = {}
    for channel in range(4):
        try:
            spans[str(channel)] = event_span(clean_events, channel, require_ec=False)
        except ValueError as exc:
            spans[str(channel)] = {"span_s": None, "ec_exposure_eligible": False, "reason": str(exc)}
    native = {"analysis_contract": CONTRACT, "exp_node": str(exp_node.resolve()), "l0_node": str(l0_node.resolve()),
              "time_axis": exp_meta["time_axis"], "native_time": {"by_channel": spans},
              "source": exp_meta["source"], "hk_source": exp_meta["hk_source"], "cleaning": cleaning}
    l0_meta = {**native, "schema": f"g{payload.lower()}-l0@1", "payload": payload,
               "columns": {"events": list(clean_events), "hk": list(clean_hk)},
               "rows": {"events": len(clean_events), "hk": len(clean_hk)}}
    write_parquet(l0_node / "events.parquet", clean_events)
    write_parquet(l0_node / "hk.parquet", clean_hk)
    write_json(l0_node / "meta.json", l0_meta)
    metadata["native"] = native
    metadata["analysis_contract"] = CONTRACT
    metadata["acquisition"].update(analysis_contract=CONTRACT, native=native,
                                   time_axis=native["time_axis"], native_time=native["native_time"])
    return native
