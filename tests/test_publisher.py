"""Publisher 테스트: 가짜 클라이언트로 발행 규칙을 확인하고, 로컬 Mosquitto로 유실 없음을 확인한다."""
import time
from types import SimpleNamespace

import pytest

from barcode_system import messages, publisher as publisher_mod
from barcode_system.mqtt_compat import MQTT_ERR_NO_CONN
from barcode_system.outbox import Outbox
from barcode_system.publisher import Publisher
from conftest import wait_until

SCANNER = "dock-01"


def add_scans(box, n, scanner=SCANNER):
    ids = []
    for _ in range(n):
        def make(seq):
            msg = messages.build_scan(scanner, seq, "CODE128", f"ITEM-{seq:05d}")
            ids.append(msg["id"])
            return msg["id"], messages.scan_topic(scanner), messages.encode(msg)
        box.add(make)
    return ids


@pytest.fixture
def box(tmp_path):
    b = Outbox(str(tmp_path / "outbox.db"))
    yield b
    b.close()


# ================================================================ 가짜 클라이언트 (발행 규칙)
class FakeClient:
    def __init__(self):
        self.published = []   # (mid, topic, payload, retain)
        self.next_mid = 1
        self.rc = 0

    def publish(self, topic, payload, qos=0, retain=False):
        mid, self.next_mid = self.next_mid, self.next_mid + 1
        self.published.append((mid, topic, payload, retain))
        return SimpleNamespace(rc=self.rc, mid=mid, wait_for_publish=lambda timeout=None: True)

    def scans(self):
        return [(mid, messages.parse_scan(p)) for mid, t, p, _ in self.published if t.endswith("/scan")]

    def __getattr__(self, name):  # connect_async, will_set 등 나머지는 아무 일도 하지 않음
        return lambda *a, **k: None


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def fake(monkeypatch, box):
    client, clock = FakeClient(), FakeClock()
    monkeypatch.setattr(publisher_mod, "make_client", lambda *a, **k: client)
    pub = Publisher(box, SCANNER, "unused", max_inflight=5, ack_timeout=10, retention_days=None,
                    clock=clock)
    return SimpleNamespace(pub=pub, client=client, clock=clock, box=box)


def connect(f):
    f.pub._push("connected", True, "Success")
    f.pub._tick()


def test_no_publish_until_connected(fake):
    add_scans(fake.box, 3)
    fake.pub._tick()
    assert fake.client.published == []
    connect(fake)
    assert [m["seq"] for _, m in fake.client.scans()] == [1, 2, 3]
    # 연결 직후 online 상태를 retain으로 발행
    status = [(t, p, r) for _, t, p, r in fake.client.published if t.endswith("/status")]
    assert messages.parse_status(status[0][1])["state"] == "online" and status[0][2] is True


def test_inflight_window_and_acks(fake):
    add_scans(fake.box, 12)
    connect(fake)
    sent = fake.client.scans()
    assert len(sent) == 5                              # max_inflight
    for mid, _ in sent:
        fake.pub._push("acked", mid)
    fake.pub._tick()
    assert fake.box.pending_count() == 7
    assert [m["seq"] for _, m in fake.client.scans()][5:] == [6, 7, 8, 9, 10]


def test_ack_timeout_republishes_same_message(fake):
    ids = add_scans(fake.box, 1)
    connect(fake)
    (old_mid, first), = fake.client.scans()
    fake.clock.t += 11                                 # ack_timeout 경과
    fake.pub._tick()
    (_, again), (new_mid, second) = fake.client.scans()
    assert second == first and second["id"] == ids[0]  # 같은 id로 재발행 → 받는 쪽에서 중복 제거
    fake.pub._push("acked", old_mid)                   # 포기한 mid의 늦은 확인은 무시
    fake.pub._tick()
    assert fake.box.pending_count() == 1
    fake.pub._push("acked", new_mid)
    fake.pub._tick()
    assert fake.box.pending_count() == 0
    assert fake.pub.stats["republished"] == 1


