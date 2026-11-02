"""FT584 native clocks, unselected inputs and role-specific HK evidence."""

import binascii
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common import legacy_parser
from calibration_lib.common.native_time import apply_conditions, event_span, publish_native_input
from calibration_lib.common.utils import read_json, read_parquet, read_parquet_metadata
from calibration_lib.config.payload_config import get_payload_config
from calibration_lib.ec.processors.L0_processor import ECL0Processor
from calibration_lib.workbench.acquisition import default_profile

RESOURCES = Path(__file__).resolve().parents[1] / "resources" / "legacy"


def science(channel, counter, *, valid=True):
    raw = bytearray(584)
    raw[:4] = bytes.fromhex("1c1c2288")
    raw[4:8] = (17).to_bytes(4, "big")  # Deliberately useless coarse UTC.
    raw[20:28] = counter.to_bytes(8, "big")
    raw[28:30] = channel.to_bytes(2, "big")
    raw[34:36] = (38).to_bytes(2, "big")
    for event in range(38):
        offset = 44 + event * 14
        raw[offset:offset + 4] = (700 + event).to_bytes(4, "big")
        raw[offset + 4:offset + 6] = (1000 + channel).to_bytes(2, "big")
        raw[offset + 6:offset + 8] = (400).to_bytes(2, "big")
    raw[578:580] = binascii.crc_hqx(raw[:578], 0).to_bytes(2, "big")
    raw[580:] = bytes.fromhex("cc118822")
    if not valid:
        raw[50] ^= 1
    return bytes(raw)


def telemetry(temperature, voltage, *, valid=True):
    raw = bytearray(178)
    raw[:4] = bytes.fromhex("1a2b3c4d")
    raw[4:8] = (99).to_bytes(4, "big")
    packet = ET.parse(RESOURCES / "hnu_packet.xml").getroot().find("hk_packet")
    for channel in range(4):
        for name, value in ((f"sipm_temp{channel}", temperature), (f"sipm_voltage{channel}", voltage),
                            (f"sipm_current{channel}", 8)):
            field = packet.find(name)
            start, size = int(field.findtext("start")), int(field.findtext("size"))
            raw[start:start + size] = int(value).to_bytes(size, "big")
    raw[176:] = binascii.crc_hqx(raw[:176], 0).to_bytes(2, "big")
    if not valid:
        raw[10] ^= 1
    return bytes(raw)


def decoded(tmp_path):
    event, hk = tmp_path / "source.dat", tmp_path / "source.hk"
    event.write_bytes(b"prefix" + b"".join(science(ch, counter, valid=not (ch == 2 and counter == 200_000_000))
                                           for counter in (100_000_000, 200_000_000) for ch in range(4)) + b"end")
    hk.write_bytes(telemetry(29315, 28500) + telemetry(30315, 28500) + telemetry(31315, 26000)
                   + telemetry(35000, 28500, valid=False))
    profile = default_profile("14B")
    profile["analysis_contract"] = "cali_proc@0.5"
    frame, meta = legacy_parser.parse_event_hk_to_dataframe(event, hk, tmp_path, "grid1x_ft_packet", "hk_packet",
                                                         True, input_profile=profile,
                                                         observation={"kind": "source", "stem": "spectrum"})
    return frame, meta


def test_native_decode_keeps_both_clocks_crc_and_real_offsets(tmp_path, monkeypatch):
    before = (RESOURCES / "hnu_packet.xml").read_bytes()
    monkeypatch.setattr(legacy_parser, "PARSER_CHUNK_BYTES", 2001)
    frame, meta = decoded(tmp_path)
    full = frame.attrs["native_tables"]["events"]
    assert len(full) == 8 * 38
    assert len(frame) == 7 * 38
    assert full["timestamp1"].tolist() == [counter for counter in (100_000_000, 200_000_000) for _ in range(4 * 38)]
    assert full["timestamp2"].tolist() == list(range(700, 738)) * 8
    assert full["packet_byte_offset"].tolist() == [6 + index * 584 for index in range(8) for _ in range(38)]
    assert full["source_idx"].tolist() == list(range(304))
    assert meta["crc"]["failed_events"] == 38
    assert meta["hk_crc"]["failed_packets"] == 1
    assert (RESOURCES / "hnu_packet.xml").read_bytes() == before


