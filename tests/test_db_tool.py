import csv
import sqlite3

import pytest

from barcode_system import db_tool, messages
from barcode_system.store import ScanStore


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barcodes.db"
    s = ScanStore(str(path))
    for day in (1, 2, 3):
        s.save_scan(messages.build_scan("dock-01", day, "EAN13", f"880000000000{day}",
                                        ts=f"2026-10-0{day}T09:00:00+09:00"))
    s.save_scan(messages.build_scan("dock-02", 5, "CODE128", "LOC-한글", ts="2026-10-02T10:00:00+09:00"))
    s.update_status(messages.build_status("dock-01", "online"))
    s.reject("warehouse/x", b"bad", "알 수 없는 토픽")
    s.close()
    return str(path)


def run(capsys, *argv):
    code = db_tool.main(list(argv))
    return code, capsys.readouterr().out


def count(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    finally:
        c.close()


def test_scans_scanners_gaps_rejected(db, capsys):
    code, out = run(capsys, "--db", db, "scans", "--limit", "2")
    assert code == 0 and "전체 4건 중 2건" in out
    _, out = run(capsys, "--db", db, "scans", "--scanner", "dock-02")
    assert "LOC-한글" in out
    _, out = run(capsys, "--db", db, "scanners")
    assert "dock-01" in out and "online" in out
    _, out = run(capsys, "--db", db, "gaps")
    assert "dock-01" not in out and "(없음)" in out    # dock-01은 1,2,3 연속
    _, out = run(capsys, "--db", db, "rejected")
    assert "알 수 없는 토픽" in out


def test_export_csv(db, tmp_path, capsys):
    out_csv = tmp_path / "out.csv"
    run(capsys, "--db", db, "export", str(out_csv), "--since", "2026-10-02")
    with open(out_csv, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["data"] for r in rows] == ["8800000000002", "LOC-한글", "8800000000003"]   # 오래된 순
    assert rows[0]["scanned_at"].startswith("2026-10-02 ")


def test_delete_is_preview_by_default(db, capsys):
    _, out = run(capsys, "--db", db, "delete", "--before", "2026-10-02")
    assert "[미리보기]" in out and "1건" in out
    assert count(db) == 4


def test_delete_with_yes_and_backup(db, tmp_path, capsys):
    backup = tmp_path / "bk.db"
    _, out = run(capsys, "--db", db, "delete", "--scanner", "dock-01", "--yes",
                 "--backup", str(backup), "--vacuum")
    assert "3건 삭제" in out
    assert count(db) == 1 and count(str(backup)) == 4


def test_delete_all_and_rejected(db, capsys):
    _, out = run(capsys, "--db", db, "delete", "--all", "--yes")
    assert "4건 삭제" in out and count(db) == 0
    _, out = run(capsys, "--db", db, "delete", "--rejected", "--yes")
    assert "거부된 메시지 1건 삭제" in out


@pytest.mark.parametrize("argv", [
    ["delete"],                                   # 조건 없음
    ["delete", "--all", "--scanner", "dock-01"],  # 전체 + 조건
    ["delete", "--rejected", "--all"],
    ["scans", "--since", "어제"],
])
def test_delete_argument_errors(db, argv):
    with pytest.raises(SystemExit):
        db_tool.main(["--db", db] + argv)


def test_missing_db_is_not_created(tmp_path):
    with pytest.raises(SystemExit):
        db_tool.main(["--db", str(tmp_path / "nope.db"), "scans"])
    assert not (tmp_path / "nope.db").exists()
