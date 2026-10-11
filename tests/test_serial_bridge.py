"""UNO 브리지 테스트: 가짜 UNO(시리얼 대신 TCP)로 연결 규칙을 확인하고, 보안 브로커로 전체 흐름을 확인한다."""
import time
from types import SimpleNamespace

import pytest

from barcode_system import messages, serial_bridge
from barcode_system.serial_bridge import LinkError, UnoBridge, UnoLink
from barcode_system.store import ScanStore
from conftest import wait_until
from fake_uno import FakeUno
from helpers import add_scans, creds, make_publisher, make_writer


@pytest.fixture
def uno():
    u = FakeUno()
    yield u
    u.close()


def link_for(uno, **kw):
    kw.setdefault("ready_timeout", 2.0)
    kw.setdefault("ack_timeout", 0.3)
    return UnoLink(uno.url, **kw)


# ================================================================ 시리얼 연결 (UnoLink)
def test_open_waits_for_ready_and_acks(uno):
    link = link_for(uno)
    link.open()
    try:
        assert link.firmware == "uno-display/1"
        assert link.send("S", "EAN13", "dock-01", "8801234567893")
        assert uno.kinds("S")[0][2:] == ["EAN13", "dock-01", "8801234567893"]
    finally:
        link.close()


def test_retransmit_when_ack_lost(uno):
    link = link_for(uno, retries=3)
    link.open()
    try:
        uno.drop_acks(2)
        assert link.send("P")                       # 3번째 시도에서 확인
        assert len(uno.kinds("P")) == 3
        uno.drop_acks(10)
        assert not link.send("P")                   # 1 + 3번 모두 실패
    finally:
        link.close()


def test_board_without_ready_is_accepted_by_ping():
    u = FakeUno(send_ready=False)
    try:
        link = link_for(u, ready_timeout=0.5)
        link.open()                                 # R 없이 P 응답으로 연결 확인
        assert link.firmware is None
        link.close()
    finally:
        u.close()


def test_no_response_raises():
    u = FakeUno(send_ready=False, ack=False)
    try:
        with pytest.raises(LinkError):
            link_for(u, ready_timeout=0.3, retries=1).open()
    finally:
        u.close()


def test_garbage_and_reboot_detected(uno):
    link = link_for(uno)
    link.open()
    try:
        uno.send_raw(b"\x00\xff boot noise\nA|1*00\n")   # 잡음, 체크섬 틀린 확인 → 무시
        assert link.send("P")
        uno.reboot()
        assert wait_until(link.rebooted.is_set, 3)
    finally:
        link.close()


def test_unplug_marks_broken(uno):
    link = link_for(uno)
    link.open()
    uno.unplug()
    assert wait_until(lambda: not link.is_open, 3)
    assert not link.send("P")
    link.close()
    with pytest.raises(LinkError):
        link_for(uno).open()                         # 다시 꽂기 전에는 열리지 않음
    uno.plug()
    link2 = link_for(uno)
    link2.open()
    link2.close()


def test_find_port_prefers_known_usb_ids(monkeypatch):
    ports = [SimpleNamespace(device="COM3", vid=0x8087, pid=0x0001),      # 다른 장치
             SimpleNamespace(device="COM7", vid=0x1A86, pid=0x5523),      # CH341
             SimpleNamespace(device="COM4", vid=None, pid=None)]
    monkeypatch.setattr(serial_bridge.list_ports, "comports", lambda: ports)
    assert serial_bridge.find_port() == ("COM7", "CH341")
    monkeypatch.setattr(serial_bridge.list_ports, "comports", lambda: ports[:1])
    assert serial_bridge.find_port() == (None, None)
    with pytest.raises(LinkError):
        UnoLink("auto").open()


# ================================================================ 이벤트 변환 (MQTT 없이)
def make_bridge(uno, **kw):
    return UnoBridge(link_for(uno), "127.0.0.1", 1, ping_interval=kw.pop("ping_interval", 0.5), **kw)


def mqtt_msg(topic, payload):
    return SimpleNamespace(topic=topic, payload=messages.encode(payload))


def result(kind, data="8801234567893", ts=None, **kw):
    scan = messages.build_scan("dock-01", 3, "EAN13", data)
    if kind == "rejected":
        return messages.build_result("rejected", topic="warehouse/scanner/dock-01/scan",
                                     reason="bad", scanner_id="dock-01", ts=ts)
    return messages.build_result(kind, scan=scan, ts=ts)


