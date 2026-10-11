import json

import pytest

from barcode_system import messages as m


def test_topics():
    assert m.scan_topic("dock-01") == "warehouse/scanner/dock-01/scan"
    assert m.status_topic("dock-01") == "warehouse/scanner/dock-01/status"


@pytest.mark.parametrize("bad", ["", "a/b", "dock+", "dock#", "공백 있음", "x" * 65, None, 3])
def test_invalid_scanner_ids(bad):
    with pytest.raises(ValueError):
        m.scan_topic(bad)


def test_scan_roundtrip_with_unicode():
    msg = m.build_scan("dock-01", 7, "CODE128", "위치-A01")
    payload = m.encode(msg)
    assert "위치".encode() in payload          # \uXXXX로 바뀌지 않음
    back = m.parse_scan(payload)
    assert back == msg
    assert back["v"] == 1 and back["seq"] == 7


def test_scan_ts_has_timezone():
    ts = m.build_scan("dock-01", 1, "EAN13", "1")["ts"]
    assert ts[-6] in "+-" and "." in ts       # 예) 2026-10-11T20:41:03.123+09:00


def _payload(**override):
    msg = m.build_scan("dock-01", 1, "EAN13", "8801234567893")
    msg.update(override)
    return json.dumps({k: v for k, v in msg.items() if v is not ...}).encode()


@pytest.mark.parametrize("payload", [
    b"not json", b"[1,2]", b"\xff\xfe",
    _payload(v=2), _payload(id="not-a-uuid"), _payload(seq=0), _payload(seq=True),
    _payload(seq="1"), _payload(ts="yesterday"), _payload(data=""), _payload(type=None),
    _payload(scanner_id="a/b"), _payload(data=...), _payload(data="x" * (m.MAX_DATA_LEN + 1)),
])
def test_parse_scan_rejects(payload):
    with pytest.raises(ValueError):
        m.parse_scan(payload)


def test_status():
    for state in m.STATUS_STATES:
        assert m.parse_status(m.encode(m.build_status("dock-01", state)))["state"] == state
    with pytest.raises(ValueError):
        m.build_status("dock-01", "sleeping")
