"""
MQTT 구독 → DB 저장 (2단계 db-writer).

전달 보장
  - 고정 client id + clean_session=False(영속 세션) + QoS 1 구독
    → db-writer가 꺼져 있는 동안의 메시지를 브로커가 보관했다가 다시 접속하면 전달한다.
  - paho는 on_message 콜백이 끝난 '뒤에' PUBACK을 보낸다 (1.x / 2.x 동일).
    콜백 안에서 DB commit까지 끝내므로, 기록 전에 죽으면 PUBACK이 나가지 않아 브로커가 다시 보낸다.
  - DB 기록 실패 → 짧게 재시도 → 그래도 실패하면 보조 파일(spool)에 남기고 주기적으로 DB로 옮긴다.
  - 보조 파일 기록까지 실패하면(디스크 고장 등) PUBACK을 보내기 전에 프로세스를 즉시 끝낸다(fail-stop).
    브로커가 메시지를 다시 보내고, systemd가 서비스를 재시작한다.
  - 같은 메시지가 여러 번 와도 scans.id(PRIMARY KEY)로 한 번만 저장된다.

검증
  - 형식이 틀린 메시지, 토픽의 scanner_id와 내용의 scanner_id가 다른 메시지(사칭)는
    scans에 넣지 않고 rejected 테이블에 원본과 사유를 남긴다.
"""
import logging
import os
import sqlite3
import threading
import time
from collections import Counter

from . import messages
from .mqtt_compat import PAHO_VERSION, make_client, set_callbacks
from .store import ScanStore, utc_now

log = logging.getLogger("barcode.db")

SCAN_FILTER = f"{messages.TOPIC_ROOT}/+/scan"
STATUS_FILTER = f"{messages.TOPIC_ROOT}/+/status"
RETRY_DELAYS = (0.1, 0.5, 1.0)   # DB 기록 재시도 간격(초). 합계 약 1.6초 동안 paho 스레드를 붙잡는다
SPOOL_INTERVAL = 10.0            # 보조 파일 → DB 재처리 주기(초)
FATAL_EXIT_CODE = 3


def fail_stop(code=FATAL_EXIT_CODE):
    """PUBACK을 보내기 전에 프로세스를 즉시 끝낸다 (finally·atexit도 실행하지 않음)."""
    logging.shutdown()
    os._exit(code)


