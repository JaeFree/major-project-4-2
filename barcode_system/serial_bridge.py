"""
MQTT → USB 시리얼 → 아두이노 UNO 브리지 (3단계 uno-bridge).

UNO에는 네트워크가 없으므로 파이에서 이 브리지가 MQTT를 구독해 시리얼로 넘긴다.
  구독: warehouse/db/result (db-writer가 DB에 반영한 뒤 발행) + warehouse/scanner/+/status
  표시: inserted → S, rejected → X, 스캐너 상태 → T  (duplicate/conflict는 같은 스캔의 재전달이라 표시 안 함)

표시는 기록이 아니다 (기록은 DB). 그래서
  - 영속 세션을 쓰지 않고(clean_session=True), max_age초보다 오래된 결과는 버린다.
    브리지가 꺼졌다 켜졌을 때 지난 스캔이 몰아서 표시되면 현장에서 혼란스럽기 때문이다.
  - 스캐너 상태는 '현재 상태'라 나이와 관계없이 반영하고, UNO가 재부팅·재연결되면 다시 보낸다.

시리얼 연결 (UnoLink)
  - 포트 'auto': CH340/CH341 호환보드, 정품 UNO의 USB 장치 ID로 찾는다.
  - 포트를 열면 UNO가 리셋되므로 R(부팅 완료)을 기다린 뒤 보낸다. R이 없으면 P(연결 확인)로 대신 확인.
  - 프레임마다 A|n 확인을 ack_timeout 안에 못 받으면 retries번 다시 보낸다.
  - 연속 실패·포트 오류(USB 분리) → 닫고 다시 연결 (1초 → 최대 10초 간격).
  - 대기 중인 것이 없으면 ping_interval마다 P를 보낸다 (UNO는 오래 안 오면 NO LINK 표시).
"""
import collections
import logging
import threading
import time
from datetime import datetime

import serial
from serial.tools import list_ports

from . import messages, uno_protocol
from .mqtt_compat import PAHO_VERSION, make_client, set_callbacks

log = logging.getLogger("barcode.uno")

STATUS_FILTER = f"{messages.TOPIC_ROOT}/+/status"
# (VID, PID): CH340 / CH341 / CH340K·CH9340 계열, 정품 UNO R3·R4, 구형 UNO
USB_IDS = {
    (0x1A86, 0x7523): "CH340", (0x1A86, 0x5523): "CH341", (0x1A86, 0x7522): "CH340K",
    (0x2341, 0x0043): "Arduino UNO", (0x2341, 0x0001): "Arduino UNO", (0x2A03, 0x0043): "Arduino UNO",
    (0x2341, 0x0069): "Arduino UNO R4", (0x2341, 0x1002): "Arduino UNO R4",
}


def find_port():
    """알려진 USB 장치 ID의 시리얼 포트 → (장치 경로, 설명) 또는 (None, None)."""
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        name = USB_IDS.get((p.vid, p.pid))
        if name:
            return p.device, name
    return None, None


class LinkError(Exception):
    pass


