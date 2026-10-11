"""DbWriter 테스트: 메시지 분류·재시도·보조 파일·fail-stop, 그리고 보안 브로커를 통한 통합 동작."""
import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from barcode_system import messages
from barcode_system.mqtt_compat import make_client
from barcode_system.spool import Spool
from barcode_system.store import ScanStore
from barcode_system.subscriber import DbWriter
from conftest import wait_until
from helpers import add_scans, creds, db_ids, make_publisher, make_writer

SCAN_T = "warehouse/scanner/dock-01/scan"
STATUS_T = "warehouse/scanner/dock-01/status"


def scan_payload(scanner="dock-01", seq=1, data="8801234567893", **kw):
    return messages.encode(messages.build_scan(scanner, seq, "EAN13", data, **kw))


@pytest.fixture
def store(tmp_path):
    s = ScanStore(str(tmp_path / "barcodes.db"), timeout=0.2)
    yield s
    s.close()


def writer_for(store, tmp_path, port=1, **kw):
    kw.setdefault("retry_delays", (0.01, 0.01))
    return DbWriter(store, Spool(str(tmp_path / "spool.jsonl")), "127.0.0.1", port, **kw)


def msg(topic, payload):
    return SimpleNamespace(topic=topic, payload=payload)


# ================================================================ 분류 (브로커 없이)
def test_handle_classifies(store, tmp_path):
    w = writer_for(store, tmp_path)
    r = "2026-10-11T00:00:00.000+00:00"
    assert w.handle(SCAN_T, scan_payload(), r) == "inserted"
    assert w.handle(STATUS_T, messages.encode(messages.build_status("dock-01", "online")), r) == "status"
    assert w.handle(SCAN_T, b"not json", r) == "rejected"
    assert w.handle("warehouse/other/dock-01/scan", scan_payload(), r) == "rejected"
    assert w.handle("warehouse/scanner/dock-01/scan/extra", scan_payload(), r) == "rejected"
    # 사칭: dock-01 토픽으로 dock-02라고 적힌 메시지
    assert w.handle(SCAN_T, scan_payload(scanner="dock-02"), r) == "rejected"
    assert store.count() == 1
    reasons = [x["reason"] for x in store.rejected()]
    assert any("토픽의 scanner_id(dock-01)" in x for x in reasons)
    assert store.scanners()[0]["state"] == "online"


def test_duplicate_and_conflict_counted(store, tmp_path):
    w = writer_for(store, tmp_path)
    p = scan_payload()
    for _ in range(3):
        w._on_message(msg(SCAN_T, p))
    m = messages.parse_scan(p)
    w._on_message(msg(SCAN_T, messages.encode(dict(m, data="OTHER"))))
    assert store.count() == 1
    assert w.stats["inserted"] == 1 and w.stats["duplicate"] == 2 and w.stats["conflict"] == 1


def test_db_locked_goes_to_spool_then_drains(store, tmp_path):
    w = writer_for(store, tmp_path)
    locker = sqlite3.connect(store.path)
    locker.execute("BEGIN EXCLUSIVE")              # 다른 프로그램이 DB를 잠금
    w._on_message(msg(SCAN_T, scan_payload(seq=1)))
    w._on_message(msg(SCAN_T, scan_payload(seq=2)))
    assert w.stats["spooled"] == 2 and len(w.spool) == 2
    w._drain_spool()                               # 아직 잠겨 있음 → 남겨 둠
    assert len(w.spool) == 2
    locker.rollback()
    locker.close()
    w._drain_spool()
    assert len(w.spool) == 0 and store.count() == 2


def test_fail_stop_when_db_and_spool_fail(store, tmp_path):
    (tmp_path / "spool_is_a_dir").mkdir()          # 보조 파일도 쓸 수 없게
    exits = []
    w = DbWriter(store, Spool(str(tmp_path / "spool_is_a_dir")), "127.0.0.1", 1,
                 retry_delays=(), fatal=exits.append)
    store.close()                                   # DB도 쓸 수 없게
    w._on_message(msg(SCAN_T, scan_payload()))
    assert exits == [3]


