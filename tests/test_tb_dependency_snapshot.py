"""Selecting an already frozen native TB package is idempotent."""
import hashlib
import json

import pytest
from calibration_lib.workbench.dependencies import snapshot, tb_package


def test_native_snapshot_can_be_selected_again_with_its_companion(tmp_path):
    source = tmp_path / "shared/model.json"
    source.parent.mkdir()
    source.write_text(json.dumps([{"G0": .01, "k": .02, "V0": 24., "b": 54., "c": 23000.}] * 4))
    meta = {"model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "schema": "g15b-tb-l2@1"}
    source.with_suffix(".meta.json").write_text(json.dumps(meta))
    run = tmp_path / "personal/run"
    frozen = snapshot(tb_package(source), run)
    before = (run / "dependencies" / frozen["id"] / source.name).read_bytes()
    again = snapshot(frozen, run)
    assert again == frozen
    assert source.read_bytes() == before
    # Re-selecting the source also reuses its immutable existing companion.
    assert snapshot(tb_package(source), run)["path"] == frozen["path"]
    source.with_suffix(".meta.json").write_text(json.dumps({**meta, "note": "changed"}))
    with pytest.raises(ValueError, match="Meta"):
        snapshot(tb_package(source), run)
    assert json.loads((run / "dependencies" / frozen["id"] / source.with_suffix(".meta.json").name).read_text()) == meta
