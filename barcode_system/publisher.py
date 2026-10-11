"""
outbox → MQTT 발행 스레드.

동작
  - paho 네트워크 스레드(loop_start)가 연결·자동 재연결을 맡는다 (1초~30초 간격으로 늘려 가며 재시도).
  - 발행 스레드 하나가 outbox의 미전송 메시지를 seq 순서로 QoS 1 발행하고, PUBACK을 받으면
    outbox에 '전송 완료'를 기록한다. 연결이 끊겨 있는 동안에는 발행하지 않는다.
  - 브로커 확인을 기다리는 메시지(inflight)는 max_inflight개로 제한한다.
  - ack_timeout 안에 PUBACK이 오지 않으면 같은 메시지(같은 id)를 다시 발행한다.
    재연결 후 paho가 스스로 재전송하는 경우도 있어 중복이 생길 수 있지만, 메시지마다 고유 id가
    있으므로 받는 쪽(2단계 DB)에서 한 번만 반영한다 (at-least-once + 멱등성).
  - paho 콜백은 이벤트 큐에 넣기만 하고 처리는 발행 스레드가 한다 → 상태(inflight)를 스레드 하나만
    다루므로 잠금이 필요 없고, publish() 반환 전에 PUBACK이 먼저 도착하는 경우도 안전하다.
  - 상태 토픽: 연결되면 online(retain), 정상 종료 시 offline(retain),
    비정상 끊김은 브로커가 LWT로 lost(retain)를 대신 발행한다.
"""
import logging
import queue
import threading
import time

from . import messages
from .mqtt_compat import (MQTT_ERR_NO_CONN, MQTT_ERR_SUCCESS, PAHO_VERSION, make_client,
                          set_callbacks)

log = logging.getLogger("barcode.mqtt")

ACK_TIMEOUT = 30.0      # PUBACK을 기다리는 최대 시간(초). 지나면 재발행
POLL_INTERVAL = 0.5     # 새 스캔 알림(notify)이 없어도 outbox를 확인하는 주기(초)
PURGE_INTERVAL = 3600.0 # 보관 기간이 지난 outbox 기록 정리 주기(초)