class UnoLink:
    """UNO와의 시리얼 연결 1개."""

    def __init__(self, port="auto", baud=115200, ready_timeout=4.0, ack_timeout=0.5, retries=3,
                 opener=serial.serial_for_url):
        self.port, self.baud = port, baud
        self.ready_timeout, self.ack_timeout, self.retries = ready_timeout, ack_timeout, retries
        self.opener = opener
        self.ser = None
        self.device = None
        self.firmware = None
        self._n = 0
        self._cond = threading.Condition()
        self._acked = set()
        self._ready = False
        self.rebooted = threading.Event()   # 연결 중에 UNO가 다시 R을 보냄(리셋) → 상태 재전송 필요
        self.broken = threading.Event()
        self._reader = None

    # ------------------------------------------------------------ 열기·닫기
    def open(self):
        device = self.port
        if device == "auto":
            device, name = find_port()
            if device is None:
                raise LinkError("UNO 시리얼 포트를 찾을 수 없습니다 (CH340/CH341/정품 UNO)")
            log.info("UNO 포트 자동 선택: %s (%s)", device, name)
        try:
            self.ser = self.opener(device, baudrate=self.baud, timeout=0.05)
        except (serial.SerialException, OSError, ValueError) as e:
            raise LinkError(f"포트 {device}를 열 수 없습니다: {e}") from e
        self.device = device
        self.broken.clear()
        self.rebooted.clear()
        with self._cond:
            self._acked.clear()
            self._ready = False
        self._reader = threading.Thread(target=self._read_loop, name="uno-reader", daemon=True)
        self._reader.start()
        # 포트를 열면 UNO가 리셋된다(부트로더 약 1.6초) → R을 기다린다
        with self._cond:
            self._cond.wait_for(lambda: self._ready or self.broken.is_set(), self.ready_timeout)
            ready = self._ready
        if not ready and not self.send("P"):
            self.close()
            raise LinkError(f"{device}: UNO 응답 없음 (펌웨어 업로드·보드 확인)")
        self.rebooted.clear()
        log.info("UNO 연결됨: %s (펌웨어 %s)", device, self.firmware or "확인 안 됨")

    def close(self):
        ser, self.ser = self.ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        if self._reader:
            self._reader.join(timeout=2)
        self.broken.set()

    @property
    def is_open(self):
        return self.ser is not None and not self.broken.is_set()

    # ------------------------------------------------------------ 송신
    def send(self, kind, *fields):
        """프레임 1개를 보내고 확인(A|n)을 기다린다. 확인을 받으면 True."""
        self._n = self._n % 65535 + 1
        n = str(self._n)
        line = uno_protocol.encode(kind, n, *fields)
        for attempt in range(1 + self.retries):
            if self.broken.is_set() or self.ser is None:
                return False
            try:
                self.ser.write(line)
                self.ser.flush()
            except (serial.SerialException, OSError) as e:
                log.warning("시리얼 쓰기 오류: %s", e)
                self.broken.set()
                return False
            with self._cond:
                if self._cond.wait_for(lambda: n in self._acked or self.broken.is_set(), self.ack_timeout):
                    if n in self._acked:
                        self._acked.discard(n)
                        return True
            if attempt < self.retries:
                log.debug("UNO 확인 없음 → 재전송 %s (%d)", line.strip(), attempt + 1)
        return False

    # ------------------------------------------------------------ 수신
    def _read_loop(self):
        buf = b""
        while True:
            ser = self.ser
            if ser is None:
                return
            try:
                chunk = ser.read(64)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if self.ser is not None:
                    log.warning("시리얼 연결 끊김: %s", e)
                self.broken.set()
                with self._cond:
                    self._cond.notify_all()
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._on_line(line)
            if len(buf) > 256:      # 줄바꿈 없는 잡음이 계속 들어오는 경우
                buf = b""

    def _on_line(self, line):
        try:
            fields = uno_protocol.decode(line)
        except ValueError as e:
            log.debug("UNO에서 해석할 수 없는 줄: %s", e)   # 부팅 잡음 등
            return
        kind = fields[0]
        with self._cond:
            if kind == "A" and len(fields) >= 2:
                self._acked.add(fields[1])
                if len(self._acked) > 100:          # 늦게 온 확인이 쌓이지 않도록
                    self._acked = set(list(self._acked)[-50:])
            elif kind == "R":
                self.firmware = "/".join(fields[1:]) or "?"
                if self._ready:
                    log.warning("UNO가 다시 시작됨 (리셋·전원 문제?) → 상태를 다시 보냅니다")
                    self.rebooted.set()
                self._ready = True
            elif kind == "E":
                log.warning("UNO 오류 알림: %s", fields[1:] or "?")
            self._cond.notify_all()