def test_disconnect_pauses_and_reconnect_extends_deadline(fake):
    add_scans(fake.box, 1)
    connect(fake)
    (mid, _), = fake.client.scans()
    fake.pub._push("disconnected", "Unspecified error")
    add_scans(fake.box, 2)
    fake.clock.t += 60                                 # 끊긴 동안 시간이 지나도
    fake.pub._tick()
    assert len(fake.client.scans()) == 1               # 발행하지 않고
    connect(fake)                                      # 재연결 시 기존 inflight에 기한을 새로 준다
    assert [m["seq"] for _, m in fake.client.scans()] == [1, 2, 3]  # seq 1은 재발행되지 않음
    fake.pub._push("acked", mid)                       # paho가 재전송한 seq 1의 확인
    fake.pub._tick()
    assert [r.seq for r in fake.box.pending()] == [2, 3]


def test_no_conn_rc_keeps_message_inflight(fake):
    add_scans(fake.box, 3)
    fake.client.rc = MQTT_ERR_NO_CONN                  # 확인 직후 연결이 끊긴 경우
    connect(fake)
    assert len(fake.client.scans()) == 1               # 첫 메시지 이후 중단
    assert len(fake.pub._inflight) == 1


def test_connect_refused_is_logged_once(fake, caplog):
    for _ in range(3):
        fake.pub._push("connected", False, "Not authorized")
    fake.pub._tick()
    assert sum("연결 거부" in r.message for r in caplog.records) == 1
    assert not fake.pub._connected


# ================================================================ 로컬 Mosquitto (통합)
def make_pub(box, port, **kw):
    kw.setdefault("ack_timeout", 3)
    return Publisher(box, SCANNER, "127.0.0.1", port, keepalive=5, retention_days=None, **kw)


def test_delivers_in_order_with_status(anon_broker, collectors, box):
    col = collectors(anon_broker.host, anon_broker.port).wait_ready()
    ids = add_scans(box, 10)
    pub = make_pub(box, anon_broker.port)
    pub.start()
    try:
        assert wait_until(lambda: col.scan_ids() >= set(ids))
        assert wait_until(lambda: box.pending_count() == 0)
        assert [m["seq"] for m in col.scans()] == list(range(1, 11))
        assert "online" in [s["state"] for s in col.statuses()]
    finally:
        pub.stop()
    assert wait_until(lambda: col.statuses()[-1]["state"] == "offline")   # 정상 종료 → offline


def test_network_outage_no_loss_and_lwt(anon_broker, make_proxy, collectors, box):
    """스캐너↔브로커 연결만 끊는다. 끊긴 동안의 스캔이 재연결 후 모두 도착해야 한다."""
    col = collectors(anon_broker.host, anon_broker.port).wait_ready()
    proxy = make_proxy(anon_broker.host, anon_broker.port)
    pub = make_pub(box, proxy.port)
    pub.start()
    try:
        ids = add_scans(box, 3)
        pub.notify()
        assert wait_until(lambda: col.scan_ids() >= set(ids))

        proxy.cut()
        assert wait_until(lambda: not pub.connected)
        # 브로커는 스캐너 연결이 비정상적으로 끊긴 것을 보고 LWT(lost)를 발행
        assert wait_until(lambda: "lost" in [s["state"] for s in col.statuses()])
        ids += add_scans(box, 20)
        pub.notify()
        time.sleep(1.0)
        assert box.pending_count() == 20                  # 끊긴 동안은 outbox에 보관

        proxy.restore()
        assert wait_until(lambda: box.pending_count() == 0, timeout=30)
        assert wait_until(lambda: col.scan_ids() >= set(ids))
        assert col.statuses()[-1]["state"] == "online"
    finally:
        pub.stop()
    assert len(col.scan_ids()) == 23                       # 고유 id 기준 정확히 23건


def test_broker_restart_and_offline_start(anon_broker, collectors, box):
    anon_broker.stop()
    pub = make_pub(box, anon_broker.port)
    pub.start()                                            # 브로커 없이 시작해도 예외 없음
    try:
        ids = add_scans(box, 5)
        pub.notify()
        time.sleep(1.5)
        assert not pub.connected and box.pending_count() == 5

        anon_broker.start()
        assert wait_until(lambda: box.pending_count() == 0, timeout=40)

        anon_broker.stop()                                 # 운영 중 브로커 재시작
        assert wait_until(lambda: not pub.connected)
        ids += add_scans(box, 5)
        pub.notify()
        anon_broker.start()
        col = collectors(anon_broker.host, anon_broker.port).wait_ready()
        assert wait_until(lambda: box.pending_count() == 0, timeout=40)
        late = add_scans(box, 1)                           # 재시작 뒤에도 정상 발행
        pub.notify()
        assert wait_until(lambda: col.scan_ids() >= set(late))
    finally:
        pub.stop()
