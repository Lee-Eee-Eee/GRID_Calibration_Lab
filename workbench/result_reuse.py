"""Pure identities for reusing complete, matching processor results."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePath

from .impacts import LAYERS, scientific_fingerprint

# Bump this revision when a processor's numerical behavior or fixed defaults
# change. Deployment-specific implementation hashes may additionally be placed
# in input_identity, without making compute_key inspect the filesystem.
COMPUTATION_REVISION = "scoped-inputs-v1"
_PRESENTATION = {"reason", "description", "comment", "comments", "note", "notes", "label",
                 "display_name", "view", "plot_scale", "coordinate_scale", "selected_tab", "color"}
_ORIGIN = {"origin", "run_id", "run_name", "created_at", "updated_at", "modified_at", "saved_at",
           "job_id"}
_LOCATION = {"source_path", "frozen_root", "product_root", "layer_path", "snapshot_path"}
_STRONG_DIGEST = {"sha256", "model_sha256", "content_hash", "digest"}


def _plain(value):
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items() if key not in _PRESENTATION}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if hasattr(value, "tolist"):
        return _plain(value.tolist())
    return value


def _identity(value, *, tb=False):
    if isinstance(value, dict):
        hashed = any(value.get(key) for key in _STRONG_DIGEST)
        result = {}
        for key, item in value.items():
            if key in _ORIGIN or key in _PRESENTATION:
                continue
            if key in _LOCATION and (hashed or "files" in value):
                continue
            if key == "id" and (hashed or "files" in value):
                continue
            if hashed and key in {"path", "mtime", "mtime_ns", "st_mtime_ns", "size", "st_size", "filename"}:
                # Input basenames remain meaningful to legacy readers. TB
                # model filenames, which are normally timestamps, do not.
                if key == "path" and not tb and "relative" not in value:
                    result["filename"] = str(item).replace("\\", "/").rsplit("/", 1)[-1]
                continue
            result[str(key)] = _identity(item, tb=tb)
        return result
    if isinstance(value, (list, tuple)):
        values = [_identity(item, tb=tb) for item in value]
        # File inventories are sets; their enumeration order is irrelevant.
        if values and all(isinstance(item, dict) and any(key in item for key in ("path", "relative", "sha256", "filename"))
                          for item in values):
            values.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return values
    return _plain(value)


def _channels(config):
    return sorted(set(config.get("fit_channels") or range(4)))


def _tb_windows(config):
    selected, channels = config.get("only"), {str(ch) for ch in _channels(config)}
    return {stem: {str(ch): _plain(window) for ch, window in bounds.items() if str(ch) in channels}
            for stem, bounds in config.get("windows", {}).items() if selected is None or stem in selected}


def _ec_scope(config):
    point = str(config.get("ec_point_id") or "").split(":")
    energy = point[2] if len(point) == 5 else config.get("energy_key")
    channels = {point[3]} if len(point) == 5 else {str(ch) for ch in _channels(config)}
    return energy, channels


def _ec_windows(config):
    energy, channels = _ec_scope(config)
    result = {}
    for identity, window in config.get("ec_windows", {}).items():
        parts = identity.split(":")
        if len(parts) == 5 and (energy is None or parts[2] == energy) and parts[3] in channels:
            # Keep all energies of this spectrum/channel: a joint model can
            # consume a companion line even when only one point was clicked.
            result[identity] = _plain(window)
    return result


def _settings(layer, config):
    settings = {}
    if layer not in {"TB_L2", "EC_L3"}:
        if config.get("only") is not None:
            settings["only"] = sorted(set(config["only"]))
        else:
            settings["representative"] = bool(config.get("representative", True))
    if layer in {"TB_L1", "TB_L2", "EC_L2", "EC_L3"}:
        settings["fit_channels"] = _channels(config)
    if layer == "TB_L1":
        settings["windows"] = _tb_windows(config)
    if layer == "TB_L2":
        settings["excluded"] = sorted(set(config.get("excluded", [])))
    if layer in {"TB_L1", "EC_L2"}:
        settings["automatic_window"] = bool(config.get("automatic_window", False))
        settings["peak_detection"] = _plain(config.get("peak_detection", {}))
    if layer in {"EC_L1", "EC_L2"}:
        settings.update({key: config[key] for key in ("ec_point_id", "energy_key") if config.get(key) is not None})
    if layer == "EC_L2":
        settings["ec_windows"] = _ec_windows(config)
        if config.get("bin_width") is not None:
            settings["bin_width"] = config["bin_width"]
    if layer in {"EC_L2", "EC_L3"}:
        energy, _ = _ec_scope(config)
        settings["ec_peak_sets"] = {key: _plain(value) for key, value in config.get("ec_peak_sets", {}).items()
                                     if layer == "EC_L3" or energy is None or energy == key}
    if layer == "EC_L3":
        settings["ec_excluded"] = sorted(set(config.get("ec_excluded", [])))
        settings.update({key: config[key] for key in ("energy_split_low", "energy_split_high") if key in config})
    return settings


def compute_key(layer, config, profile, input_identity, tb_identity=None):
    """Hash effective settings, immutable inputs and model dependency identity.

    The caller supplies file content digests (or path/stat identities when
    content digests are unavailable) and verifies saved outputs still exist.
    For partial aggregates include the retained aggregate model in
    ``input_identity`` too: its unchanged channels are part of the result.
    """
    if layer not in LAYERS:
        raise ValueError(f"Unknown layer: {layer}")
    value = {"revision": COMPUTATION_REVISION, "layer": layer,
             "profile": scientific_fingerprint(profile or {}, layer),
             "settings": _settings(layer, config or {}), "inputs": _identity(input_identity)}
    if layer in {"EC_L1", "EC_L2", "EC_L3"}:
        value["tb"] = _identity(tb_identity, tb=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def find_matching_result(state, key, layer):
    """Return a detached successful job with the exact computation identity."""
    if layer not in LAYERS:
        raise ValueError(f"Unknown layer: {layer}")
    if not key:
        return None
    matches = [job for job in (state or {}).get("jobs", []) if job.get("layer") == layer
               and job.get("status") == "succeeded" and job.get("computational_key") == key and job.get("outputs")]
    if not matches:
        return None
    latest = max(matches, key=lambda job: (job.get("finished_at") or job.get("created_at") or "", job.get("id", "")))
    return copy.deepcopy(latest)
