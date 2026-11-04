"""Scientific configuration differences and their smallest downstream scopes.

Layer names are the workbench's stable internal identifiers.  EC_L1 means
``corr``, EC_L2 means peak fitting, and EC_L3 means the final EC model.
The functions describe work; they never read raw data, mutate profiles or run
processors.  ``None`` in a scope means all relevant files/channels, not empty.
"""

from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

LAYERS = ("TB_L0", "TB_L1", "TB_L2", "EC_L0", "EC_L1", "EC_L2", "EC_L3")
_METADATA = {
    "provenance", "source_commit", "evidence", "accept_residual", "description",
    "comment", "comments", "note", "notes", "label", "display_name", "reason",
    "saved_at", "updated_at", "config_sha256", "initial_defaults_source",
    "exclusion_evidence", "limitation", "expected_input_measurements",
}
_CHANNEL_ARRAYS = {"fit_range", "center_anchor", "forbidden_adc", "p0_by_channel"}
_CORR_KEYS = {"selection", "background_selection", "conditions", "stable", "clock", "scale",
              "exposure", "exposure_s", "exposure_minutes", "ref", "corr"}
_FILE_KEYS = {"files", "hk_files", "primary_ids", "background_files", "background_hk_files", "background_ids"}
_ANALYSIS_TRANSFORMS = {"hk_selection", "stable_bias_tolerance", "stable_bias_min", "tail_samples"}


def _ordered(values):
    return [layer for layer in LAYERS if layer in values]


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()
                if key not in _METADATA and not str(key).startswith("_")}
    if isinstance(value, (tuple, list)):
        return [_clean(item) for item in value]
    return value


def _xml(section, tag=None):
    text = (section or {}).get("text")
    if text is None:
        return {"source_path": (section or {}).get("source_path")}
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, TypeError):
        return text
    if tag and (selected := next((node for node in root if node.tag == tag), None)) is not None:
        root = selected

    def node(item):
        return [item.tag, sorted(item.attrib.items()), (item.text or "").strip(),
                [node(child) for child in item]]

    return node(root)


def _stem(value):
    name = PurePosixPath(str(value).replace("\\", "/")).name
    for suffix in (".cut", ".event.dat", ".dat", ".l0.parquet", "_corr.l1.parquet"):
        name = name.removesuffix(suffix)
    return name


def _decode_parser(profile):
    parser = copy.deepcopy(profile.get("parser", {}))
    if profile.get("analysis_contract") == "cali_proc@0.5":
        parser.pop("conditions", None)
        clock = parser.get("clock", {})
        parser["clock"] = {key: value for key, value in clock.items()
                           if key not in {"frequency_hz", "max_internal_gap_s", "shared"}}
        parser["transform"] = {key: value for key, value in parser.get("transform", {}).items()
                               if key not in _ANALYSIS_TRANSFORMS}
    return _clean(parser)


def _analysis_parser(profile):
    parser = profile.get("parser", {})
    return _clean({"conditions": parser.get("conditions", {}),
                   "clock": {key: value for key, value in parser.get("clock", {}).items()
                             if key in {"frequency_hz", "max_internal_gap_s", "shared"}},
                   "transform": {key: value for key, value in parser.get("transform", {}).items()
                                 if key in _ANALYSIS_TRANSFORMS}})


def _inventory_rows(inventory):
    if isinstance(inventory, list):
        return inventory
    if not isinstance(inventory, dict):
        return None
    if "rows" in inventory:
        return inventory["rows"]
    if "reading_preview" in inventory:
        return _inventory_rows(inventory["reading_preview"])
    if "raw" in inventory:
        return _inventory_rows(inventory["raw"])
    return None


def _manifest(profile, inventory=None):
    rows = _inventory_rows(inventory)
    if rows is None:
        discovery = profile.get("discovery", {})
        if discovery.get("mode") != "manifest":
            return None
        rows = discovery.get("manifest", [])
    return [row for row in rows if isinstance(row, dict)]