class DbWriter:
    def __init__(self, store, spool, host, port=1883, username=None, password=None,
                 client_id="db-writer", keepalive=30, fatal=fail_stop, retry_delays=RETRY_DELAYS,
                 spool_interval=SPOOL_INTERVAL):
        self.store, self.spool = store, spool
        self.host, self.port, self.keepalive = host, port, keepalive
        self.client_id = client_id
        self.fatal = fatal
        self.retry_delays = retry_delays
        self.spool_interval = spool_interval
        self.stats = Counter()
        self._stats_lock = threading.Lock()
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._last_fail_reason = None
        self._spool_thread = None

        c = self._client = make_client(client_id, clean_session=False)
        if username:
            c.username_pw_set(username, password)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        set_callbacks(c, on_connect=self._on_connect, on_disconnect=self._on_disconnect,
                      on_message=self._on_message)
        c.on_subscribe = lambda *a: self._ready.set()  # 인자 형식이 버전마다 달라 *a로 받음

    # ------------------------------------------------------------ 공개 API
    def start(self):
        log.info("MQTT 브로커 %s:%d 연결 시작 (paho-mqtt %s, client_id=%s, 영속 세션)",
                 self.host, self.port, PAHO_VERSION, self.client_id)
        self._drain_spool()  # 지난 실행에서 남은 보조 파일 먼저
        self._client.connect_async(self.host, self.port, self.keepalive)
        self._client.loop_start()
        self._spool_thread = threading.Thread(target=self._spool_loop, name="spool", daemon=True)
        self._spool_thread.start()

    def wait_ready(self, timeout=None):
        """연결되고 구독이 확인될 때까지 기다린다."""
        return self._ready.wait(timeout)

    def stop(self):
        """정상 종료. 구독을 해제하지 않으므로(영속 세션) 꺼져 있는 동안의 메시지는 브로커가 보관한다."""
        self._stopping.set()
        if self._spool_thread:
            self._spool_thread.join(timeout=5)
        self._client.disconnect()
        self._client.loop_stop()
        self._drain_spool()
        log.info("DB 저장 종료 (%s)", self.stats_text())

    def stats_text(self):
        with self._stats_lock:
            s = dict(self.stats)
        return ", ".join(f"{k} {v}" for k, v in sorted(s.items())) or "처리 없음"

    # ------------------------------------------------------------ 메시지 처리
    def handle(self, topic, payload, received_at):
        """메시지 1건을 분류해 DB에 반영하고 결과 이름을 반환한다. DB 오류는 sqlite3.Error로 올린다."""
        parts = topic.split("/")
        root = messages.TOPIC_ROOT.split("/")
        if len(parts) != len(root) + 2 or parts[:len(root)] != root or parts[-1] not in ("scan", "status"):
            return self._reject(topic, payload, "알 수 없는 토픽", received_at)
        topic_id, kind = parts[-2], parts[-1]
        try:
            msg = messages.parse_scan(payload) if kind == "scan" else messages.parse_status(payload)
        except ValueError as e:
            return self._reject(topic, payload, str(e), received_at)
        if msg["scanner_id"] != topic_id:
            return self._reject(topic, payload,
                                f"토픽의 scanner_id({topic_id})와 내용({msg['scanner_id']})이 다름",
                                received_at)

        if kind == "status":
            self.store.update_status(msg, received_at)
            log.info("스캐너 %s 상태: %s", msg["scanner_id"], msg["state"])
            return self._count("status")

        result = self.store.save_scan(msg, received_at)
        if result == ScanStore.INSERTED:
            log.info("저장 [%s] %s (%s #%d)", msg["type"], msg["data"], msg["scanner_id"], msg["seq"])
        elif result == ScanStore.CONFLICT:
            log.warning("같은 id(%s)로 내용이 다른 메시지 — 기존 기록 유지", msg["id"])
        return self._count(result)

    def _reject(self, topic, payload, reason, received_at):
        self.store.reject(topic, payload, reason, received_at)
        log.warning("거부: %s (%s)", topic, reason)
        return self._count("rejected")

    def _count(self, name):
        with self._stats_lock:
            self.stats[name] += 1
        return name

    def _on_message(self, msg):
        topic, payload, received_at = msg.topic, bytes(msg.payload), utc_now()
        delays = list(self.retry_delays)
        while True:
            try:
                self.handle(topic, payload, received_at)
                return
            except sqlite3.Error as e:
                if not delays:
                    error = e
                    break
                log.debug("DB 기록 재시도: %s", e)
                time.sleep(delays.pop(0))
            except Exception as e:  # 예상하지 못한 오류도 메시지를 잃지 않도록 보조 파일로
                log.exception("메시지 처리 중 오류")
                error = e
                break
        try:
            self.spool.append(topic, payload, received_at)
            self._count("spooled")
            log.warning("DB 기록 실패 → 보조 파일에 보관: %s (%s)", topic, error)
        except Exception as e2:
            log.critical("DB와 보조 파일 모두 기록 실패 (%s / %s). PUBACK 전에 종료합니다 → 브로커가 재전송",
                         error, e2)
            self.fatal(FATAL_EXIT_CODE)

    # ------------------------------------------------------------ 연결·보조 파일
    def _on_connect(self, ok, reason):
        if not ok:
            if reason != self._last_fail_reason:
                log.error("MQTT 연결 거부: %s", reason)
                self._last_fail_reason = reason
            return
        self._last_fail_reason = None
        log.info("MQTT 연결됨 → %s, %s 구독", SCAN_FILTER, STATUS_FILTER)
        self._client.subscribe([(SCAN_FILTER, 1), (STATUS_FILTER, 1)])

    def _on_disconnect(self, reason):
        self._ready.clear()
        if not self._stopping.is_set():
            log.warning("MQTT 연결 끊김: %s (자동 재연결, 그동안의 메시지는 브로커가 보관)", reason)

    def _spool_loop(self):
        while not self._stopping.wait(self.spool_interval):
            self._drain_spool()

    def _drain_spool(self):
        """보조 파일을 DB로 옮긴다. 여기서 나는 오류는 기록만 하고 넘어간다
        (보조 파일은 비상용 경로라서, 이것 때문에 시작이 막히거나 재처리 스레드가 죽으면 안 된다)."""
        try:
            if not len(self.spool):
                return
            n = self.spool.drain(self.handle)
            log.info("보조 파일 %d건을 DB에 반영", n)
        except (sqlite3.Error, OSError) as e:
            log.warning("보조 파일 반영 실패, 나중에 다시 시도: %s", e)
        except Exception:
            log.exception("보조 파일 반영 중 오류")
