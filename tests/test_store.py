import sqlite3

import pytest

from barcode_system import messages
from barcode_system.store import SCHEMA_VERSION, ScanStore, to_utc


def scan(seq, data="8801234567893", scanner="dock-01", ts="2026-10-11T20:00:00.000+09:00", **kw):
    return messages.build_scan(scanner, seq, kw.pop("type", "EAN13"), data, ts=ts, **kw)


@pytest.fixture
def store(tmp_path):
    s = ScanStore(str(tmp_path / "db" / "barcodes.db"))
    yield s
    s.close()


def test_to_utc():
    assert to_utc("2026-10-11T20:00:00.123+09:00") == "2026-10-11T11:00:00.123+00:00"


def test_save_and_duplicate(store):
    m = scan(1)
    assert store.save_scan(m) == ScanStore.INSERTED
    assert store.save_scan(m) == ScanStore.DUPLICATE        # 재전송 → 1행 유지
    assert store.count() == 1
    row = store.scans()[0]
    assert row["scanned_at"] == "2026-10-11T11:00:00.000+00:00"
    assert row["scan_ts"] == m["ts"] and row["data"] == m["data"]


def test_conflict_keeps_original(store):
    m = scan(1)
    store.save_scan(m)
    other = dict(m, data="DIFFERENT")
    assert store.save_scan(other) == ScanStore.CONFLICT
    assert store.scans()[0]["data"] == m["data"]


def test_scanner_summary(store):
    for seq in (1, 3, 2):
        store.save_scan(scan(seq), received_at=f"2026-10-11T11:00:0{seq}.000+00:00")
    store.update_status({"scanner_id": "dock-01", "state": "online"}, "2026-10-11T10:59:00.000+00:00")
    (s,) = store.scanners()
    assert s["last_seq"] == 3                      # 도착 순서가 바뀌어도 가장 큰 seq
    assert s["scan_count"] == 3 and s["state"] == "online"


def test_status_applied_in_arrival_order(store):
    """LWT의 ts는 연결 시점 값이라 online보다 이를 수 있다 → ts가 아니라 받은 순서로 반영."""
    store.update_status(messages.build_status("dock-01", "online", ts="2026-10-11T10:00:00+09:00"))
    store.update_status(messages.build_status("dock-01", "lost", ts="2026-10-11T09:00:00+09:00"))
    assert store.scanners()[0]["state"] == "lost"


def test_reject(store):
    store.reject("warehouse/scanner/x/scan", b"\xff not json", "bad")
    (r,) = store.rejected()
    assert r["reason"] == "bad" and bytes(r["payload"]) == b"\xff not json"


def test_gaps(store):
    for seq in (1, 2, 5, 6, 9):
        store.save_scan(scan(seq))
    store.save_scan(scan(1, scanner="dock-02"))
    store.save_scan(scan(3, scanner="dock-02"))
    gaps = [(g["scanner_id"], g["gap_start"], g["gap_end"]) for g in store.gaps()]
    assert gaps == [("dock-01", 3, 4), ("dock-01", 7, 8), ("dock-02", 2, 2)]
    assert len(store.gaps("dock-02")) == 1


def test_filters_and_order(store):
    store.save_scan(scan(1, ts="2026-10-01T09:00:00+09:00"))
    store.save_scan(scan(2, ts="2026-10-02T09:00:00+09:00", data="LOC-1", type="CODE128"))
    store.save_scan(scan(1, scanner="dock-02", ts="2026-10-03T09:00:00+09:00"))
    assert [r["seq"] for r in store.scans(scanner_id="dock-01")] == [2, 1]          # 최신순
    assert [r["data"] for r in store.scans(data="LOC-1")] == ["LOC-1"]
    assert store.count(since="2026-10-02T00:00:00+09:00") == 2
    assert store.count(until="2026-10-02T00:00:00+09:00") == 1
    assert store.scans(limit=1)[0]["scanner_id"] == "dock-02"


def test_delete(store, tmp_path):
    ids = []
    for day in (1, 2, 3):
        m = scan(day, ts=f"2026-10-0{day}T09:00:00+09:00")
        ids.append(m["id"])
        store.save_scan(m)
    with pytest.raises(ValueError):
        store.delete_scans()                                    # 조건 없이 전체 삭제 방지
    assert store.delete_scans(until="2026-10-02T00:00:00+09:00") == 1
    assert store.delete_scans(ids=[ids[2]]) == 1
    store.backup(str(tmp_path / "bk" / "backup.db"))
    assert store.delete_scans(all=True) == 1
    assert store.count() == 0
    bk = sqlite3.connect(tmp_path / "bk" / "backup.db")
    assert bk.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1   # 백업에는 남아 있음
    bk.close()
    store.vacuum()


def test_schema_version_and_reopen(tmp_path):
    path = str(tmp_path / "b.db")
    s = ScanStore(path)
    s.save_scan(scan(1))
    s.close()
    s = ScanStore(path)                                         # 다시 열어도 기록 유지
    assert s.count() == 1
    assert s._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    s.close()
    with pytest.raises(RuntimeError):                           # 더 새로운 스키마는 거부
        ScanStore(path)


def test_reader_while_writing(store):
    """WAL: 다른 연결(예: 나중의 웹 페이지)이 읽는 중에도 기록 가능."""
    reader = sqlite3.connect(store.path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM scans").fetchone()
    store.save_scan(scan(1))
    reader.execute("COMMIT")
    assert reader.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
    reader.close()