def _row_key(row):
    return str(row.get("event", row.get("event_path", row.get("acquisition_id", row.get("stem", "")))))


def _row_stem(row):
    return str(row.get("stem") or _stem(row.get("event", row.get("event_path", row.get("acquisition_id", "")))))


def _pairs(profile, family, inventory=None):
    rows = _manifest(profile, inventory)
    if rows is None:
        return None
    return {_row_key(row): {key: row.get(key) for key in
                            ("event", "event_path", "hk", "hk_path", "kind", "channel", "acquisition_id")
                            if key in row}
            for row in rows if (row.get("kind") == "tb") == (family == "tb")}


def _raw_signature(profile, family, inventory=None):
    parser = profile.get("parser", {})
    discovery = profile.get("discovery", {})
    pairs = _pairs(profile, family, inventory)
    if pairs is None:
        pairs = [_clean(rule) for rule in discovery.get("rules", [])
                 if (rule.get("kind") == "tb") == (family == "tb")]
    return {"contract": profile.get("analysis_contract"), "parser": _decode_parser(profile),
            "xml": {role: _xml(profile.get("xml", {}).get(role), parser.get(role + "_tag"))
                    for role in ("event", "hk")}, "files": pairs}


def scientific_fingerprint(profile, layer, inventory=None):
    """Hash only settings consumed by a layer, not a complete preset identity.

    Input product identities belong in a job's dependency manifest separately.
    For L0 this excludes peak windows, model choices and presentation metadata.
    """
    if layer not in LAYERS:
        raise ValueError(f"Unknown layer: {layer}")
    profile = profile or {}
    configs = profile.get("analysis_config", {})
    if layer.endswith("L0"):
        value = _raw_signature(profile, "tb" if layer.startswith("TB") else "ec", inventory)
    elif layer == "TB_L1":
        value = {"analysis": configs.get("tb_l1", {}), "parser": _analysis_parser(profile),
                 "fitting": profile.get("fitting", {}), "contract": profile.get("analysis_contract")}
    elif layer == "TB_L2":
        value = {"analysis": configs.get("tb_l2", {}), "contract": profile.get("analysis_contract")}
    elif layer == "EC_L1":
        cfg = configs.get("ec_l1", {})
        value = {"parser": _analysis_parser(profile), "contract": profile.get("analysis_contract"),
                 "shared": {key: item for key, item in cfg.get("shared", {}).items()
                            if key in _CORR_KEYS or key in {"map", "channels"}},
                 "observations": {kind: {name: {key: item for key, item in entry.items()
                                                if key in _CORR_KEYS or key in _FILE_KEYS}
                                         for name, entry in cfg.get(kind, {}).items()}
                                  for kind in ("source", "xray")}}
    elif layer == "EC_L2":
        value = {"analysis": configs.get("ec_l1", {}), "sources": profile.get("discovery", {}).get("sources", []),
                 "contract": profile.get("analysis_contract")}
    else:
        value = {"analysis": configs.get("ec_l2", {}), "contract": profile.get("analysis_contract")}
    encoded = json.dumps(_clean(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def downstream(layer, pinned_tb=False):
    """Return this layer and downstream consumers in execution order."""
    if layer not in LAYERS:
        raise ValueError(f"Unknown layer: {layer}")
    graph = {"TB_L0": ["TB_L1"], "TB_L1": ["TB_L2"],
             "TB_L2": [] if pinned_tb else ["EC_L1"], "EC_L0": ["EC_L1"],
             "EC_L1": ["EC_L2"], "EC_L2": ["EC_L3"], "EC_L3": []}
    affected, pending = set(), [layer]
    while pending:
        item = pending.pop()
        if item not in affected:
            affected.add(item)
            pending.extend(graph[item])
    return _ordered(affected)


def impact_for_layer(layer, *, stems=None, channels=None, state=None, label=None, **details):
    pinned = (state or {}).get("tb_binding") == "pinned"
    layers = downstream(layer, pinned_tb=pinned)
    scopes = {}
    for target in layers:
        # Aggregates consume all the other, unchanged points in a channel too.
        target_stems = None if target in {"TB_L2", "EC_L3"} else stems
        # A changed TB model affects all EC acquisitions of the changed channel.
        if layer.startswith("TB") and target.startswith("EC"):
            target_stems = None
        scopes[target] = {"stems": sorted(set(target_stems)) if target_stems is not None else None,
                          "channels": sorted(set(int(ch) for ch in channels)) if channels is not None else None}
    change = {"label": label or layer, "layer": layer, "roots": [layer], "layers": layers,
              "stems": sorted(set(stems)) if stems is not None else None,
              "channels": sorted(set(int(ch) for ch in channels)) if channels is not None else None, **details}
    return {"layers": layers, "roots": [layer], "scopes": scopes, "changes": [change], "metadata_only": False}


def _union(left, right):
    return None if left is None or right is None else sorted(set(left) | set(right))


def merge_impacts(*plans):
    result = {"layers": [], "roots": [], "scopes": {}, "changes": [], "metadata_only": True}
    for plan in plans:
        if not plan:
            continue
        result["changes"].extend(copy.deepcopy(plan.get("changes", [])))
        result["roots"] = _ordered(set(result["roots"]) | set(plan.get("roots", [])))
        for layer in plan.get("layers", []):
            scope = plan.get("scopes", {}).get(layer, {"stems": None, "channels": None})
            if layer not in result["scopes"]:
                result["scopes"][layer] = copy.deepcopy(scope)
            else:
                for key in ("stems", "channels"):
                    result["scopes"][layer][key] = _union(result["scopes"][layer].get(key), scope.get(key))
    result["layers"] = _ordered(result["scopes"])
    result["metadata_only"] = not result["layers"]
    return result


def _diff(left, right, path=()):
    if isinstance(left, dict) and isinstance(right, dict):
        return [item for key in sorted(set(left) | set(right))
                for item in _diff(left.get(key), right.get(key), (*path, key))]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [item for index, (old, new) in enumerate(zip(left, right, strict=True))
                for item in _diff(old, new, (*path, index))]
    return [] if left == right else [path]


def _channels(paths):
    channels = set()
    for path in paths:
        indexes = [index for index, key in enumerate(path[:-1]) if key in _CHANNEL_ARRAYS]
        if not indexes:
            return None
        value = path[indexes[-1] + 1]
        if not (isinstance(value, int) or str(value).isdigit()) or int(value) not in range(4):
            return None
        channels.add(int(value))
    return sorted(channels) or None


def _entry_stems(profile, kind, name, entry):
    rows = _manifest(profile) or []
    selected = [row for row in rows if (row.get("kind") == kind
                and str(row.get("analysis_id", row.get("source", ""))) == str(name))]
    if selected:
        return sorted({_row_stem(row) for row in selected})
    refs = entry.get("files", entry.get("primary_ids", []))
    return sorted({_stem(ref) for ref in refs}) or None


def _background_dependents(profile, changed_refs):
    """Find every observation whose signal or background uses changed files."""
    result = set()
    config = profile.get("analysis_config", {}).get("ec_l1", {})
    for kind in ("source", "xray"):
        for name, entry in config.get(kind, {}).items():
            refs = {str(ref) for key in _FILE_KEYS for ref in entry.get(key, [])}
            if any(_same_ref(ref, changed) for ref in refs for changed in changed_refs):
                result.update(_entry_stems(profile, kind, name, entry) or [])
    return result


def _same_ref(left, right):
    left, right = str(left).replace("\\", "/"), str(right).replace("\\", "/")
    return left == right or left.endswith("/" + right) or right.endswith("/" + left)


def configuration_impact(old_profile, new_profile, state=None, *, old_inventory=None, new_inventory=None):
    """Compare editable presets and describe only changed numerical work.

    Passing both discovery previews makes equivalent filename-rule edits a
    no-op.  The caller already performs previews for its normal UI validation;
    this function does not enumerate the filesystem a second time.
    """
    old, new = old_profile or {}, new_profile or {}
    plans = []

    def add(layer, label, stems=None, channels=None, **details):
        plans.append(impact_for_layer(layer, stems=stems, channels=channels, state=state, label=label, **details))

    if old.get("analysis_contract") != new.get("analysis_contract"):
        add("TB_L0", "数据规范", family="tb")
        add("EC_L0", "数据规范", family="ec")
        return merge_impacts(*plans)
    parser_changed = _decode_parser(old) != _decode_parser(new)
    xml_changed = any(_xml(old.get("xml", {}).get(role), old.get("parser", {}).get(role + "_tag"))
                      != _xml(new.get("xml", {}).get(role), new.get("parser", {}).get(role + "_tag"))
                      for role in ("event", "hk"))
    if parser_changed or xml_changed:
        add("TB_L0", "XML 与字段换算")
        add("EC_L0", "XML 与字段换算")
    elif _analysis_parser(old) != _analysis_parser(new):
        add("TB_L1", "工况与时间归一化")
        add("EC_L1", "工况与时间归一化")

    for family, start in (("tb", "TB_L0"), ("ec", "EC_L0")):
        before, after = _pairs(old, family, old_inventory), _pairs(new, family, new_inventory)
        if before is not None and after is not None:
            keys = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
            if keys:
                rows = (_manifest(old, old_inventory) or []) + (_manifest(new, new_inventory) or [])
                stems = sorted({_row_stem(row) for row in rows if _row_key(row) in keys})
                add(start, "事件与 HK 配对", stems=stems or None)
                if family == "ec":
                    dependents = _background_dependents(old, keys) | _background_dependents(new, keys)
                    if dependents:
                        add("EC_L2", "本底引用", stems=sorted(dependents))
        else:
            def rules(profile, family=family):
                discovery = profile.get("discovery", {})
                return {"mode": discovery.get("mode"), "rules": [_clean(rule) for rule in discovery.get("rules", [])
                        if (rule.get("kind") == "tb") == (family == "tb")]}
            if rules(old) != rules(new):
                add(start, "文件匹配规则")

    # The file paths can stay identical while a filename rule or mapping changes
    # the physical interpretation.  Such edits start at analysis, not decoding.
    rows_before, rows_after = _manifest(old, old_inventory), _manifest(new, new_inventory)
    if rows_before is not None and rows_after is not None:
        old_rows = {_row_key(row): row for row in rows_before}
        new_rows = {_row_key(row): row for row in rows_after}
        keys = {"temp", "bias", "source", "source_name", "energy_keV", "peak_energies_keV", "analysis_id", "background_ids"}
        for key in sorted(set(old_rows) & set(new_rows)):
            before, after = old_rows[key], new_rows[key]
            changes = {field for field in keys if before.get(field) != after.get(field)}
            if not changes:
                continue
            stems = sorted({_row_stem(before), _row_stem(after)})
            if after.get("kind") == "tb":
                add("TB_L1", "工况映射", stems=stems)
            elif changes & {"temp", "bias"}:
                add("EC_L1", "EC 工况映射", stems=stems)
            elif not any("EC_L2" in plan["layers"] for plan in plans):
                add("EC_L2", "能线与本底映射", stems=stems)

    if _clean(old.get("fitting", {})) != _clean(new.get("fitting", {})):
        add("TB_L1", "TB 分箱与寻峰设置")

    configs_old, configs_new = old.get("analysis_config", {}), new.get("analysis_config", {})
    for config_name, start, label in (("tb_l2", "TB_L2", "TB 曲面设置"), ("ec_l2", "EC_L3", "EC 刻度与分辨率设置")):
        paths = _diff(_clean(configs_old.get(config_name, {})), _clean(configs_new.get(config_name, {})))
        if paths:
            add(start, label, channels=_channels(paths))

    tb_old, tb_new = configs_old.get("tb_l1", {}), configs_new.get("tb_l1", {})
    old_shared = {key: value for key, value in tb_old.items() if key != "measurements"}
    new_shared = {key: value for key, value in tb_new.items() if key != "measurements"}
    if _clean(old_shared) != _clean(new_shared):
        add("TB_L1", "TB 选段、分箱与谱模型")
    entries_old, entries_new = tb_old.get("measurements", {}), tb_new.get("measurements", {})
    for name in sorted(set(entries_old) | set(entries_new)):
        before, after = entries_old.get(name, {}), entries_new.get(name, {})
        paths = _diff(_clean(before), _clean(after))
        if paths:
            stems = set(_entry_stems(old, "tb", name, before) or []) | set(_entry_stems(new, "tb", name, after) or [])
            add("TB_L1", f"TB 工况 {name}", stems=sorted(stems) or None, channels=_channels(paths), analysis=name)

    ec_old, ec_new = configs_old.get("ec_l1", {}), configs_new.get("ec_l1", {})
    before_shared = {key: value for key, value in ec_old.items() if key not in {"source", "xray"}}
    after_shared = {key: value for key, value in ec_new.items() if key not in {"source", "xray"}}
    shared_paths = _diff(_clean(before_shared), _clean(after_shared))
    if shared_paths:
        corr = any(any(key in _CORR_KEYS or key in {"map", "channels"} for key in path) for path in shared_paths)
        add("EC_L1" if corr else "EC_L2", "EC 选段与校正" if corr else "EC 分箱与谱模型")
    for kind in ("source", "xray"):
        before_entries, after_entries = ec_old.get(kind, {}), ec_new.get(kind, {})
        for name in sorted(set(before_entries) | set(after_entries)):
            before, after = before_entries.get(name, {}), after_entries.get(name, {})
            paths = _diff(_clean(before), _clean(after))
            if not paths:
                continue
            corr = any(any(key in _CORR_KEYS or key in _FILE_KEYS for key in path) for path in paths)
            stems = set(_entry_stems(old, kind, name, before) or []) | set(_entry_stems(new, kind, name, after) or [])
            channels = _channels(paths)
            joint = before.get("fit_mode") == "doublet" or after.get("fit_mode") == "doublet"
            energies = sorted({float(line["energy_keV"]) for entry in (before, after)
                               for line in entry.get("peaks", [entry]) if line.get("energy_keV") is not None})
            add("EC_L1" if corr else "EC_L2", f"EC {name}", stems=sorted(stems) or None,
                channels=channels, analysis=name, kind=kind, joint_fit=joint,
                energies_keV=energies, changed_paths=[list(path) for path in paths])
            # A shared background's selection must be common to all its consumers.
            if any("background_selection" in path for path in paths):
                refs = {str(ref) for entry in (before, after) for key in ("background_files", "background_ids")
                        for ref in entry.get(key, [])}
                dependents = _background_dependents(old, refs) | _background_dependents(new, refs)
                background_stems = {_row_stem(row) for profile in (old, new) for row in (_manifest(profile) or [])
                                    if _row_key(row) in refs}
                if dependents or background_stems:
                    add("EC_L1", "共享本底选段", stems=sorted(dependents | background_stems), channels=channels)

    # Source aliases/energies can be edited without touching native YAML too.
    if _clean(old.get("discovery", {}).get("sources", [])) != _clean(new.get("discovery", {}).get("sources", [])):
        # Native sync mirrors YAML edits; retain the narrower already-derived scope.
        if not any("EC_L2" in plan["layers"] for plan in plans):
            add("EC_L2", "放射源能线")
    return merge_impacts(*plans)
