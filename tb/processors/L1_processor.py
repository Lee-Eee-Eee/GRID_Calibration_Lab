"""温度偏压(TB)标定 - L1层处理器。"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from lmfit.models import ExponentialModel, GaussianModel, LinearModel, QuadraticModel
from scipy.signal import savgol_filter

from ...common.cali_contract import CONTRACT, figure_bytes, gehrels_error, publish_l1
from ...common.peak_finding import (
    PeakDetectionError,
    detect_photopeak_for_fit,
    fit_quality,
    propose_photopeaks,
)
from ...common.spectral_models import pending_peak_record, unsupported_spectral_model
from ...common.tb_peak_context import gain_guides
from ...common.utils import (
    DataLayout,
    get_timestamp,
    read_json,
    validated_events,
    write_json,
)

# 单峰高斯 + 单一背景（与原古项目 calibration_process 对齐）
FIT_MODE_GAUS = "gaus"              # 无背景
FIT_MODE_LIN_GAUS = "lin + gaus"    # 线性背景
FIT_MODE_EXP_GAUS = "exp + gaus"    # 指数背景
FIT_MODE_QUAD_GAUS = "quad + gaus"  # 二次背景


@dataclass
class FitResult:
    hist: np.ndarray
    mids: np.ndarray
    hist_smooth: np.ndarray
    x_cut: np.ndarray
    y_cut: np.ndarray
    best_fit: np.ndarray
    gauss_fit: np.ndarray
    background_fit: np.ndarray
    residual: np.ndarray
    center: float
    center_err: float
    sigma: float
    sigma_err: float
    amplitude: float
    amplitude_err: float
    slope: float | None
    intercept: float | None
    exp_amplitude: float | None
    exp_decay: float | None
    quad_a: float | None
    quad_b: float | None
    quad_c: float | None
    fwhm: float
    height: float
    fit_mode: str
    lo: float
    hi: float
    rsquared: float
    peak_detection: dict[str, Any]
    fit_quality: dict[str, Any]
    histogram_config: dict[str, float]
    center_sigma_covariance: float = 0.0
    height_std: float = 0.0
    redchi: float = 0.0
    ndf: int = 0
    ordinate: str = "counts/bin"


class TBL1Processor:
    """TB L1层处理器 - 兼容旧流程拟合与绘图。"""

    def __init__(self, payload_name: str, product_root: Path, *, response_payload: str | None = None,
                 analysis_contract: str | None = None):
        self.payload_name = payload_name
        self.response_payload = response_payload or payload_name
        self.layout = DataLayout(product_root, payload_name)
        self._native_fit = analysis_contract == CONTRACT
        self._active_bin_width = 1.0

    def process(
        self,
        tb_l0_dir: Path,
        config: dict[str, Any] | None = None,
        only_stems: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """处理 TB L1。

        参数:
            tb_l0_dir: L0 输出根目录（含 parquet/ 子目录）
            config: TB_PROCESS_PARAMS dict
            only_stems: 若给定，只重跑这些 stem 对应的 parquet（用于改完手选窗
                后单点重画）。其他 stem 的 L1 输出保持不变；不会覆盖 manifest /
                fit_windows_used 全局快照。
        """
        cfg = config or {}
        auto_window = bool(cfg.get("auto_window", True))

        win_path = cfg.get("fit_windows_config")
        if win_path is None:
            default_path = Path(__file__).resolve().parents[2] / "resources" / f"legacy_fit_windows_config_{self.payload_name}.json"
            win_path = str(default_path) if default_path.exists() else None

        manual_windows = self._load_windows(Path(win_path)) if win_path else {}
        used_windows: dict[str, dict[str, dict[str, Any]]] = {}

        results: dict[str, Any] = {
            "payload": self.payload_name,
            "layer": "TB_L1",
            "timestamp": get_timestamp(),
            "auto_window": auto_window,
            "automatic_window": bool(cfg.get("automatic_window")),
            "peak_detection_config": cfg.get("peak_detection") or {},
            "fit_windows_config": str(win_path) if win_path else None,
            "n_processed": 0,
            "outputs": [],
            "errors": [],
        }

        parquet_dir = tb_l0_dir / "parquet"
        if not parquet_dir.exists():
            raise ValueError(f"L0 parquet directory not found: {parquet_dir}")

        only_set = {s.strip().removesuffix(".l0") for s in only_stems if s and s.strip()} if only_stems else None
        partial_run = only_set is not None

        all_parquets = sorted(parquet_dir.glob("*.parquet"))
        if partial_run:
            parquet_files = [p for p in all_parquets if p.stem.removesuffix(".l0") in only_set]
            missing = only_set - {p.stem.removesuffix(".l0") for p in parquet_files}
            if missing:
                print(f"⚠ 以下 stem 在 L0 parquet 中找不到: {sorted(missing)}")
            if not parquet_files:
                raise ValueError(f"No matching parquet for stems={sorted(only_set)}")
            print(f"[partial] L1 only re-running {len(parquet_files)} stem(s): "
                  f"{[p.stem for p in parquet_files]}")
        else:
            parquet_files = all_parquets

        selected_channels = cfg.get("fit_channels", [0, 1, 2, 3])
        if (not isinstance(selected_channels, (list, tuple)) or not selected_channels
                or any(type(ch) is not int or ch not in range(4) for ch in selected_channels)):
            raise ValueError("fit_channels must contain channel numbers 0–3")
        selected_channels = sorted(set(selected_channels))
        skipped = [ch for ch in selected_channels if unsupported_spectral_model(self.response_payload, ch)]
        results["skipped_channels"] = [{"channel": ch, "reason": unsupported_spectral_model(self.response_payload, ch)}
                                       for ch in skipped]
        selected_channels = [ch for ch in selected_channels if ch not in skipped]
        if not selected_channels:
            raise ValueError(unsupported_spectral_model(self.response_payload, skipped[0]))
        results["fit_channels"] = selected_channels
        guides = {}
        if (not cfg.get("peak_detection", {}).get("expected_center")
                and (cfg.get("automatic_window") or not manual_windows)):
            context_paths = cfg.get("peak_context_l0_paths", all_parquets)
            guide_channels = [ch for ch in range(4) if not unsupported_spectral_model(self.response_payload, ch)]
            guides = self._learn_peak_guides([Path(path) for path in context_paths], cfg, guide_channels)
            results["peak_context"] = str(self.layout.get_tb_l1_json("auto_peak_context.json")) if guides else None
        results["n_files"] = len(parquet_files)
        results["file_outcomes"] = []
        results["channels_fitted"] = 0
        results["channels_failed"] = 0
        results["channels_accepted"] = 0
        results["channels_rejected"] = 0
        results["channels_suspect"] = 0
        results["quality_policy"] = "warning_only"
        for file_index, parquet_file in enumerate(parquet_files, 1):
            started = perf_counter()
            stem = parquet_file.stem.removesuffix(".l0")
            fig = None
            updated = []
            fits = {}
            unconfirmed = []
            failures = []
            try:
                # Spectral fitting needs amplitude, channel and CRC only. Read
                # one footer for both metadata fields instead of once per channel.
                import pyarrow.parquet as pq

                parquet = pq.ParquetFile(parquet_file)
                columns = [name for name in ("channel", "amp", "crc_check", "timestamp1")
                           if name in parquet.schema_arrow.names]
                df, input_quality = validated_events(parquet.read(columns=columns).to_pandas())
                metadata = parquet.schema_arrow.metadata or {}
                meta = json.loads(metadata.get(b"hk_data", b"{}"))
                acquisition = json.loads(metadata.get(b"acquisition", b"{}"))
                native = json.loads(metadata.get(b"native", b"{}"))
                self._native_fit = (cfg.get("analysis_contract") == CONTRACT or
                                    acquisition.get("analysis_contract") == CONTRACT or
                                    native.get("analysis_contract") == CONTRACT)
                native_selection = None
                if self._native_fit:
                    from ...common.legacy_parser import to_hk_data
                    from ...common.native_time import apply_conditions, event_span

                    bundle = cfg.get("payload_contract_config", {})
                    native_config = bundle.get("tb_l1", {})
                    shared = native_config.get("shared", {})
                    measurement = native_config.get("measurements", {}).get(stem, {})
                    observation = acquisition.get("observation", {})
                    target_bias = cfg.get("target_bias_V", measurement.get("target_bias_V", observation.get("bias")))
                    if target_bias is None:
                        raise ValueError("TB L1 needs an explicit target_bias_V for HK plateau selection")
                    tolerance = shared.get("conditions", shared.get("stable", {})).get("tolerance_V", .3)
                    df, native_selection = apply_conditions(df, native, float(target_bias), float(tolerance))
                    meta = to_hk_data(native_selection["channels"])
                    # The 15B reference uses the full SCI extrema; 14B uses
                    # native first/last. Both retain clock-order diagnostics.
                    extrema = shared.get("clock", {}).get("tb_global_extrema",
                                                         native_config.get("payload", self.response_payload) == "15B")
                    native_selection["exposure"] = event_span(df, require_ec=False, global_extrema=extrema)
                    self._native_background = measurement.get("background", shared.get("background", "auto"))
                    self._native_max_nfev = int(shared.get("fit", {}).get("max_nfev", 100000))
                histogram = cfg.get("histogram_config") or acquisition.get("fitting", {}).get("tb_histogram")
                if self._native_fit and not histogram:
                    histogram = {"min": 0., "max": 16384., "bin_width": 4.}
                windows = {} if cfg.get("automatic_window") else manual_windows
                json_path = self.layout.get_tb_l1_json(f"{stem}_fit_results.l1.json")
                existing = read_json(json_path) if json_path.exists() else []
                records = {row["channel"]: row for row in existing if isinstance(row, dict) and "channel" in row
                           and not unsupported_spectral_model(self.response_payload, row["channel"])}
                fits = {}
                used_windows.setdefault(stem, {})
                amplitudes = df["amp"].to_numpy(dtype=float)
                channels = df["channel"].to_numpy()
                for ch in selected_channels:
                    try:
                        amp_data = amplitudes[channels == ch]
                        if len(amp_data) < 10:
                            raise ValueError(f"Only {len(amp_data)} valid events; at least 10 are required")
                        if (self._native_fit and not cfg.get("automatic_window") and stem in windows
                                and str(ch) not in windows[stem]):
                            records[ch] = {
                                "channel": ch, "status": "unconfirmed", "reason": "no_confirmed_window",
                                "center": None, "center_err": None, "sigma": None, "sigma_err": None,
                                "window": None, "window_lo": None, "window_hi": None,
                                "fit_performed": False, "n_events": len(amp_data),
                                "temp": meta["temp"][ch], "temp_err": meta["temp_err"][ch],
                                "bias": meta["bias"][ch], "bias_err": meta["bias_err"][ch],
                                "analysis_contract": CONTRACT,
                                "exposure_s": native_selection["exposure"]["span_s"],
                                "exposure_basis": native_selection["exposure"]["basis"],
                                "hk_samples": native_selection["channels"][str(ch)]["temp_n"],
                                "selection": native_selection["selection"],
                            }
                            unconfirmed.append(ch)
                            updated.append(ch)
                            continue
                        settings = dict(cfg.get("peak_detection") or {})
                        guide = guides.get(stem, {}).get(str(ch)) if not self._is_manual_entry(windows.get(stem, {}).get(str(ch), {})) else None
                        if guide:
                            settings.update({key: guide[key] for key in ("expected_center", "expected_tolerance")})
                        fit_res = self._fit_channel(stem, ch, amp_data, windows, auto_window,
                                                    settings, histogram)
                        if guide:
                            fit_res.peak_detection["gain_guide"] = guide
                        is_manual = self._is_manual_entry(windows.get(stem, {}).get(str(ch), {}))
                        record = self._fit_record(ch, fit_res, meta, is_manual, len(amp_data), input_quality)
                        record["acquisition_profile_sha256"] = acquisition.get("profile_sha256")
                        if native_selection:
                            record.update(analysis_contract=CONTRACT,
                                          hk_samples=native_selection["channels"][str(ch)]["temp_n"],
                                          exposure_s=native_selection["exposure"]["span_s"],
                                          exposure_basis=native_selection["exposure"]["basis"],
                                          selection=native_selection["selection"])
                        records[ch] = record
                        fits[ch] = fit_res
                        results["channels_accepted" if fit_res.fit_quality["accepted"] else "channels_rejected"] += 1
                        results["channels_suspect"] += int(not fit_res.fit_quality["accepted"])
                        updated.append(ch)
                        used_windows[stem][str(ch)] = {
                            "lo": float(fit_res.lo), "hi": float(fit_res.hi), "manual": is_manual,
                        }
                    except Exception as exc:
                        failure = {"file": parquet_file.name, "stem": stem, "channel": ch,
                                   "error": str(exc), "peak_detection": getattr(exc, "diagnostics", None)}
                        failures.append(failure)
                        results["errors"].append(failure)
                if updated:
                    for deferred in range(4):
                        if not unsupported_spectral_model(self.response_payload, deferred):
                            continue
                        empty = pending_peak_record(self.response_payload, deferred)
                        for key in ("temp", "temp_err", "bias", "bias_err"):
                            values = meta.get(key, [])
                            if len(values) > deferred:
                                empty[key] = float(values[deferred])
                        empty.update(window_lo=float((histogram or {}).get("min", 0.)),
                                     window_hi=float((histogram or {}).get("max", 65536.)),
                                     n_events=int(np.count_nonzero(channels == deferred)),
                                     window_manual=False, input_quality=input_quality,
                                     acquisition_profile_sha256=acquisition.get("profile_sha256"))
                        records[deferred] = empty
                    # A channel edit preserves the other saved channel records.
                    # Failed requests retain their previous fit, while errors
                    # and channels_updated explicitly describe this attempt.
                    write_json(json_path, [records[ch] for ch in sorted(records)])
                    if len(selected_channels) == 1:
                        fig, ax = plt.subplots(figsize=(8, 5))
                        axes = {selected_channels[0]: ax}
                        figure_name = f"{stem}_ch{selected_channels[0]}_fit.l1.jpg"
                    else:
                        fig, grid = plt.subplots(2, 2, figsize=(13, 9))
                        axes = dict(enumerate(grid.flat))
                        figure_name = f"{stem}_fit.l1.jpg"
                    for ch, ax in axes.items():
                        if ch in fits:
                            self._plot_channel(ax, ch, fits[ch])
                        elif ch in unconfirmed:
                            width = histogram["bin_width"]
                            counts, edges = np.histogram(amplitudes[channels == ch], bins=np.arange(
                                histogram["min"], histogram["max"] + width, width))
                            ax.step((edges[:-1] + edges[1:]) / 2, counts / width, where="mid")
                            ax.set_title(f"CH{ch}: no confirmed window")
                            ax.set_ylabel("Counts/ADC")
                        else:
                            label = (f"CH{ch}: Compton response, model pending" if unsupported_spectral_model(self.response_payload, ch)
                                     else f"CH{ch}: fit failed" if ch in selected_channels else f"CH{ch}: unchanged")
                            ax.text(0.5, 0.5, label,
                                    ha="center", va="center", transform=ax.transAxes)
                    label = (f"Background: {getattr(self, '_native_background', 'auto')}" if self._native_fit
                             else "Fit mode: auto-select")
                    fig.suptitle(f"{stem} | {label}", fontsize=14)
                    # Fixed padding avoids constrained-layout's repeated text
                    # measurement on every draw during an interactive refit.
                    fig.subplots_adjust(left=0.08, right=0.98, bottom=0.10, top=0.88, hspace=0.42, wspace=0.25)
                    fig_path = self.layout.get_tb_l1_figure(figure_name)
                    fig.savefig(fig_path, dpi=150, format="jpg")
                    canonical = None
                    if self._native_fit:
                        import pandas as pd

                        spectra = []
                        for channel, fit in fits.items():
                            width = fit.histogram_config["bin_width"]
                            counts = fit.hist * width
                            spectra.append(pd.DataFrame({
                                "channel": channel, "x": fit.mids,
                                "bin_left": fit.mids - width / 2, "bin_right": fit.mids + width / 2,
                                "counts": counts, "err": gehrels_error(counts),
                                "density": fit.hist, "density_err": gehrels_error(counts) / width,
                            }))
                        for channel in unconfirmed:
                            width = histogram["bin_width"]
                            counts, edges = np.histogram(amplitudes[channels == channel], bins=np.arange(
                                histogram["min"], histogram["max"] + width, width))
                            spectra.append(pd.DataFrame({
                                "channel": channel, "x": (edges[:-1] + edges[1:]) / 2,
                                "bin_left": edges[:-1], "bin_right": edges[1:],
                                "counts": counts, "err": gehrels_error(counts),
                                "density": counts / width, "density_err": gehrels_error(counts) / width,
                            }))
                        canonical = publish_l1(
                            self.layout.product_root, self.payload_name, "tb", stem,
                            [records[ch] for ch in updated], pd.concat(spectra, ignore_index=True),
                            {"l0_source": native["l0_node"], "source": native.get("source"),
                             "hk_source": native.get("hk_source"), "history": [native_selection],
                             "conditions": native_selection, "time": native.get("time_axis"),
                             "fit": {"histogram": histogram, "scale_covar": False,
                                     "weights": "1/(Gehrels(counts)/bin_width)", "ordinate": "Counts/ADC"},
                             "config": cfg, "internal_layer": "TB_L1"},
                            fit_figure=figure_bytes(fig),
                            selection_figures={"selection.png": self._selection_figure(native, native_selection)},
                        )
                    results["outputs"].append({
                        "stem": stem, "channels_fitted": sum(row.get("center") is not None and not row.get("placeholder", False) for row in records.values()), "channels_updated": updated,
                        "channels_unconfirmed": unconfirmed,
                        "channels_failed": [failure["channel"] for failure in failures],
                        "json": json_path.name, "figure": fig_path.name, "input_quality": input_quality,
                        "canonical": canonical,
                    })
                    results["n_processed"] += 1
            except Exception as exc:
                failure = {"file": parquet_file.name, "stem": stem, "channel": None,
                           "error": str(exc), "peak_detection": getattr(exc, "diagnostics", None)}
                failures.append(failure)
                results["errors"].append(failure)
            finally:
                if fig is not None:
                    plt.close(fig)
            results["channels_fitted"] += len(fits)
            results["channels_failed"] += len(selected_channels) - len(updated)
            outcome = {"stem": stem, "channels_updated": updated,
                       "channels_failed": [failure["channel"] for failure in failures],
                       "errors": failures, "elapsed_seconds": round(perf_counter() - started, 3)}
            results["file_outcomes"].append(outcome)
            print("L1_PROGRESS " + json.dumps({
                "completed": file_index, "total": len(parquet_files), "stem": stem,
                "fitted": results["channels_fitted"], "failed": results["channels_failed"],
            }), flush=True)
            print(f"[L1 {file_index}/{len(parquet_files)}] {stem}: "
                  f"fitted {len(updated)}/{len(selected_channels)} channels, "
                  f"failed {len(failures)} ({outcome['elapsed_seconds']:.2f}s)", flush=True)
            for failure in failures:
                print(f"  CH{failure['channel']}: {failure['error']}", flush=True)

        # partial run 时只合并更新本次涉及的 stem，避免清掉之前其他文件的快照/清单
        snap_path = self.layout.get_tb_l1_json("fit_windows_used.json")
        manifest_path = self.layout.get_tb_l1_json("L1_manifest.json")
        if partial_run:
            existing_snap = self._load_windows(snap_path) if snap_path.exists() else {}
            for stem, channels in used_windows.items():
                existing_snap.setdefault(stem, {}).update(channels)
            write_json(snap_path, existing_snap)

            existing_manifest = read_json(manifest_path) if manifest_path.exists() else {}
            if isinstance(existing_manifest, dict):
                merged_outputs = {o["stem"]: o for o in existing_manifest.get("outputs", []) if isinstance(o, dict)}
                for o in results["outputs"]:
                    merged_outputs[o["stem"]] = o
                existing_manifest["outputs"] = list(merged_outputs.values())
                existing_manifest["last_partial_run"] = {
                    "timestamp": results["timestamp"],
                    "stems": sorted(only_set or []),
                    "n_processed": results["n_processed"],
                    "errors": results.get("errors", []),
                }
                write_json(manifest_path, existing_manifest)
            else:
                write_json(manifest_path, results)
            results["partial_run_stems"] = sorted(only_set or [])
        else:
            write_json(snap_path, used_windows)
            write_json(manifest_path, results)
        return results

    def _learn_peak_guides(self, paths, config, channels):
        """Cache an independent L0-series search guide in this output run."""
        if len(paths) < 16:
            return {}
        import pyarrow.parquet as pq

        from ...common.peak_finding import ALGORITHM_SHA256
        from ...workbench.acquisition import tb_histogram_config

        unique = {}
        for path in paths:
            unique.setdefault(path.stem.removesuffix(".l0"), path)
        paths = [unique[stem] for stem in sorted(unique)]
        inputs = []
        skipped_inputs = []
        for path in paths:
            try:
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                inputs.append({"stem": path.stem.removesuffix(".l0"), "size": path.stat().st_size,
                               "sha256": digest.hexdigest()})
            except OSError as exc:
                inputs.append({"stem": path.stem.removesuffix(".l0"), "unavailable": type(exc).__name__})
        identity = {"inputs": inputs, "settings": config.get("peak_detection") or {},
                    "histogram": config.get("histogram_config"), "channels": channels,
                    "search_sha256": ALGORITHM_SHA256,
                    "context_sha256": hashlib.sha256(Path(__file__).resolve().parents[2].joinpath(
                        "common/tb_peak_context.py").read_bytes()).hexdigest()}
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        output = self.layout.get_tb_l1_json("auto_peak_context.json")
        try:
            cached = read_json(output) if output.exists() else {}
        except (OSError, ValueError):
            cached = {}
        if cached.get("signature") == signature:
            return cached["guides"]
        spectra, temperatures, biases, stems = [], [], [], []
        reference_profile, histogram = None, None
        for path in paths:
            try:
                parquet = pq.ParquetFile(path)
                metadata = parquet.schema_arrow.metadata or {}
                hk = json.loads(metadata.get(b"hk_data", b"{}"))
                acquisition = json.loads(metadata.get(b"acquisition", b"{}"))
                profile = acquisition.get("profile_sha256")
                if len(hk.get("temp", [])) != 4 or len(hk.get("bias", [])) != 4:
                    continue
                if histogram is None:
                    reference_profile = profile
                    histogram = config.get("histogram_config") or tb_histogram_config(acquisition)
                    edges = np.arange(histogram["min"], histogram["max"] + histogram["bin_width"], histogram["bin_width"])
                if profile != reference_profile:
                    continue
                columns = [name for name in ("amp", "channel", "crc_check") if name in parquet.schema_arrow.names]
                frame, _ = validated_events(parquet.read(columns=columns).to_pandas())
                amplitude, channel = frame["amp"].to_numpy(), frame["channel"].to_numpy()
                spectra.append([np.histogram(amplitude[channel == ch], edges)[0] for ch in range(4)])
                temperatures.append(hk["temp"])
                biases.append(hk["bias"])
                stems.append(path.stem.removesuffix(".l0"))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                skipped_inputs.append({"stem": path.stem.removesuffix(".l0"), "error": str(exc)})
        guides = {stem: {} for stem in stems}
        if spectra:
            spectra, temperatures, biases = map(np.asarray, (spectra, temperatures, biases))
            x = (edges[1:] + edges[:-1]) / 2
            for ch in channels:
                for stem, guide in zip(stems, gain_guides(x, spectra[:, ch], temperatures[:, ch], biases[:, ch],
                                                         config.get("peak_detection")), strict=True):
                    if guide:
                        guide["anchor_stems"] = [stems[index] for index in guide.pop("anchor_indices")]
                        guides[stem][str(ch)] = guide
        write_json(output, {**identity, "signature": signature, "guides": guides, "skipped_inputs": skipped_inputs})
        return guides

    def _fit_record(self, ch, fit_res, meta, is_manual, n_events, input_quality):
        temps = meta.get("temp", [float("nan")] * 4) if isinstance(meta, dict) else [float("nan")] * 4
        temp_errs = meta.get("temp_err", [0.0] * 4) if isinstance(meta, dict) else [0.0] * 4
        biases = meta.get("bias", [float("nan")] * 4) if isinstance(meta, dict) else [float("nan")] * 4
        bias_errs = meta.get("bias_err", [0.0] * 4) if isinstance(meta, dict) else [0.0] * 4
        ch_temp = float(temps[ch]) if ch < len(temps) else float("nan")
        ch_temp_err = float(temp_errs[ch]) if ch < len(temp_errs) else 0.0
        ch_bias = float(biases[ch]) if ch < len(biases) else float("nan")
        ch_bias_err = float(bias_errs[ch]) if ch < len(bias_errs) else 0.0

        # 分辨率 R = FWHM / center；误差用一阶传播
        if np.isfinite(fit_res.center) and fit_res.center > 0:
            resolution = float(fit_res.fwhm / fit_res.center)
            sigma_term = (fit_res.fwhm / fit_res.center) * (fit_res.sigma_err / max(fit_res.sigma, 1e-12)) if fit_res.sigma > 0 else 0.0
            center_term = (fit_res.fwhm / fit_res.center**2) * fit_res.center_err
            resolution_err = float(np.sqrt(sigma_term**2 + center_term**2))
            if self._native_fit:
                resolution_variance = (sigma_term**2 + center_term**2
                                       - 2 * resolution**2 * fit_res.center_sigma_covariance /
                                       (fit_res.center * fit_res.sigma))
                resolution_err = float(np.sqrt(max(resolution_variance, 0.)))
        else:
            resolution = float("nan")
            resolution_err = 0.0

        record = {
            "channel": int(ch),
            "center": float(fit_res.center),
            "center_err": float(fit_res.center_err),
            "sigma": float(fit_res.sigma),
            "sigma_err": float(fit_res.sigma_err),
            "amplitude": float(fit_res.amplitude),
            "amplitude_err": float(fit_res.amplitude_err),
            "fwhm": float(fit_res.fwhm),
            "height": float(fit_res.height),
            "resolution": resolution,
            "resolution_err": resolution_err,
            "temp": ch_temp,
            "temp_err": ch_temp_err,
            "bias": ch_bias,
            "bias_err": ch_bias_err,
            "window_lo": float(fit_res.lo),
            "window_hi": float(fit_res.hi),
            "window_manual": is_manual,
            "fit_mode": fit_res.fit_mode,
            "rsquared": float(fit_res.rsquared),
            "n_events": int(n_events),
            "peak_detection": fit_res.peak_detection,
            "fit_quality": fit_res.fit_quality,
            "histogram_config": fit_res.histogram_config,
            "input_quality": input_quality,
        }
        if self._native_fit:
            record.update(center_sigma_covariance=fit_res.center_sigma_covariance,
                          height_std=fit_res.height_std, redchi=fit_res.redchi, ndf=fit_res.ndf,
                          n_bins=len(fit_res.x_cut), ordinate="Counts/ADC",
                          amplitude_kind="area", resolution_unit="fraction")
        if fit_res.slope is not None:
            record["slope"] = float(fit_res.slope)
        if fit_res.intercept is not None:
            record["intercept"] = float(fit_res.intercept)
        if fit_res.exp_amplitude is not None:
            record["exp_amplitude"] = float(fit_res.exp_amplitude)
        if fit_res.exp_decay is not None:
            record["exp_decay"] = float(fit_res.exp_decay)
        if fit_res.quad_a is not None:
            record["quad_a"] = float(fit_res.quad_a)
            record["quad_b"] = float(fit_res.quad_b)
            record["quad_c"] = float(fit_res.quad_c)
        return record

    def _is_manual_entry(self, item: dict[str, Any]) -> bool:
        """判断窗口条目是否为 resource 中提供的拟合窗口。

        legacy 项目里只要 ``lo`` / ``hi`` 同时存在就视为"已配置"，``manual`` 字段
        只在交互式编辑器里做显示标识，不参与逻辑判断。这里沿用 legacy 行为：
        有 lo/hi 就用，否则走自动峰检测；同时 L1 不再回写 resource。
        """
        if not isinstance(item, dict):
            return False
        return "lo" in item and "hi" in item

    def _load_windows(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = read_json(path)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _extract_tb_file_mean(self, metadata: dict[str, Any]) -> tuple[float, float]:
        temps = metadata.get("temp", []) if isinstance(metadata, dict) else []
        biases = metadata.get("bias", []) if isinstance(metadata, dict) else []
        valid_t = [float(t) for t in temps if isinstance(t, (int, float)) and np.isfinite(t)]
        valid_b = [float(b) for b in biases if isinstance(b, (int, float)) and np.isfinite(b)]
        temp_mean = float(np.mean(valid_t)) if valid_t else float("nan")
        bias_mean = float(np.mean(valid_b)) if valid_b else float("nan")
        return temp_mean, bias_mean

    def _fit_channel(
        self,
        stem: str,
        ch: int,
        energy: np.ndarray,
        config: dict[str, Any],
        auto_window: bool,
        peak_config: dict[str, Any] | None = None,
        histogram_config: dict[str, float] | None = None,
    ) -> FitResult:
        if histogram_config is not None:
            from ...workbench.acquisition import tb_histogram_config

            histogram = tb_histogram_config({"fitting": {"tb_histogram": histogram_config}})
            bin_lo, bin_hi, bin_width = (histogram[key] for key in ("min", "max", "bin_width"))
        else:
            bin_lo, bin_width = 0, 1
            bin_hi = int(max(420, np.percentile(energy, 99.8) + 40))
            if self.payload_name in ("14B", "15B"):
                bin_hi = min(1200, bin_hi)
            else:
                bin_hi = min(65536, bin_hi)
            histogram = {"min": float(bin_lo), "max": float(bin_hi), "bin_width": float(bin_width)}
        bins = np.arange(bin_lo, bin_hi + bin_width, bin_width)
        if bins[-1] > bin_hi:
            bins[-1] = bin_hi
        if not np.allclose(np.diff(bins), bin_width):
            # Keep equal bin widths required by candidate detection; the final
            # edge may extend by less than one bin beyond the requested bound.
            bins = np.arange(bin_lo, bin_hi + bin_width, bin_width)
        hist, edges = np.histogram(energy, bins=bins)
        mids = (edges[:-1] + edges[1:]) / 2.0

        return self._fit_histogram(stem, ch, mids, hist, config, auto_window, peak_config, histogram)

    def _fit_histogram(
        self, stem: str, ch: int, mids: np.ndarray, hist: np.ndarray,
        config: dict[str, Any], auto_window: bool,
        peak_config: dict[str, Any] | None = None,
        histogram: dict[str, float] | None = None,
    ) -> FitResult:
        """Fit the same original counting histogram used by the event processor.

        Exposing this step permits repeatable spectral studies without expanding
        histogram counts back into artificial pulse events.
        """
        mids, hist = np.asarray(mids, dtype=float), np.asarray(hist, dtype=float)
        if reason := unsupported_spectral_model(self.response_payload, ch):
            raise ValueError(reason)
        if histogram is None:
            width = float(np.median(np.diff(mids)))
            histogram = {"min": float(mids[0] - width / 2),
                         "max": float(mids[-1] + width / 2), "bin_width": width}

        window = 11 if len(hist) >= 11 else max(5, len(hist) // 2 * 2 + 1)
        hist_smooth = np.asarray(
            savgol_filter(hist.astype(float), window_length=window, polyorder=2),
            dtype=float,
        )

        item = config.get(stem, {}).get(str(ch), {}) if config else {}
        manual = self._is_manual_entry(item)

        requested = [float(item["lo"]), float(item["hi"])] if manual else None
        if manual:
            candidate, detection = detect_photopeak_for_fit(mids, hist, window=requested, config=peak_config)
            lo, hi = requested
            detection["histogram"] = histogram
            return self._fit_candidate(mids, hist, hist_smooth, candidate, detection, lo, hi, histogram)

        # A TB scintillation peak must be resolved at more than the single-bin
        # scale. These dimensionless response bounds are configurable; no
        # payload-specific ADC threshold or historical hand window is used.
        settings = {"min_resolution": .03, "max_resolution": .6, **(peak_config or {})}
        candidates, detection = propose_photopeaks(mids, hist, config=settings)
        detection["histogram"] = histogram
        # Identify the strongest original-count Gaussian contrast before judging
        # model adequacy. A poor fit of that signal must not silently switch to
        # a statistically weaker, distant continuum structure.
        candidate = candidates[0]
        if (len(candidates) > 1 and candidates[1].score > .82 * candidate.score
                and abs(candidates[1].center - candidate.center) > 2 * max(candidate.sigma, candidates[1].sigma)):
            detection.setdefault("selection_warnings", []).append("Competing photopeaks need an independent gain anchor")
        trials, rejected = [], []
        initial_sigma = candidate.sigma
        for extent in (3., 2.5, 2.):
            lo = max(float(mids[0]), candidate.center - extent * candidate.sigma)
            hi = min(float(mids[-1]), candidate.center + extent * candidate.sigma)
            if settings.get("noise_ceiling") is not None:
                lo = max(lo, float(settings["noise_ceiling"]))
            details = copy.deepcopy(detection)
            details.update(manual_window=False, selected_center=candidate.center,
                           candidate_origin="original_count_model_comparison",
                           window_sigma=extent, response_bounds={
                               "min_resolution": settings["min_resolution"],
                               "max_resolution": settings["max_resolution"],
                           })
            try:
                fit = self._fit_candidate(mids, hist, hist_smooth, candidate, details, lo, hi, histogram)
                fraction = fit.fwhm / max(fit.center, 1e-12)
                if not settings["min_resolution"] <= fraction <= settings["max_resolution"]:
                    fit.fit_quality["accepted"] = False
                    fit.fit_quality.update(status="suspect", suspect=True, quality_pass=False, warning_only=True)
                    fit.fit_quality["warnings"].append("Fitted width is outside the configured scintillation response range")
                trials.append(fit)
                if len(trials) == 1 and fit.sigma < .7 * initial_sigma:
                    # A coarse-scale shoulder can merge with a neighbour.
                    # Estimate its width from original counts, then refit
                    # a conventional interval in those measured sigma units.
                    candidate.sigma = fit.sigma
                # Keep the widest accepted interval. This is bounded model
                # adequacy testing; it does not shrink to an arbitrary set
                # of bins until an apparently good chi-square is obtained.
                if fit.fit_quality["accepted"]:
                    break
            except (ValueError, RuntimeError) as exc:
                rejected.append({"center": candidate.center, "window_sigma": extent, "error": str(exc)})
        if not trials:
            detection["fit_trials"] = rejected
            raise PeakDetectionError("No resolved photopeak model could be fitted", detection)

        def score(fit):
            quality = fit.fit_quality
            return np.sqrt(max(quality["delta_bic_background"], 0))

        trials.sort(key=lambda fit: (not fit.fit_quality["accepted"], -score(fit)))
        selected = trials[0]
        selected.peak_detection["fit_trials"] = [{
            "center": fit.center, "sigma": fit.sigma, "window": [fit.lo, fit.hi],
            "score": float(score(fit)), "quality": fit.fit_quality,
        } for fit in trials]
        selected.peak_detection["fit_errors"] = rejected
        selected.peak_detection["selection_status"] = "resolved" if selected.fit_quality["accepted"] else "suspect"
        return selected

    def _fit_candidate(self, mids, hist, hist_smooth, candidate, detection, lo, hi, histogram):
        peak_x = candidate.center

        cut_mask = (mids >= lo) & (mids <= hi)
        x_cut = mids[cut_mask]
        width = float(histogram["bin_width"])
        self._active_bin_width = width
        y_cut = hist[cut_mask] / width if self._native_fit else hist[cut_mask]
        if x_cut.size < 8 or np.max(y_cut) <= 0:
            raise RuntimeError("Invalid fit window")

        center_lo = max(lo, peak_x - 1.5 * candidate.sigma)
        center_hi = min(hi, peak_x + 1.5 * candidate.sigma)
        result, comps, fit_mode, rsquared = self._select_best_fit(
            x_cut, y_cut, peak_x, center_lo, center_hi, candidate.sigma,
        )
        quality = fit_quality(result, x_cut, y_cut, self._fit_errors(y_cut),
                              selection_warnings=detection.get("selection_warnings"))

        center = float(result.params["g_center"].value)
        center_err = float(result.params["g_center"].stderr or 0.0)
        sigma = float(result.params["g_sigma"].value)
        sigma_err = float(result.params["g_sigma"].stderr or 0.0)
        amplitude = float(result.params["g_amplitude"].value)
        amplitude_err = float(result.params["g_amplitude"].stderr or 0.0)
        slope = intercept = None
        exp_amplitude = exp_decay = None
        quad_a = quad_b = quad_c = None

        if fit_mode == FIT_MODE_LIN_GAUS:
            slope = float(result.params["b_slope"].value)
            intercept = float(result.params["b_intercept"].value)
            background_fit = comps["b_"]
        elif fit_mode == FIT_MODE_EXP_GAUS:
            exp_amplitude = float(result.params["e_amplitude"].value)
            exp_decay = float(result.params["e_decay"].value)
            background_fit = comps["e_"]
        elif fit_mode == FIT_MODE_QUAD_GAUS:
            quad_a = float(result.params["q_a"].value)
            quad_b = float(result.params["q_b"].value)
            quad_c = float(result.params["q_c"].value)
            background_fit = comps["q_"]
        else:  # gaus only
            background_fit = np.zeros_like(x_cut, dtype=float)

        fwhm = 2.0 * np.sqrt(2.0 * np.log(2.0)) * sigma
        height = amplitude / (sigma * np.sqrt(2.0 * np.pi)) if sigma > 0 else float("nan")
        residual = y_cut - result.best_fit
        covariance = 0.
        height_std = 0.
        if result.covar is not None and all(name in result.var_names for name in ("g_center", "g_sigma")):
            ic, isig = result.var_names.index("g_center"), result.var_names.index("g_sigma")
            covariance = float(result.covar[ic, isig])
            if "g_amplitude" in result.var_names and sigma > 0:
                gradient = np.zeros(len(result.var_names))
                gradient[result.var_names.index("g_amplitude")] = 1 / (sigma * np.sqrt(2 * np.pi))
                gradient[isig] = -height / sigma
                height_std = float(np.sqrt(max(float(gradient @ result.covar @ gradient), 0.)))

        return FitResult(
            hist=hist / width if self._native_fit else hist,
            mids=mids,
            hist_smooth=hist_smooth / width if self._native_fit else hist_smooth,
            x_cut=x_cut,
            y_cut=y_cut,
            best_fit=result.best_fit,
            gauss_fit=comps["g_"],
            background_fit=background_fit,
            residual=residual,
            center=center,
            center_err=center_err,
            sigma=sigma,
            sigma_err=sigma_err,
            amplitude=amplitude,
            amplitude_err=amplitude_err,
            slope=slope,
            intercept=intercept,
            exp_amplitude=exp_amplitude,
            exp_decay=exp_decay,
            quad_a=quad_a,
            quad_b=quad_b,
            quad_c=quad_c,
            fwhm=float(fwhm),
            height=float(height),
            fit_mode=fit_mode,
            lo=float(lo),
            hi=float(hi),
            rsquared=float(rsquared),
            peak_detection=detection,
            fit_quality=quality,
            histogram_config=histogram,
            center_sigma_covariance=covariance, height_std=height_std,
            redchi=float(result.redchi), ndf=int(result.nfree),
            ordinate="Counts/ADC" if self._native_fit else "counts/bin",
        )

    def _fit_errors(self, values):
        if self._native_fit:
            return gehrels_error(np.asarray(values) * self._active_bin_width) / self._active_bin_width
        return np.sqrt(np.maximum(values, 1.0))

    def _fit_options(self):
        return {"scale_covar": False, "max_nfev": getattr(self, "_native_max_nfev", 100000)} if self._native_fit else {}

    @staticmethod
    def _selection_figure(native, conditions):
        import pandas as pd

        hk = pd.read_parquet(Path(native["l0_node"]) / "hk.parquet")
        selected = hk["source_idx"].isin(conditions["selection"]["selected_source_idx"])
        fig, axes = plt.subplots(2, 1, figsize=(9, 5), sharex=True)
        for ch in range(4):
            for ax, name in zip(axes, ("sipm_temp", "bias"), strict=True):
                ax.plot(hk["source_idx"], hk[f"{name}_ch{ch}"], color="0.8", linewidth=.6)
                ax.plot(hk.loc[selected, "source_idx"], hk.loc[selected, f"{name}_ch{ch}"],
                        ".", markersize=2, label=f"CH{ch}")
        axes[0].set_ylabel("Temperature (°C)")
        axes[1].set_ylabel("Bias (V)")
        axes[1].set_xlabel("HK source_idx")
        axes[0].legend(ncol=4, frameon=False)
        fig.tight_layout()
        raw = figure_bytes(fig)
        plt.close(fig)
        return raw

    def _get_fit_window(
        self,
        item: dict[str, Any],
        auto_lo: float,
        auto_hi: float,
        auto_window: bool,
    ) -> tuple[float, float]:
        if isinstance(item, dict) and "lo" in item and "hi" in item:
            return float(item["lo"]), float(item["hi"])
        if auto_window:
            return auto_lo, auto_hi
        return float(np.floor(auto_lo)), float(np.ceil(auto_hi))

    def _find_valley_left(self, y: np.ndarray, center_idx: int, span: int = 85) -> int:
        lo = max(0, center_idx - span)
        segment = y[lo : center_idx + 1]
        if segment.size < 5:
            return max(0, center_idx - 55)
        return lo + int(np.argmin(segment))

    def _find_valley_right(self, y: np.ndarray, center_idx: int, span: int = 110) -> int:
        hi = min(len(y) - 1, center_idx + span)
        segment = y[center_idx : hi + 1]
        if segment.size < 5:
            return min(len(y) - 1, center_idx + 55)
        return center_idx + int(np.argmin(segment))

    def _calculate_rsquared(self, y_data: np.ndarray, y_fit: np.ndarray) -> float:
        ss_res = np.sum((y_data - y_fit) ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
        if ss_tot == 0:
            return 0.0
        return float(1.0 - ss_res / ss_tot)

    def _fit_lin_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_") + LinearModel(prefix="b_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)
        # A fitted local continuum can have a negative extrapolated intercept
        # at ADC=0. Constraining that distant intercept biases a native peak and
        # makes its bound-transform covariance singular at intercept=0.
        params["b_intercept"].set(value=float(max(np.percentile(y_cut, 20), 0.0)),
                                  min=-np.inf if self._native_fit else 0.0)
        params["b_slope"].set(value=0.0)

        weights = 1.0 / self._fit_errors(y_cut)
        result = model.fit(y_cut, params, x=x_cut, weights=weights, **self._fit_options())
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_exp_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_") + ExponentialModel(prefix="e_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)
        params["e_amplitude"].set(value=float(max(np.percentile(y_cut, 20), 1.0)), min=0.0)
        params["e_decay"].set(value=max(float(np.ptp(x_cut)), 1.), min=1.0, max=max(float(np.ptp(x_cut)) * 100, 1000.))

        weights = 1.0 / self._fit_errors(y_cut)
        result = model.fit(y_cut, params, x=x_cut, weights=weights, **self._fit_options())
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_quad_gaus(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_") + QuadraticModel(prefix="q_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)
        params["q_a"].set(value=0.0)
        params["q_b"].set(value=0.0)
        params["q_c"].set(value=float(max(np.percentile(y_cut, 20), 0.0)))

        weights = 1.0 / self._fit_errors(y_cut)
        result = model.fit(y_cut, params, x=x_cut, weights=weights, **self._fit_options())
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _fit_gaus_only(self, x_cut: np.ndarray, y_cut: np.ndarray, peak_x: float, lo: float, hi: float, sigma_guess: float = 12.0):
        model = GaussianModel(prefix="g_")
        params = model.make_params()
        params["g_center"].set(value=float(peak_x), min=float(lo), max=float(hi))
        params["g_sigma"].set(value=max(sigma_guess, 1.0), min=max(0.7, sigma_guess / 12), max=max(3.0 * sigma_guess, 2.0))
        params["g_amplitude"].set(value=float(np.max(y_cut) * sigma_guess * np.sqrt(2.0 * np.pi)), min=0.0)

        weights = 1.0 / self._fit_errors(y_cut)
        result = model.fit(y_cut, params, x=x_cut, weights=weights, **self._fit_options())
        comps = result.eval_components(x=x_cut)
        return result, comps

    def _select_best_fit(
        self,
        x_cut: np.ndarray,
        y_cut: np.ndarray,
        peak_x: float,
        lo: float,
        hi: float,
        sigma_guess: float = 12.0,
    ):
        """对齐原古项目 calibration_process：单高斯峰 + 4 选 1 单背景。

        候选: gaus | gaus + lin | gaus + exp | gaus + quad
        策略: 同时拟合，使用已知计数方差的 χ² + 2k 挑选单背景模型。
        """
        candidates = [
            (self._fit_gaus_only,  FIT_MODE_GAUS),
            (self._fit_lin_gaus,   FIT_MODE_LIN_GAUS),
            (self._fit_exp_gaus,   FIT_MODE_EXP_GAUS),
            (self._fit_quad_gaus,  FIT_MODE_QUAD_GAUS),
        ]
        if self._native_fit and getattr(self, "_native_background", "auto") != "auto":
            mode = {None: FIT_MODE_GAUS, "lin": FIT_MODE_LIN_GAUS,
                    "exp": FIT_MODE_EXP_GAUS, "quad": FIT_MODE_QUAD_GAUS}.get(self._native_background)
            if mode is None:
                raise ValueError(f"Unsupported configured TB background: {self._native_background}")
            candidates = [(fit_fn, name) for fit_fn, name in candidates if name == mode]
        results = []
        for fit_fn, mode in candidates:
            try:
                result, comps = fit_fn(x_cut, y_cut, peak_x, lo, hi, sigma_guess)
                rsq = self._calculate_rsquared(y_cut, result.best_fit)
                if not result.success or not np.isfinite(result.best_fit).all():
                    continue
                aic = float(np.sum(((y_cut - result.best_fit) / self._fit_errors(y_cut)) ** 2)) + 2 * result.nvarys
                results.append((result, comps, mode, rsq, aic))
            except Exception:
                continue

        if not results:
            raise RuntimeError("All fit modes failed")

        results.sort(key=lambda item: (
            not all(item[0].params[name].stderr is not None and np.isfinite(item[0].params[name].stderr)
                    and item[0].params[name].stderr > 0 for name in ("g_center", "g_sigma", "g_amplitude")),
            item[4],
        ))
        best = results[0]
        return best[0], best[1], best[2], best[3]

    def _plot_channel(self, ax_spec, ch: int, fit_res: FitResult) -> None:
        from ...common.plotting import (
            adaptive_xlim,
            plot_spectrum_fit,
            style_axes,
            style_legend,
            style_title,
        )
        plot_spectrum_fit(
            ax_spec,
            mids=fit_res.mids,
            raw=fit_res.hist,
            cut_x=fit_res.x_cut,
            cut_y=fit_res.y_cut,
            best_fit=fit_res.best_fit,
            gauss_fit=fit_res.gauss_fit,
            background_fit=fit_res.background_fit,
            fit_lo=fit_res.lo,
            fit_hi=fit_res.hi,
            center=fit_res.center,
        )
        style_title(
            ax_spec,
            f"ch{ch}: center={fit_res.center:.2f}, sigma={fit_res.sigma:.2f}, mode={fit_res.fit_mode}",
        )
        style_axes(ax_spec, xlabel="ADC unit", ylabel=fit_res.ordinate)
        adaptive_xlim(ax_spec, fit_res.mids, fit_res.hist, fit_res.center, fit_res.sigma, fit_res.hi)
        if self._native_fit:
            padding = (fit_res.hi - fit_res.lo) * .35
            lower, upper = max(0., fit_res.lo - padding), fit_res.hi + padding
            ax_spec.set_xlim(lower, upper)
            visible = fit_res.hist[(fit_res.mids >= lower) & (fit_res.mids <= upper)]
            if visible.size:
                ax_spec.set_ylim(min(0., float(visible.min())), 1.12 * max(float(visible.max()), float(np.max(fit_res.best_fit)), 1.))
        style_legend(ax_spec)
