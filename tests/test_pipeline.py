"""
1~2단계 전체 흐름: 스캐너 → Mosquitto → db-writer → SQLite.
  - 브로커 영속성 설정(mosquitto/pi/barcode.conf의 값 그대로)이 브로커 강제 종료를 견디는지
  - 실제 프로세스 두 개를 각각 강제 종료·재시작해도 outbox와 DB가 정확히 일치하는지
"""
import re
import time

import pytest

from barcode_system.store import ScanStore
from conftest import ROOT, make_secure_broker, wait_until
from helpers import (add_scans, db_ids, graceful_stop, hard_kill, make_publisher, make_writer, query,
                     read_log, scanner_args, start_process)

PI_CONF = ROOT / "mosquitto" / "pi" / "barcode.conf"


def pi_persistence_lines():
    """파이 브로커 설정에서 영속성·큐 관련 줄만 가져온다 (테스트가 실제 설정값을 쓰도록)."""
    lines = [ln.strip() for ln in PI_CONF.read_text(encoding="utf-8").splitlines()
             if re.match(r"(autosave_|max_queued_|persistent_client_)", ln.strip())]
    assert any(ln.startswith("autosave_") for ln in lines)
    return lines


@pytest.mark.parametrize("settings,kept", [("pi", True), ("old_60s", False)])
def test_broker_crash_keeps_queued_messages(tmp_path, settings, kept):
    """
    db-writer가 꺼져 있는 동안 브로커에 쌓인 메시지가 브로커 강제 종료 후에도 남는지.
    pi      : mosquitto/pi/barcode.conf 의 설정 (변경할 때마다 저장) → 남아야 한다
    old_60s : 이전 설정 (60초마다 저장) → 사라진다. 설정을 바꾼 이유를 보여 주는 대조군
    """
    pdir = tmp_path / "mq"
    pdir.mkdir()
    lines = pi_persistence_lines() if settings == "pi" else ["autosave_interval 60"]
    broker = make_secure_broker(tmp_path, f"persistence true\npersistence_location {pdir.as_posix()}/\n"
                                + "\n".join(lines) + "\n")
    broker.start()
    store = ScanStore(str(tmp_path / "barcodes.db"))
    box, pub = make_publisher(tmp_path, broker)
    try:
        make_writer(store, tmp_path, broker).stop()      # 영속 세션 만들고 종료
        pub.start()
        ids = add_scans(box, 10)
        pub.notify()
        assert wait_until(lambda: box.pending_count() == 0)   # 스캐너는 전송 완료로 처리
        pub.stop()
        time.sleep(0.5)
        broker.stop()                                     # 강제 종료 (kill)
        broker.start()
        w = make_writer(store, tmp_path, broker)
        if kept:
            assert wait_until(lambda: db_ids(store) == set(ids), timeout=15)
        else:
            time.sleep(3)
            assert store.count() == 0
        w.stop()
    finally:
        box.close()
        store.close()
        broker.stop()


def test_full_pipeline_with_process_crashes(tmp_path, images):
    broker = make_secure_broker(tmp_path, f"persistence true\npersistence_location {tmp_path.as_posix()}/\n"
                                + "\n".join(pi_persistence_lines()) + "\n")
    broker.start()
    for user in ("dock-01", "db-writer"):
        (tmp_path / f"{user}.pass").write_text(f"pw-{user}\n", encoding="utf-8")
    outbox, db = tmp_path / "outbox.db", tmp_path / "barcodes.db"
    writer_args = ["barcode_system.db_writer_app", "--host", "127.0.0.1", "--port", str(broker.port),
                   "--password-file", str(tmp_path / "db-writer.pass"), "--keepalive", "5",
                   "--db", str(db), "--spool", str(tmp_path / "spool.jsonl")]
    scan_args = scanner_args(images, broker.port, outbox, "--password-file", str(tmp_path / "dock-01.pass"))
    db_count = lambda: (query(db, "SELECT COUNT(*) FROM scans") or [(0,)])[0][0]  # noqa: E731
    procs = []

    def start(args, name):
        p = start_process(args, tmp_path / f"{name}.log")
        procs.append(p)
        return p

    try:
        w = start(writer_args, "writer1")
        assert wait_until(lambda: "구독" in read_log(w), timeout=15), read_log(w)
        s = start(scan_args, "scanner1")
        assert wait_until(lambda: db_count() >= 8, timeout=30), read_log(s) + read_log(w)

        hard_kill(w)                                  # db-writer 강제 종료 → 그동안 브로커가 보관
        n = len(query(outbox, "SELECT seq FROM outbox"))
        assert wait_until(lambda: len(query(outbox, "SELECT seq FROM outbox")) >= n + 5, timeout=30)
        w = start(writer_args, "writer2")

        hard_kill(s)                                  # 스캐너 강제 종료 → outbox에 남은 것은 재시작 후 전송
        s = start(scan_args, "scanner2")
        assert wait_until(lambda: db_count() >= n + 15, timeout=30), read_log(s) + read_log(w)

        assert graceful_stop(s) == 0, read_log(s)    # 정상 종료: 남은 메시지 전송 후 offline
        out_ids = {r[0] for r in query(outbox, "SELECT id FROM outbox")}
        assert not query(outbox, "SELECT seq FROM outbox WHERE sent_at IS NULL")
        assert wait_until(lambda: {r[0] for r in query(db, "SELECT id FROM scans")} == out_ids,
                          timeout=20)
        assert wait_until(lambda: query(db, "SELECT state FROM scanners") == [("offline",)])
        assert graceful_stop(w) == 0, read_log(w)
    finally:
        for p in procs:
            if p.poll() is None:
                hard_kill(p)
        broker.stop()

    seqs = [r[0] for r in query(db, "SELECT seq FROM scans ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1))      # DB 기준으로도 빈 번호·중복 행 없음
    assert query(db, "SELECT COUNT(*) FROM rejected") == [(0,)]
    print(f"outbox {len(out_ids)}건 = DB {len(seqs)}건 (강제 종료: db-writer 1회, 스캐너 1회)")
