"""스캐너 앱: 설정 처리, 그리고 실제 프로세스(이미지 폴더 카메라)로 end-to-end·강제 종료 복구 확인."""
import os
import signal
import sqlite3
import subprocess
import sys

import cv2
import pytest

from barcode_images import camera_like, ean13_image, symbol_image
from barcode_system import scanner_app
from conftest import ROOT, wait_until


# ================================================================ 설정
def write_ini(tmp_path, text):
    p = tmp_path / "scanner.ini"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_config_precedence_cli_over_ini_over_default(tmp_path):
    (tmp_path / "pw.pass").write_text("s3cret\n", encoding="utf-8")
    ini = write_ini(tmp_path, f"""
[scanner]
id = dock-07          ; 주석
cooldown = 1.5
show = yes
[mqtt]
host = 10.0.0.5
password_file = {(tmp_path / 'pw.pass').as_posix()}
""")
    cfg = scanner_app.load_config(["--config", ini, "--cooldown", "0.5"])
    assert cfg.scanner_id == "dock-07"
    assert cfg.scanner_cooldown == 0.5          # 명령줄 우선
    assert cfg.scanner_show is True             # INI
    assert cfg.mqtt_host == "10.0.0.5"
    assert cfg.mqtt_port == 1883                # 기본값
    assert cfg.mqtt_username == "dock-07"       # 비우면 scanner id
    assert cfg.mqtt_password == "s3cret"        # 줄바꿈 제거


def test_password_from_env(monkeypatch):
    monkeypatch.setenv(scanner_app.PASSWORD_ENV, "envpw")
    assert scanner_app.load_config(["--id", "d1"]).mqtt_password == "envpw"


@pytest.mark.parametrize("argv", [
    [],                                          # id 없음
    ["--id", "a/b"],                             # 토픽에 쓸 수 없는 id
    ["--id", "d1", "--confirm", "0"],
    ["--id", "d1", "--cooldown", "-1"],
    ["--id", "d1", "--camera", "files"],         # --images 없음
    ["--id", "d1", "--password-file", "does-not-exist.pass"],
])
def test_config_errors(argv):
    with pytest.raises(SystemExit):
        scanner_app.load_config(argv)


def test_config_invalid_ini_value(tmp_path):
    ini = write_ini(tmp_path, "[scanner]\nid = d1\nmode = sometimes\n")
    with pytest.raises(SystemExit):
        scanner_app.load_config(["--config", ini])


# ================================================================ 실제 프로세스
EAN = "8801234567893"
LOC = "LOC-A01-03"


@pytest.fixture
def images(tmp_path):
    d = tmp_path / "images"
    d.mkdir()
    expected, img = ean13_image(EAN[:12])
    assert expected == EAN
    cv2.imwrite(str(d / "1_ean13.png"), camera_like(img, blur=5))
    cv2.imwrite(str(d / "2_code128.png"), camera_like(symbol_image("code128", LOC)))
    return d


def start_scanner(tmp_path, images, port, log_name):
    args = [sys.executable, "-m", "barcode_system.scanner_app", "--id", "dock-01",
            "--camera", "files", "--images", str(images), "--fps", "10",
            "--mode", "interval", "--cooldown", "0.3", "--confirm", "1",
            "--host", "127.0.0.1", "--port", str(port), "--keepalive", "5",
            "--outbox", str(tmp_path / "outbox.db")]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    log = open(tmp_path / log_name, "w", encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1")
    proc = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=flags, env=env)
    proc.log_file = log
    return proc


def graceful_stop(proc):
    """Windows는 Ctrl+Break, 그 외는 SIGTERM → 앱이 정상 종료 경로(finally)를 탄다."""
    proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
    code = proc.wait(timeout=15)
    proc.log_file.close()
    return code


def outbox_rows(tmp_path):
    db = sqlite3.connect(f"file:{tmp_path / 'outbox.db'}?mode=ro", uri=True)
    try:
        return db.execute("SELECT seq, id, sent_at FROM outbox ORDER BY seq").fetchall()
    finally:
        db.close()


def test_standalone_barcode_scanner_still_works(images, tmp_path):
    """리팩터링 후에도 기존 단독 실행판(barcode_scanner.py, CSV + SQLite)이 그대로 동작하는지."""
    csv_path, db_path = tmp_path / "out" / "b.csv", tmp_path / "out" / "b.db"
    args = [sys.executable, "barcode_scanner.py", "--camera", "files", "--images", str(images),
            "--confirm", "1", "--cooldown", "60", "--csv", str(csv_path), "--db", str(db_path)]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    log = open(tmp_path / "s.log", "w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
                            env=dict(os.environ, PYTHONUTF8="1"))

    def rows():
        if not db_path.exists():
            return []
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return db.execute("SELECT type, data FROM scans").fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()

    try:
        assert wait_until(lambda: len(rows()) >= 2, timeout=30), (tmp_path / "s.log").read_text("utf-8")
    finally:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        code = proc.wait(timeout=15)
        log.close()
    assert code == 0, (tmp_path / "s.log").read_text("utf-8")
    assert sorted(rows()) == [("CODE128", LOC), ("EAN13", EAN)]   # cooldown 60초 → 각 1건
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "timestamp,type,data" and len(lines) == 3


def test_end_to_end_and_crash_recovery(anon_broker, collectors, images, tmp_path):
    col = collectors(anon_broker.host, anon_broker.port).wait_ready()

    # 1) 실행 → 두 종류 바코드가 ZBar 표기로 발행되는지
    p1 = start_scanner(tmp_path, images, anon_broker.port, "run1.log")
    try:
        assert wait_until(lambda: len(col.scans()) >= 10, timeout=30), \
            (tmp_path / "run1.log").read_text(encoding="utf-8")
        assert {(m["type"], m["data"]) for m in col.scans()} == {("EAN13", EAN), ("CODE128", LOC)}
    finally:
        p1.kill()                     # 2) 강제 종료 (전원 차단·프로세스 kill 흉내)
        p1.wait()
        p1.log_file.close()

    # 비정상 종료 → 브로커가 LWT(lost) 발행
    assert wait_until(lambda: col.statuses()[-1]["state"] == "lost")
    before = outbox_rows(tmp_path)
    assert before, "강제 종료 전에 outbox에 기록이 있어야 함"

    # 3) 같은 outbox로 재시작 → 남은 메시지 전송 + 일련번호 이어짐
    p2 = start_scanner(tmp_path, images, anon_broker.port, "run2.log")
    try:
        assert wait_until(lambda: len(outbox_rows(tmp_path)) >= len(before) + 5, timeout=30)
    finally:
        code = graceful_stop(p2)      # 4) 정상 종료
    log2 = (tmp_path / "run2.log").read_text(encoding="utf-8")
    assert code == 0, log2
    assert "종료" in log2
    assert wait_until(lambda: col.statuses()[-1]["state"] == "offline")

    rows = outbox_rows(tmp_path)
    seqs = [s for s, _, _ in rows]
    assert seqs == list(range(1, len(rows) + 1))              # 재시작해도 seq가 이어짐 (빈 번호 없음)
    assert all(sent for _, _, sent in rows), "정상 종료 시 남은 메시지를 모두 전송(flush)"
    assert wait_until(lambda: col.scan_ids() >= {i for _, i, _ in rows})   # 유실 없음
    received = col.scans()
    dupes = len(received) - len(col.scan_ids())
    print(f"outbox {len(rows)}건, 수신 {len(received)}건 (중복 {dupes}건, 고유 id 기준 유실 0)")