class UnoBridge:
    def __init__(self, link, host, port=1883, username=None, password=None, client_id="uno-bridge",
                 keepalive=30, max_age=30.0, ping_interval=5.0, queue_size=100, clock=time.monotonic):
        self.link = link
        self.host, self.port, self.keepalive = host, port, keepalive
        self.max_age, self.ping_interval = max_age, ping_interval
        self.clock = clock
        self.states = {}                     # scanner_id → 현재 상태
        self.stats = collections.Counter()
        self._queue = collections.deque(maxlen=queue_size)   # 넘치면 가장 오래된 것부터 버림
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._thread = None

        c = self._client = make_client(client_id, clean_session=True)
        if username:
            c.username_pw_set(username, password)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        set_callbacks(c, on_connect=self._on_connect, on_message=self._on_message,
                      on_disconnect=lambda reason: (self._ready.clear(),
                                                    log.warning("MQTT 연결 끊김: %s", reason)))
        c.on_subscribe = lambda *a: self._ready.set()

    # ------------------------------------------------------------ 공개 API
    def start(self):
        log.info("MQTT 브로커 %s:%d 연결 시작 (paho-mqtt %s)", self.host, self.port, PAHO_VERSION)
        self._client.connect_async(self.host, self.port, self.keepalive)
        self._client.loop_start()
        self._thread = threading.Thread(target=self._run, name="uno-bridge", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout=None):
        return self._ready.wait(timeout)

    def stop(self):
        self._stopping.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._client.disconnect()
        self._client.loop_stop()
        self.link.close()
        log.info("UNO 브리지 종료 (%s)", ", ".join(f"{k} {v}" for k, v in sorted(self.stats.items())))

    # ------------------------------------------------------------ MQTT → 표시 이벤트
    def _on_connect(self, ok, reason):
        if not ok:
            log.error("MQTT 연결 거부: %s", reason)
            return
        log.info("MQTT 연결됨 → %s, %s 구독", messages.RESULT_TOPIC, STATUS_FILTER)
        self._client.subscribe([(messages.RESULT_TOPIC, 1), (STATUS_FILTER, 1)])

    def _on_message(self, msg):
        try:
            if msg.topic == messages.RESULT_TOPIC:
                self._on_result(messages.parse_result(msg.payload))
            elif msg.topic.endswith("/status"):
                st = messages.parse_status(msg.payload)
                if msg.topic.split("/")[-2] != st["scanner_id"]:
                    raise ValueError("토픽과 scanner_id가 다름")
                if self.states.get(st["scanner_id"]) != st["state"]:
                    self.states[st["scanner_id"]] = st["state"]
                    self._enqueue(("T", st["scanner_id"], st["state"]), None)
        except ValueError as e:
            self.stats["invalid"] += 1
            log.warning("해석할 수 없는 메시지 (%s): %s", msg.topic, e)

    def _on_result(self, r):
        if r["result"] == "inserted":
            event = ("S", r["type"], r["scanner_id"], r["data"])
        elif r["result"] == "rejected":
            event = ("X", r.get("scanner_id") or "?", r["reason"])
        else:
            return                                   # duplicate / conflict: 표시하지 않음
        age = self._age(r["ts"])
        if age > self.max_age:
            self.stats["stale"] += 1
            log.debug("오래된 결과 버림 (%.0f초 전): %s", age, event)
            return
        self._enqueue(event, self.clock() - max(age, 0.0))

    @staticmethod
    def _age(ts):
        return (datetime.now().astimezone() - datetime.fromisoformat(ts)).total_seconds()

    def _enqueue(self, event, created):
        with self._lock:
            self._queue.append((event, created))
        self._wake.set()

    # ------------------------------------------------------------ 시리얼 송신 스레드
    def _run(self):
        delay = 1.0
        last_error = None
        while not self._stopping.is_set():
            if not self.link.is_open:
                try:
                    self.link.open()
                    delay, last_error = 1.0, None
                    self._resync_states()
                except LinkError as e:
                    if str(e) != last_error:
                        log.error("%s (%.0f초 후 다시 시도)", e, delay)
                        last_error = str(e)
                    self._stopping.wait(delay)
                    delay = min(delay * 2, 10.0)
                    continue
            if self.link.rebooted.is_set():
                self.link.rebooted.clear()
                self._resync_states()
            self._wake.wait(self.ping_interval)
            self._wake.clear()
            if not self._drain() and not self._stopping.is_set():
                if not self._send("P"):
                    self._reconnect("연결 확인(P) 응답 없음")

    def _drain(self):
        """큐의 이벤트를 모두 보낸다. 하나라도 보냈으면 True."""
        sent = False
        fails = 0
        while not self._stopping.is_set():
            with self._lock:
                if not self._queue:
                    return sent
                event, created = self._queue.popleft()
            if created is not None and self.clock() - created > self.max_age:
                self.stats["stale"] += 1      # 시리얼이 끊겨 있던 동안 오래된 것
                continue
            if self._send(*event):
                sent = True
                fails = 0
            else:
                self.stats["send_failed"] += 1
                fails += 1
                if fails >= 2 or not self.link.is_open:
                    self._reconnect("확인 응답 없음")
                    return sent
        return sent

    def _send(self, kind, *fields):
        ok = self.link.send(kind, *fields)
        if ok:
            self.stats[f"sent_{kind}"] += 1
        return ok

    def _resync_states(self):
        """UNO가 새로 시작됐거나 다시 연결됨 → 아는 스캐너 상태를 다시 보낸다."""
        for sid, state in list(self.states.items()):
            self._send("T", sid, state)

    def _reconnect(self, reason):
        log.warning("UNO 다시 연결: %s", reason)
        self.link.close()