class Publisher:
    def __init__(self, outbox, scanner_id, host, port=1883, username=None, password=None,
                 keepalive=30, max_inflight=20, ack_timeout=ACK_TIMEOUT, retention_days=7,
                 client_id=None, clock=time.monotonic):
        self.outbox = outbox
        self.scanner_id = messages.validate_scanner_id(scanner_id)
        self.host, self.port, self.keepalive = host, port, keepalive
        self.max_inflight = max_inflight
        self.ack_timeout = ack_timeout
        self.retention_days = retention_days
        self.clock = clock
        self.status_topic = messages.status_topic(scanner_id)

        self._events = queue.Queue()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._connected_evt = threading.Event()
        self._thread = None
        self._inflight = {}          # mid → (seq, 재발행 기한)
        self._connected = False
        self._last_fail_reason = None
        self._last_purge = None
        self.stats = {"published": 0, "acked": 0, "republished": 0}

        c = self._client = make_client(client_id or f"scanner-{scanner_id}", clean_session=True)
        if username:
            c.username_pw_set(username, password)
        # 비정상 끊김(전원·네트워크 단절, 프로세스 강제 종료) 시 브로커가 대신 발행할 메시지
        c.will_set(self.status_topic, messages.encode(messages.build_status(scanner_id, "lost")),
                   qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.max_inflight_messages_set(max_inflight)
        set_callbacks(c,
                      on_connect=lambda ok, reason: self._push("connected", ok, reason),
                      on_disconnect=lambda reason: self._push("disconnected", reason),
                      on_publish=lambda mid: self._push("acked", mid))

    # ------------------------------------------------------------ 공개 API
    def start(self):
        log.info("MQTT 브로커 %s:%d 연결 시작 (paho-mqtt %s, scanner_id=%s)",
                 self.host, self.port, PAHO_VERSION, self.scanner_id)
        # connect_async + loop_start: 첫 연결이 실패해도 예외 없이 백그라운드에서 계속 재시도한다
        self._client.connect_async(self.host, self.port, self.keepalive)
        self._client.loop_start()
        self._thread = threading.Thread(target=self._run, name="mqtt-publisher", daemon=True)
        self._thread.start()

    def notify(self):
        """outbox에 새 메시지를 넣은 뒤 호출 → 다음 확인 주기를 기다리지 않고 바로 발행."""
        self._wake.set()

    @property
    def connected(self):
        return self._connected_evt.is_set()

    def wait_connected(self, timeout=None):
        return self._connected_evt.wait(timeout)

    def stop(self, flush_timeout=0.0):
        """
        flush_timeout초 동안 미전송 메시지가 모두 전송되기를 기다린 뒤 종료한다.
        남은 메시지는 outbox에 그대로 있으므로 다음 실행 때 전송된다.
        """
        deadline = self.clock() + flush_timeout
        while flush_timeout > 0 and self.clock() < deadline and self.outbox.pending_count():
            time.sleep(0.05)
        self._stopping.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._connected:
            # 정상 종료는 LWT가 발행되지 않으므로 offline을 직접 남긴다
            try:
                info = self._client.publish(
                    self.status_topic,
                    messages.encode(messages.build_status(self.scanner_id, "offline")),
                    qos=1, retain=True)
                info.wait_for_publish(timeout=2)
            except Exception as e:
                log.debug("offline 상태 발행 실패: %s", e)
        self._client.disconnect()
        self._client.loop_stop()
        log.info("MQTT 종료 (발행 %d, 확인 %d, 재발행 %d, 미전송 %d)",
                 self.stats["published"], self.stats["acked"], self.stats["republished"],
                 self.outbox.pending_count())

    # ------------------------------------------------------------ 발행 스레드
    def _push(self, *event):
        self._events.put(event)
        self._wake.set()

    def _run(self):
        while not self._stopping.is_set():
            self._wake.wait(POLL_INTERVAL)
            self._wake.clear()   # 처리 중에 들어온 알림은 다음 wait에서 바로 반영된다
            try:
                self._tick()
            except Exception:
                # outbox DB 오류 등 → 스레드가 죽지 않도록 하고 다음 주기에 다시 시도
                log.exception("발행 처리 중 오류")
                self._stopping.wait(1.0)

    def _tick(self):
        self._handle_events()
        if self._connected:
            self._expire_inflight()
            self._fill()
        self._maybe_purge()

    def _handle_events(self):
        while True:
            try:
                kind, *args = self._events.get_nowait()
            except queue.Empty:
                return
            if kind == "connected":
                self._on_connected(*args)
            elif kind == "disconnected":
                if self._connected:
                    log.warning("MQTT 연결 끊김: %s (자동 재연결, 스캔은 outbox에 계속 저장)", args[0])
                self._connected = False
                self._connected_evt.clear()
            elif kind == "acked":
                entry = self._inflight.pop(args[0], None)
                if entry is not None:  # status 발행이나 시간 초과로 포기한 mid의 늦은 확인은 무시
                    self.outbox.mark_sent(entry[0])
                    self.stats["acked"] += 1

    def _on_connected(self, ok, reason):
        if not ok:
            if reason != self._last_fail_reason:  # 같은 이유로 반복 실패하면 한 번만 기록
                log.error("MQTT 연결 거부: %s", reason)
                self._last_fail_reason = reason
            return
        self._last_fail_reason = None
        self._connected = True
        self._connected_evt.set()
        log.info("MQTT 연결됨 (미전송 %d건)", self.outbox.pending_count())
        self._client.publish(self.status_topic,
                             messages.encode(messages.build_status(self.scanner_id, "online")),
                             qos=1, retain=True)
        # 끊기기 전에 보낸 메시지는 paho가 재연결 후 다시 보낼 수 있으므로 기한을 새로 준다
        deadline = self.clock() + self.ack_timeout
        self._inflight = {mid: (seq, deadline) for mid, (seq, _) in self._inflight.items()}

    def _expire_inflight(self):
        now = self.clock()
        for mid, (seq, deadline) in list(self._inflight.items()):
            if now >= deadline:
                del self._inflight[mid]   # 다음 _fill()에서 outbox로부터 다시 발행된다
                self.stats["republished"] += 1
                log.warning("seq %d: %.0f초 안에 브로커 확인이 없어 다시 보냅니다", seq, self.ack_timeout)

    def _fill(self):
        room = self.max_inflight - len(self._inflight)
        if room <= 0:
            return
        busy = {seq for seq, _ in self._inflight.values()}
        for m in self.outbox.pending(room, exclude=busy):
            info = self._client.publish(m.topic, m.payload, qos=1)
            if info.rc not in (MQTT_ERR_SUCCESS, MQTT_ERR_NO_CONN):
                log.warning("발행 실패 seq %d: rc=%s", m.seq, info.rc)
                return
            # NO_CONN: 확인 직후 연결이 끊긴 경우. paho가 큐에 보관했다가 재연결 후 보내거나,
            # 그렇지 않으면 기한이 지나 outbox에서 다시 보낸다.
            self._inflight[info.mid] = (m.seq, self.clock() + self.ack_timeout)
            self.stats["published"] += 1
            if info.rc == MQTT_ERR_NO_CONN:
                return

    def _maybe_purge(self):
        if not self.retention_days:
            return
        now = self.clock()
        if self._last_purge is None or now - self._last_purge >= PURGE_INTERVAL:
            self._last_purge = now
            n = self.outbox.purge(self.retention_days)
            if n:
                log.info("보관 기간(%d일)이 지난 outbox 기록 %d건 정리", self.retention_days, n)