def test_exp_retains_bad_records_l0_retains_full_valid_hk_and_l1_selects_platform(tmp_path):
    frame, meta = decoded(tmp_path)
    native = publish_native_input(frame, meta, tmp_path / "products", "14B", "ec", "capture-A")
    exp, l0 = Path(native["exp_node"]), Path(native["l0_node"])
    conversion = read_json(exp / "meta.json")["conversion"]
    assert conversion["measurement_frequency_hz"] == 100_000_000
    assert conversion["sci_packet_bytes"] == 584
    assert conversion["sci_events_per_packet"] == 38
    assert conversion["hk_packet_bytes"] == 178
    assert len(read_parquet(exp / "events.parquet")) == 304
    assert len(read_parquet(exp / "telemetry.parquet")) == 4
    assert len(read_parquet(l0 / "events.parquet")) == 266
    assert len(read_parquet(l0 / "hk.parquet")) == 3  # Ramp remains in L0.
    assert "temp" not in read_parquet(l0 / "events.parquet")
    assert read_json(l0 / "meta.json")["cleaning"]["events"]["bad_crc"] == 38
    assert not frame.attrs
    corrected, conditions = apply_conditions(frame, native, 28.5)
    np.testing.assert_allclose(corrected["temp"], 25.)
    np.testing.assert_allclose(corrected["bias"], 28.5)
    assert conditions["selection"]["selected_source_idx"] == [0, 1]
    assert conditions["selection"]["sci_hk_aligned"] is False
    assert len(read_parquet(l0 / "hk.parquet")) == 3
    with pytest.raises(ValueError, match="没有.*27"):
        apply_conditions(frame, native, 27.)


def test_exposure_uses_native_physical_channel_axis_not_coarse_utc():
    frame = pd.DataFrame({"channel": [0, 1, 0, 1], "timestamp1": [1, 100_000_000, 300_000_001, 200_000_000], "utc": [17] * 4})
    assert event_span(frame, 0)["span_s"] == 3.
    assert event_span(frame, 1)["span_s"] == 1.
    with pytest.raises(ValueError, match="回退"):
        event_span(frame)
    tb_span = event_span(frame, require_ec=False)
    assert tb_span["monotonic"] is False
    assert tb_span["span_s"] == pytest.approx((200_000_000 - 1) / 100_000_000)
    assert tb_span["ec_exposure_eligible"] is False


@pytest.mark.parametrize("counter,reason", [([100, 20], "回退"), ([1, 1], "非正"), ([1, 1_100_000_001], "间隙")])
def test_invalid_native_exposure_is_explicit_and_not_replaced_by_one_second(counter, reason):
    frame = pd.DataFrame({"channel": [0, 0], "timestamp1": counter})
    with pytest.raises(ValueError, match=reason):
        event_span(frame, 0)
    assert not event_span(frame, 0, require_ec=False)["ec_exposure_eligible"]


def test_missing_native_clock_does_not_fall_back_to_row_numbers():
    with pytest.raises(ValueError, match="timestamp1"):
        event_span(pd.DataFrame({"channel": [0, 0], "utc": [1, 2]}), 0)


def test_tb_global_extrema_preserves_native_order_diagnostics():
    counters = [300_000_000, 100_000_000, 400_000_000, 200_000_000]
    frame = pd.DataFrame({"channel": [0, 1, 0, 1], "timestamp1": counters})
    span = event_span(frame, require_ec=False, global_extrema=True)
    assert span["span_s"] == 3.
    assert span["first_last_span_s"] == -1.
    assert span["monotonic"] is False
    assert span["ec_exposure_eligible"] is False
    assert frame.timestamp1.tolist() == counters
    with pytest.raises(ValueError, match="per-channel"):
        event_span(frame, global_extrema=True)


def test_native_l0_source_identity_does_not_require_filename_minutes(tmp_path):
    decoded(tmp_path)
    profile = default_profile("14B")
    profile["analysis_contract"] = "cali_proc@0.5"
    profile["discovery"].update(mode="manifest", manifest=[{
        "kind": "source", "event": str(tmp_path / "source.dat"), "hk": str(tmp_path / "source.hk"),
        "source": "Cs137", "stem": "src_Cs137_capture-A", "acquisition_id": "capture-A",
        "exposure_minutes": None, "peak_energies_keV": [661.657],
    }])
    config = replace(get_payload_config("14B"), raw_root=tmp_path)
    result = ECL0Processor("14B", tmp_path / "products", input_profile=profile).process(config)
    assert not result["errors"]
    output = result["outputs"][0]
    assert output["stem"] == "src_Cs137_capture-A"
    assert output["exposure_minutes"] is None
    path = tmp_path / "products/14B/ec/L0/parquet/src_Cs137_capture-A.l0.parquet"
    native = read_parquet_metadata(path, "native")
    assert Path(native["l0_node"]).name == "capture-A"
    assert native["analysis_contract"] == "cali_proc@0.5"
    assert read_parquet_metadata(path, "acquisition")["native"] == native
