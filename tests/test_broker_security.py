"""
브로커 인증·ACL 테스트. mosquitto/pi/acl(파이에 설치할 파일)을 그대로 쓴다.
비밀번호는 conftest의 secure_broker가 'pw-<사용자>'로 만든다.
"""
import time
import uuid

import pytest

from barcode_system import messages
from barcode_system.mqtt_compat import make_client, set_callbacks
from barcode_system.outbox import Outbox
from barcode_system.publisher import Publisher
from conftest import wait_until


def creds(user):
    return {"username": user, "password": f"pw-{user}"}


def raw_client(broker, user):
    """연결된 paho 클라이언트 (loop_start 상태)."""
    c = make_client(f"t-{uuid.uuid4().hex[:8]}")
    c.username_pw_set(user, f"pw-{user}")
    result = {}
    set_callbacks(c, on_connect=lambda ok, reason: result.update(ok=ok, reason=reason))
    c.connect(broker.host, broker.port)
    c.loop_start()
    assert wait_until(lambda: "ok" in result) and result["ok"], result
    return c


def scan_payload(scanner_id, data="X"):
    return messages.encode(messages.build_scan(scanner_id, 1, "CODE128", data))


@pytest.mark.parametrize("username,password", [(None, None), ("dock-01", "wrong"), ("nobody", "x")])
def test_rejects_anonymous_and_bad_credentials(secure_broker, collectors, username, password):
    col = collectors(secure_broker.host, secure_broker.port, username=username, password=password)
    assert wait_until(lambda: col.connect_result is not None)
    ok, reason = col.connect_result
    assert not ok, reason


def test_acl_scanner_can_only_write_own_topics(secure_broker, collectors):
    db = collectors(secure_broker.host, secure_broker.port, "warehouse/#", **creds("db-writer"))
    mon = collectors(secure_broker.host, secure_broker.port, "warehouse/#", **creds("monitor"))
    db.wait_ready()
    mon.wait_ready()
    dock1 = raw_client(secure_broker, "dock-01")
    dbw = raw_client(secure_broker, "db-writer")
    monc = raw_client(secure_broker, "monitor")
    try:
        # 거부돼야 하는 발행 (MQTT 3.1.1에서는 거부돼도 PUBACK이 오므로 발행 측은 알 수 없다)
        for client, topic, data in [
            (dock1, "warehouse/scanner/dock-02/scan", "SPOOF"),   # 다른 스캐너 사칭
            (dock1, "warehouse/other", "OTHER"),                  # 허용 범위 밖
            (monc, "warehouse/scanner/dock-01/scan", "MON"),      # monitor는 읽기 전용
            (dbw, "warehouse/scanner/dock-01/scan", "DBW"),       # db-writer는 읽기 전용
        ]:
            client.publish(topic, scan_payload("dock-01", data), qos=1).wait_for_publish(timeout=5)
        # 허용돼야 하는 발행
        dock1.publish("warehouse/scanner/dock-01/scan", scan_payload("dock-01", "OK"),
                      qos=1).wait_for_publish(timeout=5)
        assert wait_until(lambda: [m["data"] for m in db.scans()] == ["OK"])
        time.sleep(0.5)   # 거부된 메시지가 늦게 도착하지 않는지 잠시 더 확인
        assert [m["data"] for m in db.scans()] == ["OK"]
        assert [m["data"] for m in mon.scans()] == ["OK"]
    finally:
        for c in (dock1, dbw, monc):
            c.loop_stop()
            c.disconnect()


def test_publisher_with_auth_and_lwt(secure_broker, collectors, make_proxy, tmp_path):
    """실제 설정과 같은 방식(사용자 이름 = scanner_id)으로 발행하고, 비정상 끊김 시 LWT가 ACL을 통과하는지."""
    db = collectors(secure_broker.host, secure_broker.port, "warehouse/#", **creds("db-writer"))
    db.wait_ready()
    proxy = make_proxy(secure_broker.host, secure_broker.port)
    box = Outbox(str(tmp_path / "o.db"))
    pub = Publisher(box, "dock-01", "127.0.0.1", proxy.port, keepalive=5, retention_days=None,
                    **creds("dock-01"))
    pub.start()
    try:
        def make(seq):
            msg = messages.build_scan("dock-01", seq, "EAN13", "8801234567893")
            return msg["id"], messages.scan_topic("dock-01"), messages.encode(msg)
        box.add(make)
        pub.notify()
        assert wait_until(lambda: len(db.scans()) == 1)
        assert wait_until(lambda: "online" in [s["state"] for s in db.statuses()])
        proxy.cut()
        assert wait_until(lambda: db.statuses()[-1]["state"] == "lost")
    finally:
        pub.stop()
        box.close()