# ================================================================ 보안 브로커 통합
def test_publisher_to_db_with_status(secure_broker, store, tmp_path):
    w = make_writer(store, tmp_path, secure_broker)
    box, pub = make_publisher(tmp_path, secure_broker)
    pub.start()
    try:
        ids = add_scans(box, 15)
        pub.notify()
        assert wait_until(lambda: db_ids(store) == set(ids))
        assert wait_until(lambda: store.scanners() and store.scanners()[0]["state"] == "online")
        assert store.scanners()[0]["last_seq"] == 15
    finally:
        pub.stop()
    assert wait_until(lambda: store.scanners()[0]["state"] == "offline")
    w.stop()
    box.close()


def test_persistent_session_receives_while_offline(secure_broker, store, tmp_path):
    """db-writer가 꺼져 있는 동안 발행된 메시지를 다시 켜면 받는다 (영속 세션)."""
    w = make_writer(store, tmp_path, secure_broker)
    w.stop()                                        # 구독 해제 없이 종료
    box, pub = make_publisher(tmp_path, secure_broker)
    pub.start()
    try:
        ids = add_scans(box, 20)
        pub.notify()
        assert wait_until(lambda: box.pending_count() == 0)   # 브로커가 모두 받음
        assert store.count() == 0
        w2 = make_writer(store, tmp_path, secure_broker)
        assert wait_until(lambda: db_ids(store) == set(ids))
        w2.stop()
    finally:
        pub.stop()
        box.close()


def test_duplicate_publishes_stored_once(secure_broker, store, tmp_path):
    w = make_writer(store, tmp_path, secure_broker)
    c = make_client(f"t-{uuid.uuid4().hex[:6]}")
    c.username_pw_set(**creds("dock-01"))
    c.connect(secure_broker.host, secure_broker.port)
    c.loop_start()
    try:
        p = scan_payload()
        for _ in range(3):
            c.publish(SCAN_T, p, qos=1).wait_for_publish(5)
        c.publish(SCAN_T, scan_payload(scanner="dock-02", seq=9), qos=1).wait_for_publish(5)  # 사칭
        assert wait_until(lambda: w.stats["duplicate"] == 2 and w.stats["rejected"] == 1)
        assert store.count() == 1
    finally:
        c.loop_stop()
        c.disconnect()
        w.stop()


class FatalStop(Exception):
    pass


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_fail_stop_message_is_redelivered(secure_broker, tmp_path):
    """DB와 보조 파일이 모두 실패 → PUBACK 전에 멈춤 → 재시작한 db-writer가 같은 메시지를 다시 받는다."""
    (tmp_path / "bad_spool").mkdir()
    broken = ScanStore(str(tmp_path / "broken.db"))
    broken.close()                                  # 쓰기 불가 상태
    exits = []

    def fatal(code):
        exits.append(code)
        raise FatalStop()                           # 실제 서비스는 os._exit(3)

    w1 = DbWriter(broken, Spool(str(tmp_path / "bad_spool")), secure_broker.host, secure_broker.port,
                  keepalive=5, retry_delays=(), fatal=fatal, **creds("db-writer"))
    w1.start()
    assert w1.wait_ready(10)
    box, pub = make_publisher(tmp_path, secure_broker)
    pub.start()
    try:
        ids = add_scans(box, 1)
        pub.notify()
        assert wait_until(lambda: exits == [3])
        good = ScanStore(str(tmp_path / "good.db"))
        w2 = make_writer(good, tmp_path, secure_broker)   # 같은 client_id → 세션 인계
        assert wait_until(lambda: db_ids(good) == set(ids), timeout=15)
        w2.stop()
        good.close()
    finally:
        pub.stop()
        box.close()
        w1._client.loop_stop()


def test_db_lock_during_live_traffic(secure_broker, store, tmp_path):
    w = make_writer(store, tmp_path, secure_broker)
    box, pub = make_publisher(tmp_path, secure_broker)
    pub.start()
    locker = sqlite3.connect(store.path)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        ids = add_scans(box, 5)
        pub.notify()
        assert wait_until(lambda: w.stats["spooled"] == 5, timeout=20)
        locker.rollback()
        assert wait_until(lambda: db_ids(store) == set(ids))   # 보조 파일에서 DB로
        assert len(w.spool) == 0
    finally:
        locker.close()
        pub.stop()
        w.stop()
        box.close()