def test_results_mapped_to_frames(uno):
    b = make_bridge(uno)
    for kind in ("inserted", "duplicate", "conflict", "rejected"):
        b._on_message(mqtt_msg(messages.RESULT_TOPIC, result(kind)))
    old = result("inserted", data="OLD", ts="2026-01-01T00:00:00+09:00")
    b._on_message(mqtt_msg(messages.RESULT_TOPIC, old))                 # 오래된 결과 → 버림
    b._on_message(SimpleNamespace(topic=messages.RESULT_TOPIC, payload=b"junk"))
    events = [e for e, _ in b._queue]
    assert events == [("S", "EAN13", "dock-01", "8801234567893"), ("X", "dock-01", "bad")]
    assert b.stats["stale"] == 1 and b.stats["invalid"] == 1


def test_status_changes_only(uno):
    b = make_bridge(uno)
    topic = "warehouse/scanner/dock-01/status"
    for state in ("online", "online", "lost", "lost", "online"):
        b._on_message(mqtt_msg(topic, messages.build_status("dock-01", state)))
    b._on_message(mqtt_msg("warehouse/scanner/dock-02/status", messages.build_status("dock-01", "lost")))
    assert [e for e, _ in b._queue] == [("T", "dock-01", "online"), ("T", "dock-01", "lost"),
                                        ("T", "dock-01", "online")]
    assert b.stats["invalid"] == 1                   # 토픽과 내용이 다른 상태 메시지


def test_bridge_loop_sends_reconnects_and_resyncs(uno):
    b = make_bridge(uno, max_age=30)
    b.start()                                        # MQTT는 접속 실패해도 시리얼 쪽은 동작
    try:
        b._on_message(mqtt_msg("warehouse/scanner/dock-01/status", messages.build_status("dock-01", "lost")))
        b._on_message(mqtt_msg(messages.RESULT_TOPIC, result("inserted")))
        assert wait_until(lambda: uno.kinds("S") and uno.kinds("T"), 5)
        assert wait_until(lambda: len(uno.kinds("P")) >= 1, 3)       # 대기 중이면 연결 확인

        uno.unplug()                                 # USB 분리 → 재연결 시도
        time.sleep(1.0)
        uno.plug()
        assert wait_until(lambda: uno.connections >= 2, 15)
        # 재연결 후 알고 있던 스캐너 상태(lost)를 다시 보냄
        assert wait_until(lambda: len(uno.kinds("T")) >= 2, 5)

        before = len(uno.kinds("T"))
        uno.reboot()                                 # 연결 중 UNO 리셋 → 상태 재전송
        assert wait_until(lambda: len(uno.kinds("T")) > before, 5)
    finally:
        b.stop()


def test_queue_drops_stale_events_while_unplugged(uno):
    clock = SimpleNamespace(t=100.0)
    b = make_bridge(uno, max_age=10, clock=lambda: clock.t)
    b._enqueue(("S", "EAN13", "dock-01", "OLD"), created=100.0)
    clock.t = 120.0                                  # 시리얼이 끊겨 있는 동안 20초 경과
    b._enqueue(("S", "EAN13", "dock-01", "NEW"), created=120.0)
    b.link.open()
    try:
        b._drain()
        assert [f[4] for f in uno.kinds("S")] == ["NEW"]
        assert b.stats["stale"] == 1
    finally:
        b.link.close()


# ================================================================ 전체 흐름 (보안 브로커)
def test_publisher_db_writer_bridge_uno(secure_broker, uno, tmp_path):
    store = ScanStore(str(tmp_path / "barcodes.db"))
    w = make_writer(store, tmp_path, secure_broker)
    bridge = UnoBridge(link_for(uno), secure_broker.host, secure_broker.port, keepalive=5,
                       ping_interval=1.0, **creds("uno-bridge"))
    bridge.start()
    assert bridge.wait_ready(10)
    box, pub = make_publisher(tmp_path, secure_broker)
    pub.start()
    try:
        ids = add_scans(box, 3)
        pub.notify()
        # DB에 저장된 뒤에야 UNO에 S가 간다
        assert wait_until(lambda: len(uno.kinds("S")) == 3, 15)
        assert {r["id"] for r in store.scans()} == set(ids)
        assert [f[4] for f in uno.kinds("S")] == ["ITEM-00001", "ITEM-00002", "ITEM-00003"]
        assert wait_until(lambda: ["dock-01", "online"] in [f[2:] for f in uno.kinds("T")])
    finally:
        pub.stop()
    # 정상 종료 → offline 상태도 UNO로
    assert wait_until(lambda: uno.kinds("T")[-1][3] == "offline", 10)
    bridge.stop()
    w.stop()
    box.close()
    store.close()
