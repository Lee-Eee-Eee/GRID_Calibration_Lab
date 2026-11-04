"""Task-local layer inputs, adopted results and scoped update chains."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .acquisition_store import load_profile
from .impacts import configuration_impact, impact_for_layer, merge_impacts, scientific_fingerprint

ORDER = ("TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3")
UPSTREAM = {"TB_L1": "TB_L0", "TB_L2": "TB_L1", "EC_L1": "EC_L0", "EC_L2": "EC_L1", "EC_L3": "EC_L2"}
LABELS = {"TB_L0": "TB EXP/L0", "TB_L1": "TB L1", "TB_L2": "TB L2", "EC_L0": "EC EXP/L0",
          "EC_L1": "EC corr", "EC_L2": "EC L1", "EC_L3": "EC L2"}


def directory(root, payload, layer):
    family, level = layer.split("_")
    return Path(root) / payload / family.lower() / level


def summary(candidate):
    if not candidate:
        return None
    return {key: copy.deepcopy(candidate[key]) for key in (
        "id", "layer", "origin", "run_id", "run_name", "label", "version", "created_at", "channels",
        "observations", "scope", "analysis_contract", "product_root", "path", "tb_version",
    ) if key in candidate}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


class LayerInputRouting:
    """Mixed into Workbench; scientific computation stays in its processors."""

    def _routed_layers(self, state, roots, *, include_roots=True, scopes=None):
        selected, pending = set(), list(roots)
        while pending:
            node = pending.pop()
            if node in selected:
                continue
            selected.add(node)
            for child, parent in UPSTREAM.items():
                if parent != node:
                    continue
                binding = state.get("input_bindings", {}).get(child, {}).get("data")
                if not binding or not self._binding_covers(binding, (scopes or {}).get(node, {})):
                    pending.append(child)
            if node == "TB_L2" and state.get("tb_binding", "follow_run") != "pinned":
                pending.append("EC_L1")
        if not include_roots:
            selected.difference_update(roots)
        return [layer for layer in ORDER if layer in selected]

    @staticmethod
    def _binding_covers(binding, incoming):
        scope = binding.get("scope") or {}
        stems, channels = scope.get("stems"), scope.get("channels")
        if stems is None and channels is None:
            return True
        for selected, affected in ((stems, incoming.get("stems")), (channels, incoming.get("channels"))):
            if selected is not None and (affected is None or not set(affected) <= set(selected)):
                return False
        return True

    def _job_downstream(self, state, job):
        plan = impact_for_layer(job["layer"], stems=job["config"].get("only"),
                                channels=job["config"].get("fit_channels"), state=state)
        return set(self._routed_layers(state, [job["layer"]], include_roots=False, scopes=plan["scopes"]))

    def _route_impact(self, state, impact):
        result = copy.deepcopy(impact)
        roots = impact.get("roots") or [layer for layer in impact["layers"]
                                        if UPSTREAM.get(layer) not in impact["layers"]]
        result["layers"] = self._routed_layers(state, roots, scopes=impact.get("scopes"))
        result["scopes"] = {layer: impact.get("scopes", {}).get(layer, {"stems": None, "channels": None})
                            for layer in result["layers"]}
        result["steps"] = [{"layer": layer, "label": LABELS[layer], "scope": result["scopes"][layer]}
                           for layer in result["layers"]]
        return result

    def _remember_impact(self, state, impact):
        scopes = state.setdefault("dirty_scopes", {})
        dirty = set(state.get("dirty_layers", []))
        pending = set((state.get("last_impact") or {}).get("layers", [])) & dirty
        for layer in impact["layers"]:
            incoming = impact.get("scopes", {}).get(layer, {"stems": None, "channels": None})
            old = scopes.get(layer, {"stems": None, "channels": None}) if layer in dirty else None
            scopes[layer] = {key: (None if old is not None and (old.get(key) is None or incoming.get(key) is None)
                                   else sorted(set((old or {}).get(key) or []) | set(incoming.get(key) or []))
                                   if incoming.get(key) is not None else None)
                             for key in ("stems", "channels")}
        state["dirty_layers"] = [layer for layer in ORDER if layer in dirty | set(impact["layers"])]
        pending.update(impact["layers"])
        layers = [layer for layer in ORDER if layer in pending]
        state["last_impact"] = {**impact, "layers": layers, "scopes": {layer: scopes.get(layer, {}) for layer in layers},
                                "steps": [{"layer": layer, "label": LABELS[layer], "scope": scopes.get(layer, {})}
                                          for layer in layers]}

    def _configuration_impact(self, state, before, after, inventory):
        impact = configuration_impact(before, after, state,
                                      old_inventory=state.get("inventory"), new_inventory=inventory)
        return self._route_impact(state, impact)

    def _invalidate_coverage(self, state, impact):
        pending = set(state.get("acquisition_pending_layers", []))
        for layer in impact["layers"]:
            scope = impact.get("scopes", {}).get(layer) or {}
            stems, channels = scope.get("stems"), scope.get("channels")
            completed = state.setdefault("acquisition_products", {}).get(layer, [])
            state["acquisition_products"][layer] = [item for item in completed if stems and item not in stems]
            if layer == "TB_L1":
                coverage = state.setdefault("acquisition_l1_channels", {})
                for stem in list(coverage):
                    if stems is None or stem in stems:
                        coverage[stem] = [ch for ch in coverage[stem] if channels and ch not in channels]
            if layer == "EC_L2":
                coverage = state.setdefault("acquisition_ec_l2_points", {})
                for energy in list(coverage):
                    if stems is None or energy in stems or any(energy in stem for stem in stems):
                        coverage[energy] = [point for point in coverage[energy]
                                            if channels and int(point.split(":")[-2]) not in channels]
            pending.add(layer)
        state["acquisition_pending_layers"] = [layer for layer in ORDER if layer in pending]

    def _remember_profile_rules(self, state, previous, impact):
        rules = state.setdefault("input_profile_rules", {})
        if previous:
            rules.setdefault(previous["sha256"], {layer: [] for layer in ("TB_L0", "EC_L0", "EC_L1")})
        for prior in rules.values():
            for layer in ("TB_L0", "EC_L0", "EC_L1"):
                if layer not in impact["layers"]:
                    continue
                stems = impact.get("scopes", {}).get(layer, {}).get("stems")
                prior[layer] = None if stems is None or prior.get(layer) is None else sorted(set(prior[layer]) | set(stems))

    def _accepted_input_profiles(self, state, profile):
        key = (profile.get("_sha256"), tuple(ref["sha256"] for ref in state.get("prior_acquisitions", [])))
        cache = getattr(self, "_input_profile_cache", {})
        if key in cache:
            return cache[key]
        accepted = {layer: [profile.get("_sha256")] for layer in ORDER}
        for reference in state.get("prior_acquisitions", []):
            previous = load_profile(reference)
            for layer in ORDER:
                if scientific_fingerprint(previous, layer) == scientific_fingerprint(profile, layer):
                    accepted[layer].append(reference["sha256"])
        if len(cache) > 24:
            cache.clear()
        cache[key] = accepted
        self._input_profile_cache = cache
        return accepted

    def _interactive_impact(self, state, before, patch):
        plans = []
        windows = patch.get("windows", {})
        changed = [(stem, int(ch)) for stem, channels in windows.items() for ch, value in channels.items()
                   if before.get("windows", {}).get(stem, {}).get(ch) != value]
        if changed:
            plans.append(impact_for_layer("TB_L1", stems=sorted({stem for stem, _ in changed}),
                                          channels=sorted({ch for _, ch in changed}), state=state, label="TB 峰窗已修改"))
        if state["config"].get("excluded") != before.get("excluded"):
            changed_ids = set(state["config"].get("excluded", [])) ^ set(before.get("excluded", []))
            plans.append(impact_for_layer("TB_L2", channels=sorted({int(item.rsplit(":", 1)[1]) for item in changed_ids}),
                                          state=state, label="TB 工况屏蔽已修改"))
        ec_windows = patch.get("ec_windows", {})
        points = [point for point, value in ec_windows.items() if before.get("ec_windows", {}).get(point) != value]
        if points:
            rows = (state.get("inventory", {}).get("raw", {}).get("reading_preview") or {}).get("rows", [])
            energies = {point.split(":")[2] for point in points}
            stems = sorted({row["stem"] for row in rows if
                            (f"src_{row.get('source')}" if row.get("kind") == "source" else
                             f"{float(row['energy_keV']):g}keV" if row.get("energy_keV") is not None else "") in energies})
            plans.append(impact_for_layer("EC_L2", stems=stems or None,
                                          channels=sorted({int(point.split(":")[-2]) for point in points}),
                                          state=state, label="EC 峰窗已修改"))
        if state["config"].get("ec_excluded") != before.get("ec_excluded"):
            changed_ids = set(state["config"].get("ec_excluded", [])) ^ set(before.get("ec_excluded", []))
            plans.append(impact_for_layer("EC_L3", channels=sorted({int(item.split(":")[-2]) for item in changed_ids}),
                                          state=state, label="EC 能峰屏蔽已修改"))
        return self._route_impact(state, merge_impacts(*plans))

    def layer_input_summaries(self, state):
        result = {}
        for layer in ORDER:
            slots = []
            parent = UPSTREAM.get(layer)
            if parent:
                binding = state.get("input_bindings", {}).get(layer, {}).get("data")
                selected = summary(binding) if binding else {
                    "layer": parent, "origin": "current", "run_name": state["name"], "label": "本任务当前结果",
                }
                slots.append({"key": "data", "label": LABELS[parent], "layer": parent,
                              "selected": selected, "follow_current": not bool(binding)})
            else:
                raw = (state.get("dataset") or {}).get("raw_dir")
                if not raw:
                    raw = (state.get("inventory", {}).get("raw", {}) or {}).get("root")
                slots.append({"key": "raw", "label": "原始事件 / HK", "layer": "RAW",
                              "selected": {"label": str(raw or "选择数据目录"), "path": raw}, "follow_current": False})
            if layer == "EC_L1":
                package = self.selected_tb(state)
                slots.append({"key": "tb", "label": "TB L2", "layer": "TB_L2",
                              "selected": {**(summary(package) or {}), "label": package.get("filename", "TB L2")}
                              if package else None, "follow_current": state.get("tb_binding") != "pinned"})
            adopted = state.get("adopted_results", {}).get(layer)
            if not adopted:
                latest = next((job for job in reversed(state["jobs"])
                               if job["layer"] == layer and job["status"] == "succeeded"), None)
                if latest:
                    adopted = {"id": latest["id"], "layer": layer, "origin": "current",
                               "run_name": state["name"], "label": f"本任务 · {latest.get('finished_at', latest['created_at'])}",
                               "version": latest["id"], "created_at": latest.get("finished_at", latest["created_at"])}
            result[layer] = {"layer": layer, "slots": slots, "selected_result": summary(adopted),
                             "dirty_scope": state.get("dirty_scopes", {}).get(layer),
                             "tb_binding": state.get("tb_binding", "follow_run")}
        return result

    def layer_inputs(self, run_id, layer):
        if layer not in ORDER:
            raise ValueError("未知处理层")
        state = self.read(run_id)
        catalog = self.layer_product_catalog
        result = self.layer_input_summaries(state)[layer]
        def public(candidate):
            return {**summary(candidate), "files": [{key: row[key] for key in ("relative", "size", "path") if key in row}
                                                    for row in candidate.get("files", [])]}
        for slot in result["slots"]:
            slot["candidates"] = [public(item) for item in catalog.candidates(state, slot["layer"])] if slot["key"] != "raw" else []
        result["results"] = [public(item) for item in catalog.candidates(state, layer)]
        return result

    @staticmethod
    def _input_scope(candidate, body):
        requested = body.get("scope") or {}
        observations = requested.get("observations", requested.get("stems"))
        channels = requested.get("channels")
        known = candidate.get("observations", []) if candidate else []
        if observations is not None and (not isinstance(observations, list) or not observations
                                         or any(item not in known for item in observations)):
            raise ValueError("请选择产品中存在的观测")
        if channels is not None and (not isinstance(channels, list) or not channels
                                     or any(isinstance(ch, bool) or ch not in range(4) for ch in channels)):
            raise ValueError("请选择有效物理通道")
        return {"stems": sorted(set(observations)) if observations is not None else None,
                "channels": sorted(set(channels)) if channels is not None else None}

    def preview_layer_input(self, run_id, body):
        state = self.read(run_id)
        layer = body.get("layer")
        if layer not in ORDER:
            raise ValueError("未知处理层")
        mode, slot = body.get("mode", "input"), body.get("slot", "data")
        if mode not in ("input", "result") or slot not in ("data", "tb"):
            raise ValueError("输入选择操作无效")
        expected = layer if mode == "result" else "TB_L2" if slot == "tb" and layer == "EC_L1" else UPSTREAM.get(layer)
        if expected is None or (slot == "tb" and layer != "EC_L1"):
            raise ValueError("本层请通过数据目录选择原始输入")
        candidate = None
        if not body.get("follow_current"):
            candidate = self.layer_product_catalog.get(state, expected, body.get("candidate_id"))
        elif mode == "result":
            raise ValueError("采用已有结果需要指定结果版本")
        scope = self._input_scope(candidate, body)
        if layer == "EC_L1":
            scope["channels"] = None  # A corrected event product is a whole-file result.
        if mode == "result" and layer in ("TB_L2", "EC_L3") and any(value is not None for value in scope.values()):
            raise ValueError("模型结果按整包采用；通道范围请在计算时选择")
        probe = copy.deepcopy(state)
        if slot == "tb":
            probe["tb_binding"] = "follow_run" if body.get("follow_current") else "pinned"
        roots = [layer]
        scoped = impact_for_layer(layer, stems=scope["stems"], channels=scope["channels"], state=probe)
        layers = self._routed_layers(probe, roots, include_roots=mode != "result", scopes=scoped["scopes"])
        same = candidate and (state.get("input_bindings", {}).get(layer, {}).get(slot, {}).get("id") == candidate["id"])
        if same and state.get("input_bindings", {}).get(layer, {}).get(slot, {}).get("scope") == scope:
            layers = []
        scopes = {item: scoped["scopes"].get(item, {"stems": None, "channels": scope["channels"]}) for item in layers}
        return {"layers": layers, "roots": roots, "scopes": scopes,
                "steps": [{"layer": item, "label": LABELS[item], "scope": scopes[item]} for item in layers],
                "changes": [{"label": "采用已有结果" if mode == "result" else "更换输入", "layers": layers}],
                "message": "复用所选结果" if mode == "result" else "更新使用该输入的处理层", "scope": scope,
                "candidate": summary(candidate)}

    def _freeze_layer_product(self, state, candidate, scope):
        from .layer_products import materialize_candidate

        root = self.root(state["id"]) / "inputs" / uuid.uuid4().hex[:12] / "products"
        profile = load_profile(state["acquisition"]) if state.get("acquisition") else None
        staged = materialize_candidate(candidate, root, state["payload"], scope=scope, profile=profile)
        aliases = staged.get("observation_aliases", {})
        scope = copy.deepcopy(scope)
        if scope.get("stems"):
            scope["stems"] = sorted({aliases.get(stem, stem) for stem in scope["stems"]})
        dependency_stems = set(staged.get("materialized_stems", []))
        if profile and scope.get("stems"):
            rows = profile.get("discovery", {}).get("manifest", [])
            background_ids = {str(value) for row in rows if row.get("stem") in scope["stems"]
                              for value in row.get("background_ids", [])}
            for row in rows:
                identifiers = {str(row.get("acquisition_id", "")), str(row.get("event", "")),
                               Path(str(row.get("event", ""))).name}
                if any(value in identifiers or Path(value).name in identifiers for value in background_ids):
                    dependency_stems.add(row["stem"])
        binding = {**copy.deepcopy(candidate), "scope": scope, "frozen_root": str(root),
                   "observation_aliases": aliases,
                   "observations": sorted({aliases.get(stem, stem) for stem in candidate.get("observations", [])}),
                   "materialized_stems": sorted(dependency_stems)}
        _write(root.parent / "input.json", binding)
        return binding

    def _copy_input_layer(self, binding, output, payload, *, replace=False):
        layer, scope = binding["layer"], binding.get("scope", {})
        source = Path(binding["frozen_root"])
        folder = "parquet" if layer in ("TB_L0", "EC_L0", "EC_L1") else "json"
        target = directory(output, payload, layer) / folder
        if replace and scope.get("stems") is None and scope.get("channels") is None and target.is_dir():
            # This directory is always an isolated task/job directory.
            if not target.resolve().is_relative_to(self.runs.resolve()):
                raise ValueError("输入目标应位于当前任务目录")
            shutil.rmtree(target)
        elif replace and scope.get("stems") is None and scope.get("channels") and folder == "json":
            # A channel selection replaces its complete point set, including
            # observations absent from the chosen source. Other channels stay.
            for path in target.glob("*_fit_results.*.json"):
                rows = _read(path)
                if isinstance(rows, list):
                    retained = [row for row in rows if row.get("channel") not in scope["channels"]]
                    if retained != rows:
                        _write(path, retained)
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if (layer in ("TB_L1", "EC_L2") and any(value is not None for value in scope.values())
                    and len(relative.parts) >= 4 and relative.parts[0] == payload
                    and relative.parts[2] == "L1" and relative.parts[3] not in ("json", "figures", "check")):
                # Selected compatibility rows point to their frozen native
                # spectra. Keep native nodes used by retained rows intact.
                continue
            destination = Path(output) / relative
            suffix = {"TB_L0": ".l0.parquet", "EC_L0": ".l0.parquet", "EC_L1": "_corr.l1.parquet",
                      "TB_L1": "_fit_results.l1.json", "EC_L2": "_fit_results.l2.json"}.get(layer)
            if scope.get("stems") and suffix and path.name.endswith(suffix):
                stem = path.name.removesuffix(suffix)
                selected = {Path(str(value)).name.removesuffix("_corr.l1.parquet").removesuffix(".l0.parquet")
                            for value in [*scope["stems"], *binding.get("materialized_stems", [])]}
                matches = stem in selected or (layer == "EC_L2" and any(value.startswith(stem + "_") for value in selected))
                if not matches and not stem.startswith("src_bkg_"):
                    continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                destination.unlink()
            channels = scope.get("channels")
            if channels and path.suffix == ".json" and path.name.endswith(("_fit_results.l1.json", "_fit_results.l2.json")):
                incoming = _read(path)
                previous = _read(destination) if destination.is_file() else []
                if isinstance(incoming, list) and isinstance(previous, list):
                    rows = [row for row in previous if row.get("channel") not in channels]
                    rows.extend(row for row in incoming if row.get("channel") in channels)
                    _write(destination, rows)
                    continue
            if path.suffix == ".parquet":
                if destination.exists():
                    destination.unlink()
                try:
                    destination.symlink_to(path.resolve())
                except OSError:
                    shutil.copy2(path, destination)
            else:
                shutil.copy2(path, destination)

    def apply_layer_input(self, run_id, body):
        if body.get("action", "apply") not in ("apply", "apply_and_update"):
            raise ValueError("未知输入应用操作")
        with self.lock:
            state = self.read(run_id)
            if any(job["status"] in ("queued", "running") for job in state["jobs"]):
                raise ValueError("当前计算完成后可更换输入")
            impact = self.preview_layer_input(run_id, body)
            layer, slot, mode = body["layer"], body.get("slot", "data"), body.get("mode", "input")
            if body.get("follow_current"):
                state.setdefault("input_bindings", {}).setdefault(layer, {}).pop(slot, None)
                if slot == "tb":
                    state["tb_binding"] = "follow_run"
            else:
                expected = layer if mode == "result" else "TB_L2" if slot == "tb" else UPSTREAM[layer]
                candidate = self.layer_product_catalog.get(state, expected, body["candidate_id"])
                binding = self._freeze_layer_product(state, candidate, impact["scope"])
                if slot == "tb" and mode == "input":
                    from .dependencies import snapshot, tb_package

                    files = list(Path(binding["frozen_root"]).rglob("*_TB*.json"))
                    files += list(Path(binding["frozen_root"]).rglob("*_temp_bias_fit.json"))
                    if not files:
                        raise ValueError("所选产品没有 TB L2 参数")
                    state["selected_tb"] = snapshot(tb_package(files[-1]), self.root(run_id))
                    state["tb_binding"] = "pinned"
                    state.setdefault("input_bindings", {}).setdefault(layer, {})[slot] = binding
                elif mode == "result":
                    self._copy_input_layer(binding, self.root(run_id) / "products", state["payload"], replace=True)
                    state.setdefault("adopted_results", {})[layer] = binding
                    self._mark_adopted_coverage(state, layer, binding)
                    self._clear_adopted_scope(state, layer, binding)
                else:
                    state.setdefault("input_bindings", {}).setdefault(layer, {})[slot] = binding
            self._remember_impact(state, impact)
            state["config_revision"] += 1
            state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            revision = self.root(run_id) / "revisions" / f"input-{state['config_revision']:04d}.json"
            _write(revision, {"operation": body, "impact": impact,
                              "input_bindings": state.get("input_bindings", {}),
                              "adopted_results": state.get("adopted_results", {})})
            self._saved_operation(state, "采用已有结果" if mode == "result" else "更换分层输入",
                                  [str(revision.relative_to(self.root(run_id)))], layer)
            if body.get("action", "apply") == "apply_and_update" and impact["steps"]:
                state["update_chain"] = {"status": "running", "steps": impact["steps"], "position": 0}
            self.save(state)
            self.layer_product_catalog.invalidate()
        jobs = []
        if body.get("action") == "apply_and_update":
            job = self._advance_update_chain(run_id)
            if job:
                jobs.append(job)
        return {"run": self.view(run_id, compact=True), "impact": impact, "jobs": jobs}

    def _clear_adopted_scope(self, state, layer, binding):
        scope = binding.get("scope") or {}
        stems, channels = scope.get("stems"), scope.get("channels")
        key = {"TB_L1": "pending_windows", "EC_L2": "pending_ec_windows"}.get(layer)
        if key:
            remaining = []
            for point in state.get(key, []):
                pieces = point.split(":")
                stem, channel = (pieces[-2], int(pieces[-1])) if layer == "TB_L1" else (pieces[2], int(pieces[-2]))
                if (stems is not None and stem not in stems) or (channels is not None and channel not in channels):
                    remaining.append(point)
            state[key] = remaining
            if remaining:
                return
        if stems is not None or channels is not None:
            dirty = state.get("dirty_scopes", {}).get(layer)
            if dirty is None or (dirty.get("channels") is None and channels is not None):
                return
            if dirty.get("channels") and channels and set(dirty["channels"]) - set(channels):
                return
            if dirty.get("stems") and stems and set(dirty["stems"]) - set(stems):
                return
        state["dirty_layers"] = [item for item in state["dirty_layers"] if item != layer]
        state.setdefault("dirty_scopes", {}).pop(layer, None)

    def _mark_adopted_coverage(self, state, layer, binding):
        completed = state.setdefault("acquisition_products", {})
        observations = binding.get("observations") or []
        scope = binding.get("scope") or {}
        partial = scope.get("stems") is not None or scope.get("channels") is not None
        previous = set(completed.get(layer, []))
        if layer in ("TB_L2", "EC_L3"):
            completed[layer] = ["aggregate"]
        else:
            suffix = {"TB_L0": ".l0.parquet", "EC_L0": ".l0.parquet", "EC_L1": "_corr.l1.parquet",
                      "TB_L1": "_fit_results.l1.json", "EC_L2": "_fit_results.l2.json"}[layer]
            completed[layer] = sorted({path.name.removesuffix(suffix)
                                       for path in directory(binding["frozen_root"], state["payload"], layer).rglob("*")
                                       if path.name.endswith(suffix)}) or observations
        if partial:
            selected = set(scope.get("stems") or completed[layer])
            completed[layer] = sorted(previous | selected)
            if layer == "TB_L1":
                coverage = state.setdefault("acquisition_l1_channels", {})
                for stem in selected:
                    coverage[stem] = sorted(set(coverage.get(stem, [])) | set(scope.get("channels") or binding.get("channels", [])))
                completed[layer] = [stem for stem in completed[layer] if set(coverage.get(stem, [])) >= {0, 1, 2, 3}]
        else:
            state["acquisition_pending_layers"] = [item for item in state.get("acquisition_pending_layers", []) if item != layer]
        files = []
        for path in Path(binding["frozen_root"]).rglob("*"):
            if path.is_file():
                relative = path.relative_to(binding["frozen_root"]).as_posix()
                files.append({"path": relative, "source": "adopted", "input_id": binding["id"]})
        state.setdefault("generated", {})[layer] = files
        if layer == "TB_L2":
            from .dependencies import tb_package

            models = list(Path(binding["frozen_root"]).rglob("*_TB*.json"))
            models += list(Path(binding["frozen_root"]).rglob("*_temp_bias_fit.json"))
            if models:
                state["tb_version"] = tb_package(models[-1])
        if layer == "EC_L1":
            state["ec_dependency"] = self._producer_tb(state, binding)
            state["ec_scope"] = "all" if binding.get("scope", {}).get("stems") is None else "selected"

    def _producer_tb(self, state, binding):
        from .dependencies import snapshot, tb_package
        from .layer_products import corrected_tb_params

        recorded = binding.get("tb_version")
        if recorded and recorded.get("path") and Path(recorded["path"]).is_file():
            return snapshot(tb_package(Path(recorded["path"])), self.root(state["id"]))
        evidence = corrected_tb_params(Path(binding["frozen_root"]), scope=binding.get("scope"))
        if not evidence:
            return None
        rows = evidence["params"]
        if all(isinstance(row, dict) for row in rows):
            target = Path(binding["frozen_root"]).parent / "producer-TB.json"
            _write(target, rows)
            package = tb_package(target)
            package["input_origin"] = "corrected_spectrum_metadata"
            return snapshot(package, self.root(state["id"]))
        return None

    def _products_for_layer(self, state, layer, channel=None):
        binding = state.get("input_bindings", {}).get(layer, {}).get("data")
        root = self.root(state["id"])
        if not binding:
            return root / "products"
        channels = binding.get("scope", {}).get("channels")
        if channel is not None and channels is not None and channel not in channels:
            return root / "products"
        token = str(state.get("config_revision", 0)) + "-" + str(len(state.get("jobs", [])))
        token += "-" + str(sum(job.get("status") == "succeeded" for job in state.get("jobs", [])))
        output = root / "input-views" / layer / token / "products"
        marker = output.parent / "ready.json"
        with self.lock:
            if not marker.is_file():
                for source in (root / "products").rglob("*"):
                    if not source.is_file():
                        continue
                    target = output / source.relative_to(root / "products")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.suffix == ".json":
                        shutil.copy2(source, target)
                    else:
                        try:
                            target.symlink_to(source.resolve())
                        except OSError:
                            shutil.copy2(source, target)
                self._copy_input_layer(binding, output, state["payload"], replace=True)
                _write(marker, {"input": binding["id"]})
        return output

    @staticmethod
    def _normalize_tb_settings(state, profile):
        from .layer_products import tb_aliases

        aliases = tb_aliases(profile)
        if not aliases:
            return
        config = state["config"]
        windows = config.get("windows", {})
        for alias, canonical in aliases.items():
            if alias in windows:
                previous = windows.pop(alias)
                windows[canonical] = {**previous, **windows.get(canonical, {})}
        def point_id(value):
            pieces = value.split(":")
            if len(pieces) == 3:
                pieces[1] = aliases.get(pieces[1], pieces[1])
            return ":".join(pieces)
        config["excluded"] = sorted({point_id(value) for value in config.get("excluded", [])})
        state["pending_windows"] = sorted({point_id(value) for value in state.get("pending_windows", [])})
        if config.get("only"):
            config["only"] = sorted({aliases.get(value, value) for value in config["only"]})

    def _prepare_layer_inputs(self, state, job, output):
        binding = job.get("input_bindings", {}).get("data")
        if binding:
            self._copy_input_layer(binding, output, state["payload"], replace=True)
        _write(Path(output).parent / "inputs.json", {"layer": job["layer"],
                                                    "bindings": job.get("input_bindings", {}),
                                                    "tb": job.get("tb_version")})

    def _computation_identity(self, state, job, output, profile=None):
        from .result_reuse import compute_key, find_matching_result

        layer = job["layer"]
        if not getattr(self, "_computation_build", None):
            package = Path(__file__).resolve().parents[1]
            files = [path for name in ("common", "config", "tb", "ec") for path in (package / name).rglob("*.py")]
            files += [package / "workbench" / name for name in ("compute.py", "impacts.py", "result_reuse.py", "input_routing.py", "layer_products.py")]
            files += list((package / "resources").glob("*.json"))
            digest = hashlib.sha256()
            for path in sorted(files):
                digest.update(path.relative_to(package).as_posix().encode())
                digest.update(path.read_bytes())
            self._computation_build = digest.hexdigest()
        identities = [{"implementation": self._computation_build}]
        parent = UPSTREAM.get(layer)
        binding = job.get("input_bindings", {}).get("data")
        if binding:
            identities.append({"binding": binding["id"], "scope": binding.get("scope")})
        elif parent:
            folder = "parquet" if parent in ("TB_L0", "EC_L0", "EC_L1") else "json"
            generated = {row["path"]: row for row in state.get("generated", {}).get(parent, [])}
            for path in sorted((directory(output, state["payload"], parent) / folder).glob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(output).as_posix()
                known = generated.get(relative, {}).get("sha256")
                if path.suffix == ".json":
                    known = hashlib.sha256(path.read_bytes()).hexdigest()
                identities.append({"relative": relative, "sha256": known} if known else
                                  {"path": str(self.root(state["id"]) / "products" / relative),
                                   "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns})
        else:
            rows = (state.get("inventory", {}).get("raw", {}).get("reading_preview") or {}).get("rows", [])
            for row in rows:
                if (row.get("kind") == "tb") != layer.startswith("TB"):
                    continue
                if job["config"].get("only") and row["stem"] not in job["config"]["only"]:
                    continue
                for key in ("event", "hk"):
                    source = Path(row[key])
                    if source.is_file():
                        identities.append({"path": str(source), "size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns})
        if job["config"].get("fit_channels") and layer in ("TB_L2", "EC_L3"):
            identities.append({"aggregate_seed": [
                {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted((directory(output, state["payload"], layer) / "json").glob("*.json"))]})
        if layer in ("TB_L1", "EC_L2") and (job["config"].get("ec_point_id") or
                len(job["config"].get("fit_channels", [0, 1, 2, 3])) < 4):
            identities.append({"retained_peak_seed": [
                {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted((directory(output, state["payload"], layer) / "json").glob("*_fit_results.*.json"))]})
        if profile is None:
            profile = load_profile(job["acquisition"]) if job.get("acquisition") else {}
        key = compute_key(layer, job["config"], profile, identities, job.get("tb_version"))
        return key, find_matching_result(state, key, layer)

    def _advance_update_chain(self, run_id):
        with self.lock:
            state = self.read(run_id)
            chain = state.get("update_chain") or {}
            if chain.get("status") != "running":
                return None
            if any(job["status"] in ("queued", "running") for job in state["jobs"]):
                return None
            previous = next((job for job in state["jobs"] if job["id"] == chain.get("current_job_id")), None)
            if previous and previous["status"] != "succeeded":
                chain.update(status="failed", error=previous.get("error") or "上游未完成")
                self.save(state)
                return None
            position = chain.get("position", 0)
            if position >= len(chain["steps"]):
                chain["status"] = "succeeded"
                self.save(state)
                return None
            step = chain["steps"][position]
            scope = step.get("scope") or {}
            spec = {"layer": step["layer"], "representative": False}
            if scope.get("channels") and step["layer"] in ("TB_L1", "TB_L2", "EC_L2", "EC_L3"):
                spec["fit_channels"] = scope["channels"]
            if step["layer"] not in ("TB_L2", "EC_L3"):
                if scope.get("stems"):
                    spec["stems"] = scope["stems"]
            try:
                job = self.submit(run_id, spec)
            except (ValueError, FileNotFoundError, RuntimeError) as exc:
                chain.update(status="failed", error=str(exc))
                self.save(state)
                return None
            # submit persists independently; reload before recording progress.
            fresh = self.read(run_id)
            fresh["update_chain"].update(position=position + 1, current_job_id=job["id"])
            self.save(fresh)
            return job

    def preview_updates(self, run_id):
        state = self.read(run_id)
        impact = copy.deepcopy(state.get("last_impact") or {"layers": [], "scopes": {}, "steps": []})
        layers = [layer for layer in impact["layers"] if layer in state.get("dirty_layers", [])]
        impact["layers"] = layers
        impact["scopes"] = {layer: state.get("dirty_scopes", {}).get(layer, impact.get("scopes", {}).get(layer, {}))
                            for layer in layers}
        impact["steps"] = [{"layer": layer, "label": LABELS[layer], "scope": impact["scopes"][layer]} for layer in layers]
        return impact

    def start_updates(self, run_id):
        with self.lock:
            state = self.read(run_id)
            if any(job["status"] in ("queued", "running") for job in state["jobs"]):
                raise ValueError("当前计算完成后可更新受影响层")
            impact = self.preview_updates(run_id)
            state["update_chain"] = {"status": "running", "steps": impact["steps"], "position": 0}
            self.save(state)
        job = self._advance_update_chain(run_id)
        return {"run": self.view(run_id, compact=True), "impact": impact, "jobs": [job] if job else []}
