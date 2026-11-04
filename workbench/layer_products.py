"""Read-only catalog of layer products in shared data and personal tasks.

The catalog reads directory entries and small JSON indexes, never event arrays.
References retain their original paths; consumers decide how to stage an input
inside their own job directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

LAYERS = ("TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3")
_READ_LIMIT = 4 * 1024 * 1024


def _json(path):
    try:
        limit = 64 * 1024 * 1024 if path.name == "state.json" else _READ_LIMIT
        if path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def _source_payload(state):
    return (state.get("dataset") or {}).get("source_payload") or state.get("payload")


def _stamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _model_json(value, layer):
    if layer == "TB_L2":
        rows = value.get("fit_params") if isinstance(value, dict) else value
        return isinstance(rows, list) and any(
            isinstance(row, dict) and {"G0", "k", "V0", "b", "c"} <= row.keys() for row in rows
        )
    return isinstance(value, dict) and any(key in value for key in (
        "calibration_params", "EC_low", "EC_high", "energy_calibration", "energy_coefficients",
    ))


def _point_json(value):
    rows = value.values() if isinstance(value, dict) else value
    return isinstance(value, (dict, list)) and any(
        isinstance(row, dict) and any(key in row for key in ("center", "fit_performed", "fit_status"))
        for row in rows
    )


class LayerProductCatalog:
    """List specific, compatible result versions without changing their files."""

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.runs = self.workspace / "runs"
        self._json_cache = {}
        self._candidate_cache = {}
        self._known_candidates = {}

    def invalidate(self):
        """Drop short-lived listings after a task adopts or produces a result."""
        self._candidate_cache.clear()

    def _read(self, path):
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._json_cache:
            # A long-lived workbench should not retain every past file version.
            if len(self._json_cache) > 4096:
                self._json_cache.clear()
            self._json_cache[key] = _json(path)
        return self._json_cache[key]

    def _files(self, directory):
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.rglob("*") if path.is_file())

    def _bundles(self, root, payload, layer):
        """Return files for internal IDs, keeping native EC names unambiguous."""
        family, level = layer.split("_")
        base = root / payload / family.lower()
        directory = base / level
        if layer in ("TB_L2", "EC_L3"):
            native = base / "L2"
            marker = "*_temp_bias_fit.json" if layer == "TB_L2" else "*_ec_coef_sci_ch*.json"
            native_stamps = set()
            for node in sorted(native.glob("*")):
                if not node.is_dir() or node.name in ("json", "figures", "check"):
                    continue
                models = [path for path in node.glob(marker) if _model_json(self._read(path), layer)]
                if models:
                    native_stamps.add(node.name)
                    yield self._files(node), node, "native", node.name
            for path in sorted((directory / "json").glob("*.json")):
                if not _model_json(self._read(path), layer):
                    continue
                if any(path.name.startswith(stamp + "_") for stamp in native_stamps):
                    continue
                companions = [path]
                stem = path.name.removesuffix(path.suffix)
                companions.extend(other for other in path.parent.glob(stem + ".*") if other != path)
                for folder in ("figures", "check"):
                    companions.extend((directory / folder).glob(stem + ".*"))
                yield sorted(set(companions)), directory, "compatibility", path.stem
            return

        if layer == "EC_L1":
            # Native ec/L1/<analysis> holds *peak fits*, not corrected events.
            paths = self._files(directory / "parquet") + self._files(directory / "json")
            paths += self._files(base / "corr")
            if any(path.suffix == ".parquet" for path in paths):
                yield sorted(set(paths)), directory, "compatibility", "current"
            return

        if layer == "EC_L2":
            compat = [path for folder in ("json", "figures", "check") for path in self._files(directory / folder)]
            points = [path for path in (directory / "json").glob("*.json") if _point_json(self._read(path))]
            native_nodes = [node for node in (base / "L1").glob("*") if node.is_dir()
                            and _point_json(self._read(node / "points.json"))]
            native_files = [path for node in native_nodes for path in self._files(node)]
            if points or native_nodes:
                layout = "mixed" if points and native_nodes else "native" if native_nodes else "compatibility"
                yield sorted(set(compat + native_files)), directory, layout, "current"
            return

        paths = self._files(directory)
        if layer in ("TB_L0", "EC_L0"):
            usable = any(path.suffix == ".parquet" and path.name != "hk.parquet" for path in paths)
        else:
            usable = any((path.name == "points.json" or path.name.endswith("_fit_results.l1.json"))
                         and _point_json(self._read(path)) for path in paths)
        if usable:
            native = any(path.parent.name not in ("parquet", "json", "figures", "check") for path in paths)
            compatibility = any(path.parent.name in ("parquet", "json") for path in paths)
            layout = "mixed" if native and compatibility else "native" if native else "compatibility"
            yield paths, directory, layout, "current"

    def _coverage(self, paths, layer):
        channels, observations, schemas = set(), set(), set()
        for path in paths:
            if path.suffix != ".json" or path.name.endswith("manifest.json"):
                continue
            value = self._read(path)
            if isinstance(value, dict):
                if value.get("analysis_contract"):
                    schemas.add(value["analysis_contract"])
                if value.get("schema"):
                    schemas.add(value["schema"])
                if path.name in ("meta.json", "points.json") and path.parent.name not in ("json", "L1", "L2"):
                    observations.add(path.parent.name)
                for key in ("measurement", "observation", "stem"):
                    if isinstance(value.get(key), str):
                        observations.add(value[key])
                if isinstance(value.get("calibration_params"), dict):
                    channels.update(int(ch) for ch in value["calibration_params"] if str(ch).isdigit())
                if path.name == "points.json":
                    channels.update(int(ch) for ch in value if str(ch).isdigit())
                rows = [value]
                if isinstance(value.get("fit_params"), list):
                    rows += value["fit_params"]
            else:
                rows = value if isinstance(value, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if isinstance(row.get("channel"), int) and row["channel"] in range(4):
                    channels.add(row["channel"])
                for key in ("stem", "signal_file", "native_analysis_id"):
                    if isinstance(row.get(key), str):
                        observations.add(row[key].removesuffix("_corr.l1.parquet").removesuffix(".l0.parquet"))
            if path.name.endswith("_fit_results.l1.json"):
                observations.add(path.name.removesuffix("_fit_results.l1.json"))
            if path.name.endswith("_fit_results.l2.json"):
                observations.add(path.name.removesuffix("_fit_results.l2.json"))
        for path in paths:
            if path.suffix == ".parquet" and path.name not in ("events.parquet", "hk.parquet", "spectra.parquet", "issues.parquet"):
                observations.add(path.name.removesuffix("_corr.l1.parquet").removesuffix(".l0.parquet"))
                match = re.search(r"(?:^|_)ch([0-3])(?:_|\.)", path.name)
                if match:
                    channels.add(int(match[1]))
            elif path.name == "events.parquet":
                observations.add(path.parent.name)
        contract = "cali_proc@0.5" if any(str(schema).startswith(("g", "cali_proc@")) for schema in schemas) else None
        if layer in ("TB_L1", "EC_L2"):
            observations = self._peak_observations(paths, layer)
        return sorted(channels), sorted(observations), contract

    def _peak_observations(self, paths, layer):
        """Expose the group IDs actually consumed by the fit JSON reader."""
        suffix = "_fit_results.l1.json" if layer == "TB_L1" else "_fit_results.l2.json"
        compatibility = {path.name.removesuffix(suffix) for path in paths if path.name.endswith(suffix)}
        if compatibility:
            return compatibility
        nodes = [path.parent for path in paths if path.name == "points.json"]
        if layer == "TB_L1":
            return {node.name for node in nodes}
        groups = set()
        for node in nodes:
            meta = self._read(node / "meta.json") or {}
            points = self._read(node / "points.json") or {}
            if not isinstance(points, dict):
                continue
            for channel, record in points.items():
                if not isinstance(record, dict):
                    continue
                kind = meta.get("kind", record.get("source"))
                if kind in ("source", "src"):
                    name = record.get("source_name") or meta.get("source_name") or meta.get("measurement", node.name)
                    base = f"src_{name}"
                    evidence = meta.get("channel_evidence", {}).get(str(channel), meta)
                    observation = record.get("signal_file") or evidence.get("roles", {}).get(str(channel), {}).get("primary")
                    observation = str(observation or meta.get("measurement", node.name)).removesuffix("_corr.l1.parquet")
                else:
                    energy = record.get("energy_keV", record.get("E"))
                    if not isinstance(energy, (int, float)) or not math.isfinite(float(energy)):
                        continue
                    base = f"{float(energy):g}keV"
                    observation = str(meta.get("measurement", node.name))
                groups.add((base, observation))
        result = set()
        for base, observation in groups:
            if sum(group[0] == base for group in groups) == 1:
                result.add(base)
            else:
                # Match contract_import's stable repeated-exposure group IDs.
                slug = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(observation).name)[:60]
                digest = hashlib.sha256(observation.encode("utf-8")).hexdigest()[:8]
                result.add(f"{base}__{slug}-{digest}")
        return result

    def _candidate(self, state, root, payload, layer, files, directory, layout, version, *, origin, job=None):
        records, fingerprint = [], []
        generated = {row["path"]: row for rows in state.get("generated", {}).values() for row in rows
                     if isinstance(row, dict) and row.get("path")}
        jobs = {row["id"]: row for row in state.get("jobs", []) if row.get("id")}
        producing = {}
        producer_ids = set()
        producer_tb = {}
        sources = []
        for path in files:
            try:
                relative = path.relative_to(root).as_posix()
                source = path
                producer = jobs.get(generated.get(relative, {}).get("job_id")) if job is None else job
                if origin != "shared" and producer and producer.get("status") in ("succeeded", "partial"):
                    immutable = self.runs / state["id"] / "jobs" / producer["id"] / "products" / relative
                    if immutable.is_file():
                        source = immutable
                        producer_ids.add(producer["id"])
                        if producer.get("acquisition"):
                            reference = producer["acquisition"]
                            producing[reference.get("sha256", str(reference.get("path")))] = reference
                        if producer.get("tb_version"):
                            package = producer["tb_version"]
                            producer_tb[package.get("sha256", str(package.get("path")))] = package
                stat = source.stat()
            except (OSError, ValueError):
                continue
            records.append({"path": str(source), "relative": relative, "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns})
            fingerprint.append((relative, str(source), stat.st_size, stat.st_mtime_ns))
            sources.append(source)
        if not records:
            return None
        version = (job or {}).get("id") or version
        identity = json.dumps([str(root), layer, version, fingerprint], separators=(",", ":"))
        identity = hashlib.sha256(identity.encode()).hexdigest()[:24]
        channels, observations, contract = self._coverage(sources, layer)
        modified = max(row["mtime_ns"] / 1e9 for row in records)
        reference = None
        if origin != "shared":
            if job:
                reference = job.get("acquisition")
            elif len(producing) == 1:
                reference = next(iter(producing.values()))
            elif not producer_ids and layer not in ("TB_L2", "EC_L3"):
                reference = state.get("acquisition")
        profile = None
        if isinstance(reference, dict) and reference.get("path"):
            profile = self._read(Path(reference["path"]))
        if job or producer_ids:
            contract = (profile or {}).get("analysis_contract") or contract
        aliases = {}
        naming_reference = state.get("acquisition") or {}
        naming_profile = self._read(Path(naming_reference["path"])) if naming_reference.get("path") else profile
        if layer in ("TB_L0", "EC_L0") and naming_profile and layout in ("native", "mixed"):
            for path in sources:
                if path.name == "events.parquet":
                    node = path.parent
                    observation = _native_observation(node, naming_profile, self._read(node / "meta.json") or {})
                    if observation["stem"] != node.name:
                        aliases[node.name] = observation["stem"]
            observations = sorted({aliases.get(item, item) for item in observations})
        current = origin != "shared"
        name = state.get("name") if current else "共享产品"
        created_at = (job or {}).get("finished_at") or _stamp(modified)
        tb_version = None
        if origin != "shared" and layer in ("EC_L1", "EC_L2"):
            if job:
                tb_version = job.get("tb_version")
            elif len(producer_tb) == 1:
                tb_version = next(iter(producer_tb.values()))
            elif not producer_tb:
                tb_version = state.get("ec_dependency")
        return {
            "id": identity, "layer": layer, "origin": origin,
            "run_id": state.get("id") if current else None, "run_name": name,
            "label": f"{name or payload} · {version}", "version": version,
            "payload": state.get("payload", payload), "source_payload": _source_payload(state),
            "storage_payload": payload,
            "created_at": created_at, "product_root": str(root), "layer_path": str(directory),
            "files": records, "channels": channels, "observations": observations,
            **({"observation_aliases": aliases} if aliases else {}),
            "analysis_contract": contract, "layout": layout,
            "acquisition": copy.deepcopy(reference),
            **({"producer_acquisitions": copy.deepcopy(list(producing.values()))} if len(producing) > 1 else {}),
            **({"producer_job_ids": sorted(producer_ids)} if producer_ids else {}),
            **({"tb_version": copy.deepcopy(tb_version)} if tb_version else {}),
            **({"tb_versions": copy.deepcopy(list(producer_tb.values()))} if len(producer_tb) > 1 else {}),
            **({"profile": copy.deepcopy(profile)} if profile else {}),
            **({"job_id": job["id"]} if job else {}),
        }

    def _enumerate(self, state, layer):
        if layer not in LAYERS:
            raise ValueError(f"Unknown layer: {layer}")
        found = []
        dataset = state.get("dataset") or {}
        shared = Path(dataset["product_dir"]) if dataset.get("product_dir") else self.workspace / "products" / state["payload"]
        source_payload = _source_payload(state)
        # Dataset product_dir is a payload root; default products follows the same layout.
        roots = [] if dataset and not dataset.get("product_dir") else [(shared.parent, shared.name, state, "shared", None)]
        saved = {state["id"]: state}
        for path in sorted(self.runs.glob("*/state.json")):
            other = self._read(path)
            if not isinstance(other, dict) or other.get("id") != path.parent.name:
                continue
            if _source_payload(other) != source_payload:
                continue
            saved.setdefault(other["id"], other)
        for run_id, other in saved.items():
            if _source_payload(other) != source_payload:
                continue
            origin = "current" if run_id == state["id"] else "task"
            root = self.runs / run_id
            roots.append((root / "products", other["payload"], other, origin, None))
            for job in other.get("jobs", []):
                if job.get("status") != "succeeded" or job.get("layer") != layer or not job.get("id"):
                    continue
                roots.append((root / "jobs" / job["id"] / "products", other["payload"], other, origin, job))
        seen = set()
        for root, payload, owner, origin, job in roots:
            for files, directory, layout, version in self._bundles(root, payload, layer):
                if job and layer in ("TB_L2", "EC_L3"):
                    outputs = {str(row.get("path")) if isinstance(row, dict) else str(row)
                               for row in job.get("outputs", [])}
                    produced_models = [path for path in files if path.suffix == ".json"
                                       and _model_json(self._read(path), layer)
                                       and path.relative_to(root).as_posix() in outputs]
                    if not produced_models:
                        continue
                candidate = self._candidate(owner, root, payload, layer, files, directory, layout, version,
                                            origin=origin, job=job)
                if candidate and candidate["id"] not in seen:
                    seen.add(candidate["id"])
                    found.append(candidate)
        return sorted(found, key=lambda row: (row["created_at"], row["id"]), reverse=True)

    def candidates(self, state, layer, *, refresh=False):
        if layer not in LAYERS:
            raise ValueError(f"Unknown layer: {layer}")
        key = (state["id"], layer, state.get("updated_at"), state.get("config_revision"),
               json.dumps(state.get("dataset"), sort_keys=True))
        cached = self._candidate_cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < 5.:
            return copy.deepcopy(cached[1])
        rows = self._enumerate(state, layer)
        if len(self._candidate_cache) > 128:
            self._candidate_cache.clear()
        self._candidate_cache[key] = (time.monotonic(), rows)
        if len(self._known_candidates) > 256:
            self._known_candidates.clear()
        for candidate in rows:
            self._known_candidates[(state["id"], layer, candidate["id"])] = candidate
        return copy.deepcopy(rows)

    def get(self, state, layer, candidate_id):
        selected = self._known_candidates.get((state["id"], layer, candidate_id))
        if selected:
            try:
                for record in selected["files"]:
                    actual = Path(record["path"]).stat()
                    if (actual.st_size, actual.st_mtime_ns) != (record["size"], record["mtime_ns"]):
                        self.invalidate()
                        self._known_candidates.pop((state["id"], layer, candidate_id), None)
                        raise ValueError("所选结果已变化，请重新选择")
            except OSError as exc:
                self.invalidate()
                self._known_candidates.pop((state["id"], layer, candidate_id), None)
                raise ValueError("所选结果已变化或不存在，请重新选择") from exc
            return copy.deepcopy(selected)
        for candidate in self.candidates(state, layer, refresh=True):
            if candidate["id"] == candidate_id:
                return candidate
        raise ValueError("所选结果已变化或不存在，请重新选择")


def _stage_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if source.suffix.lower() == ".json" or source.name == "spectra.parquet":
        # Saved binned spectra are small result snapshots. Unlike raw event
        # tables, current-task spectra may be overwritten by a later fit.
        shutil.copy2(source, target)
    else:
        try:
            target.symlink_to(source.resolve())
        except OSError:
            # Windows development machines may not enable user symlinks.
            shutil.copy2(source, target)


def _file_identities(value):
    """Match recorded acquisition paths without touching raw event files."""
    if isinstance(value, str):
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        yield name
        yield name.removesuffix(".cut").removesuffix(".event.dat").removesuffix(".dat")
    elif isinstance(value, list):
        for row in value:
            yield from _file_identities(row)
    elif isinstance(value, dict):
        for key in ("path", "source", "event", "event_path", "event_file", "sci", "file", "filename", "id"):
            if key in value:
                yield from _file_identities(value[key])


def _observation_stem(row):
    if row.get("stem"):
        return row["stem"]
    if row.get("kind") == "xray" and row.get("energy_keV") is not None and row.get("channel") is not None:
        return f"{float(row['energy_keV']):g}keV_ch{int(row['channel'])}"
    event = str(row.get("event") or row.get("acquisition_id") or "").replace("\\", "/").rsplit("/", 1)[-1]
    identity = str(row.get("acquisition_id") or event).removesuffix(".cut").removesuffix(".event.dat").removesuffix(".dat")
    if row.get("kind") in ("source", "background"):
        source = row.get("source_name") or row.get("source") or "bkg"
        return f"src_{source}_{identity}"
    return event.removesuffix(".cut").removesuffix(".event.dat").removesuffix(".dat")


def _native_observation(node, profile, meta):
    observation = copy.deepcopy(meta.get("observation") or meta.get("acquisition", {}).get("observation") or {})
    names = set(_file_identities(node.name))
    for key in ("inputs", "source", "event", "event_path", "event_file", "sci", "acquisition_id"):
        names.update(_file_identities(meta.get(key)))
        names.update(_file_identities(observation.get(key)))
    matches = []
    family = node.parent.parent.name
    for row in (profile or {}).get("discovery", {}).get("manifest", []):
        if family in ("tb", "ec") and (row.get("kind") == "tb") != (family == "tb"):
            continue
        identity = set(_file_identities(row.get("event"))) | set(_file_identities(row.get("acquisition_id")))
        identity.update(_file_identities(row.get("stem")))
        if names & identity:
            matches.append(row)
    if len(matches) > 1:
        exact = [row for row in matches if row.get("stem") == node.name or str(row.get("event", "")).replace("\\", "/").rsplit("/", 1)[-1] == node.name]
        matches = exact if len(exact) == 1 else matches
    if len(matches) > 1:
        raise ValueError(f"{node.name}: L0 文件对应多条读取映射，请明确采集配对")
    if matches:
        observation.update(copy.deepcopy(matches[0]))
    observation["stem"] = _observation_stem(observation) or node.name.removesuffix(".event.dat").removesuffix(".dat")
    if not observation.get("source_name"):
        match = re.match(r"src_([A-Za-z]+\d+)_", node.name)
        if match and not node.name.startswith("src_bkg_"):
            observation["source_name"] = match[1]
    if not observation.get("energy_keV"):
        match = re.match(r"([\d.]+)keV(?:_|$)", node.name)
        if match:
            observation["energy_keV"] = float(match[1])
    return observation


def _materialize_native_l0(candidate, stage, target_root, payload, profile, scope=None):
    from ..common.legacy_parser import to_hk_data
    from ..common.native_time import CONTRACT, read_analysis_events
    from ..common.utils import read_parquet, write_parquet

    family = "tb" if candidate["layer"] == "TB_L0" else "ec"
    outputs = []
    storage = candidate.get("storage_payload", candidate["source_payload"])
    analysis_profile = profile or candidate.get("profile")
    selected = (scope or {}).get("stems", (scope or {}).get("observations"))
    nodes = []
    for source in sorted((stage / storage / family / "L0").glob("*/events.parquet")):
        node = source.parent
        meta = _json(node / "meta.json") or {}
        observation = _native_observation(node, analysis_profile, meta)
        nodes.append((source, node, meta, observation))
    selected_nodes = [row for row in nodes if selected is None or row[3]["stem"] in selected or row[1].name in selected]
    background_ids = set()
    if family == "ec" and selected is not None:
        for _, _, _, observation in selected_nodes:
            refs = observation.get("background_ids") or observation.get("background_files") or []
            if not refs:
                source_name = observation.get("source_name") or observation.get("source")
                config_key = source_name if source_name else f"{float(observation.get('energy_keV') or 0):g}"
                config = (analysis_profile or {}).get("analysis_config", {}).get("ec_l1", {})
                entry = config.get("source" if source_name else "xray", {}).get(config_key, {})
                refs = entry.get("background_ids") or entry.get("background_files") or []
            background_ids.update(_file_identities(refs))
        for row in nodes:
            observation = row[3]
            identities = set(_file_identities(row[1].name))
            for key in ("event", "acquisition_id", "stem"):
                identities.update(_file_identities(observation.get(key)))
            if identities & background_ids and row not in selected_nodes:
                selected_nodes.append(row)
    for source, node, meta, observation in selected_nodes:
        output = target_root / payload / family / "L0/parquet" / f"{observation['stem']}.l0.parquet"
        if output.exists():
            continue
        native = {**meta, "analysis_contract": CONTRACT, "l0_node": str(node.resolve())}
        acquisition = copy.deepcopy(meta.get("acquisition") or {})
        acquisition.update(
            analysis_contract=CONTRACT, native=native,
            observation=observation,
            input_product_id=candidate["id"],
        )
        if isinstance(candidate.get("acquisition"), dict):
            acquisition.setdefault("profile_sha256", candidate["acquisition"].get("sha256"))
            acquisition.setdefault("revision", candidate["acquisition"].get("revision"))
        frame = read_analysis_events(source)
        if not {"channel", "amp"} <= set(frame.columns):
            raise ValueError(f"{source}: L0 缺少 channel/amp")
        summary = {}
        if (node / "hk.parquet").is_file():
            telemetry = read_parquet(node / "hk.parquet")
            channels = {}
            for channel in range(4):
                temperature = telemetry.get(f"sipm_temp_ch{channel}")
                bias = telemetry.get(f"bias_ch{channel}")
                if temperature is None or bias is None:
                    continue
                channels[str(channel)] = {
                    "temp": float(temperature.mean()), "bias": float(bias.mean()),
                    "temp_err": float(temperature.std(ddof=1)) if len(temperature) > 1 else 0.,
                    "bias_err": float(bias.std(ddof=1)) if len(bias) > 1 else 0.,
                    "temp_n": len(temperature), "bias_n": len(bias),
                }
            if len(channels) == 4:
                summary = to_hk_data(channels)
        write_parquet(output, frame, schema_metadata={"native": native, "acquisition": acquisition,
                                                     **({"hk_data": summary} if summary else {})})
        outputs.append(str(output))
    return outputs, sorted({row[3]["stem"] for row in selected_nodes})


def tb_aliases(profile):
    """Return known TB analysis IDs mapped to their acquisition stems."""
    aliases = {}
    for row in (profile or {}).get("discovery", {}).get("manifest", []):
        if row.get("kind") != "tb":
            continue
        alias, canonical = row.get("analysis_id"), row.get("stem")
        if not isinstance(alias, str) or not isinstance(canonical, str) or alias == canonical:
            continue
        if any(Path(value).name != value or value in (".", "..") for value in (alias, canonical)):
            raise ValueError("TB 观测标识需要为文件名")
        if alias in aliases and aliases[alias] != canonical:
            raise ValueError(f"{alias}: TB 分析标识对应多个采集，请明确文件映射")
        aliases[alias] = canonical
    return aliases


def normalize_tb_aliases(product_root, payload, profile):
    """Normalize private TB fit indexes to their known acquisition stems.

    Native analysis nodes and their saved spectra keep their original names.
    When a canonical compatibility file already exists, its channel records
    take priority over imported aliases.
    """
    root = Path(product_root) / payload
    directory = root / "tb/L1/json"
    aliases = tb_aliases(profile)
    for alias, canonical in aliases.items():
        source = directory / f"{alias}_fit_results.l1.json"
        target = directory / f"{canonical}_fit_results.l1.json"
        if not source.is_file():
            continue
        if target.is_file():
            incoming, existing = _json(source), _json(target)
            if isinstance(incoming, list) and isinstance(existing, list) and all(
                isinstance(row, dict) and isinstance(row.get("channel"), int)
                for row in incoming + existing
            ):
                rows = {row["channel"]: row for row in incoming}
                rows.update({row["channel"]: row for row in existing})
                merged = [rows[channel] for channel in sorted(rows)]
                if merged != existing:
                    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            source.unlink()
        else:
            source.rename(target)
    # The importer keeps native node IDs separately; only compatibility output
    # paths change when their private JSON files are renamed.
    index_path = root / "contract_import.json"
    index = _json(index_path)
    if isinstance(index, dict) and isinstance(index.get("outputs"), list):
        renamed = []
        for value in index["outputs"]:
            path = Path(value)
            stem = path.name.removesuffix("_fit_results.l1.json")
            renamed.append(str(path.with_name(f"{aliases[stem]}_fit_results.l1.json"))
                           if stem in aliases and path.parent == directory else value)
        if renamed != index["outputs"]:
            index["outputs"] = renamed
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return aliases


def materialize_candidate(candidate, target_root, payload, scope=None, *, profile=None):
    """Stage a selected result for existing processors in a private job root.

    Small JSON is copied and large tables are linked where the host permits it.
    Native tables/analyses are adapted using the library's existing readers and
    compatibility importer. ``scope`` records the intended compute selection;
    whole source bundles remain linked so shared backgrounds stay available.
    Native L0 projections read only selected observations and their backgrounds.
    """
    target_root = Path(target_root).resolve()
    source_root = Path(candidate["product_root"]).resolve()
    if target_root == source_root or source_root in target_root.parents:
        raise ValueError("结果输入需要写入独立任务目录")
    if Path(payload).name != payload or payload in (".", ".."):
        raise ValueError("Invalid payload")
    stage = target_root / "_inputs" / candidate["id"]
    storage = candidate.get("storage_payload", candidate["source_payload"])
    files, outputs, materialized_stems = [], [], []
    compatible = {}
    for record in candidate["files"]:
        relative = Path(record["relative"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != storage:
            raise ValueError("Invalid input product path")
        source = Path(record["path"])
        staged = stage / relative
        _stage_file(source, staged)
        target = target_root / payload / Path(*relative.parts[1:])
        _stage_file(source, target)
        files.append(str(target))
        if target.name.endswith(("_fit_results.l1.json", "_fit_results.l2.json")):
            value = _json(target)
            if isinstance(value, list):
                # A mixed result's compatibility index is the complete current
                # selection. Older native nodes may retain superseded channels.
                compatible[target] = (value, target.read_bytes())
    layer = candidate["layer"]
    if candidate.get("layout") in ("native", "mixed"):
        if layer in ("TB_L0", "EC_L0"):
            native_outputs, materialized_stems = _materialize_native_l0(candidate, stage, target_root, payload, profile, scope)
            outputs += native_outputs
        elif layer in ("TB_L1", "TB_L2", "EC_L2", "EC_L3"):
            from ..common.contract_import import import_contract_products

            imported = import_contract_products(stage / storage, target_root, payload, source_payload=storage)
            outputs += imported["outputs"]
    for target, (value, original) in compatible.items():
        redirected = False
        for row in value:
            if not isinstance(row, dict):
                continue
            for key in ("contract_node", "contract_spectrum", "contract_meta"):
                reference = row.get(key)
                if not isinstance(reference, str):
                    continue
                try:
                    relative_reference = Path(reference).relative_to(source_root)
                except ValueError:
                    continue
                frozen_reference = stage / relative_reference
                if frozen_reference.exists():
                    row[key] = str(frozen_reference)
                    redirected = True
        if redirected:
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            target.write_bytes(original)
    observation_aliases = copy.deepcopy(candidate.get("observation_aliases") or {})
    if layer == "TB_L1":
        aliases = normalize_tb_aliases(target_root, payload, candidate.get("profile") or profile)
        observation_aliases.update(aliases)
        for canonical in aliases.values():
            target = target_root / payload / "tb/L1/json" / f"{canonical}_fit_results.l1.json"
            if target.is_file():
                outputs.append(str(target))
    return {"candidate_id": candidate["id"], "layer": layer, "product_root": str(target_root),
            "files": sorted({path for path in files + outputs if Path(path).is_file()}), "scope": copy.deepcopy(scope),
            "materialized_stems": materialized_stems, "observation_aliases": observation_aliases}


def _correction_evidence(value, channel=None):
    """Walk known provenance fields without traversing saved spectral arrays."""
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                yield from _correction_evidence(row, row.get("channel", channel))
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("tb_params_used"), dict) or isinstance(value.get("channel_models"), dict):
        yield value, channel
        return
    channel_evidence = value.get("channel_evidence")
    if isinstance(channel_evidence, dict) and channel_evidence:
        for key, row in channel_evidence.items():
            yield from _correction_evidence(row, int(key) if str(key).isdigit() else channel)
        return
    for key in ("signal_tb_correction", "background_tb_correction", "corr", "background_corr"):
        child = value.get(key)
        if isinstance(child, dict) and child and all(str(item).isdigit() for item in child):
            for ch, row in child.items():
                yield from _correction_evidence(row, int(ch))
        else:
            yield from _correction_evidence(child, value.get("channel", channel))
    if value and all(str(key).isdigit() for key in value):
        for key, row in value.items():
            yield from _correction_evidence(row, int(key))


def corrected_tb_params(source, *, scope=None):
    """Read the actual TB model behind corrected events or saved peak fits.

    ``source`` is a catalog candidate, materialization result, or product root.
    Event Parquet arrays are never read. The result contains four parameter slots
    (``None`` for unrecorded channels), correction reference metadata and source
    files. ``None`` means no embedded correction evidence was found. Conflicting
    coefficients or reference conditions raise ``ValueError``.
    """
    if isinstance(source, dict):
        files = [Path(row["path"] if isinstance(row, dict) else row) for row in source.get("files", [])]
        scope = scope if scope is not None else source.get("scope")
    else:
        root = Path(source)
        files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    scope = scope or {}
    channels = scope.get("channels")
    stems = scope.get("stems", scope.get("observations"))
    parameters, metadata, missing, seen = {}, [], [], set()
    references = {}
    source_values = {key: set() for key in ("tb_model_sha256", "tb_model", "tb_corrfile")}
    keys = ("G0", "k", "V0", "b", "c")
    for path in files:
        # Copies and symlinks of a table can coexist in a private staging root.
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        name = path.name
        corr_table = path.suffix == ".parquet" and (
            name.endswith("_corr.l1.parquet") or tuple(path.parts[-4:-1]) == ("ec", "L1", "parquet")
        )
        peak_json = path.suffix == ".json" and (
            name.endswith("_fit_results.l2.json") or name in ("points.json", "meta.json")
        )
        if not corr_table and not peak_json:
            continue
        stem = (name.removesuffix("_corr.l1.parquet").removesuffix("_fit_results.l2.json")
                if name not in ("points.json", "meta.json") else path.parent.name)
        if stems and not any(stem == selected or stem.startswith(selected + "_")
                             or selected.startswith(stem + "_") for selected in stems):
            continue
        if corr_table:
            from ..common.utils import read_parquet_metadata

            value = read_parquet_metadata(path, "tb_correction")
        else:
            value = _json(path)
        evidence = list(_correction_evidence(value))
        if corr_table and not evidence:
            missing.append(str(path))
        for row, channel in evidence:
            actual = row.get("tb_params_used") or row.get("channel_models") or {}
            used = []
            for key, values in actual.items():
                if not str(key).isdigit() or not isinstance(values, dict):
                    continue
                ch = int(key)
                if ch not in range(4) or channel is not None and ch != int(channel) or channels is not None and ch not in channels:
                    continue
                try:
                    model = {key: float(values[key]) for key in keys}
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"{path.name}: CH{ch} 的 TB 参数不完整") from exc
                if not all(math.isfinite(value) for value in model.values()):
                    raise ValueError(f"{path.name}: CH{ch} 的 TB 参数不是有限数值")
                if ch in parameters and parameters[ch] != model:
                    raise ValueError(f"所选产品 CH{ch} 使用了不同的 TB 参数")
                parameters[ch] = model
                used.append(ch)
            if not used:
                continue
            for key in ("ref_temp_C", "ref_bias_V", "mode"):
                if row.get(key) is not None:
                    if key in references and references[key] != row[key]:
                        raise ValueError(f"所选产品的 TB 校正参考条件 {key} 不一致")
                    references[key] = row[key]
            for key in source_values:
                if row.get(key) is not None:
                    source_values[key].add(str(row[key]))
            metadata.append({"path": str(path), "channels": used})
    if not parameters:
        return None
    origin = {"files": sorted({row["path"] for row in metadata}), **references}
    for key, values in source_values.items():
        if len(values) == 1:
            origin[key] = next(iter(values))
        elif values:
            origin[key + "_versions"] = sorted(values)
    return {"params": [parameters.get(ch) for ch in range(4)], "channels": sorted(parameters), "source": origin,
            "complete": not missing, "missing_files": missing}
