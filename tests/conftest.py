"""공용 pytest 도구: 로컬 Mosquitto 브로커, 메시지 수집기, 네트워크 단절 흉내용 TCP 프록시."""
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from barcode_system import messages  # noqa: E402
from barcode_system.mqtt_compat import make_client, set_callbacks  # noqa: E402

BROKER_CONF = ROOT / "mosquitto" / "local-test.conf"
ACL_FILE = ROOT / "mosquitto" / "pi" / "acl"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18830
SECURE_USERS = ("dock-01", "dock-02", "db-writer", "monitor")


def find_mosquitto(name="mosquitto"):
    """MOSQUITTO_DIR 환경 변수 → PATH → 기본 설치 경로 순으로 찾는다."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    cands = [Path(os.environ["MOSQUITTO_DIR"]) / exe] if os.environ.get("MOSQUITTO_DIR") else []
    cands += [shutil.which(name), Path(r"D:\Tools\mosquitto") / exe, Path("/usr/sbin") / exe]
    for cand in cands:
        if cand and Path(cand).is_file():
            return str(cand)
    return None


def port_open(host, port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def free_port():
    with socket.socket() as s:
        s.bind((BROKER_HOST, 0))
        return s.getsockname()[1]


def wait_until(pred, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ---------------------------------------------------------------- 브로커
class Broker:
    """mosquitto 프로세스를 켜고 끄는 도우미. 껐다 켜도 같은 포트를 쓴다."""

    def __init__(self, conf, port):
        self.exe = find_mosquitto()
        self.conf, self.host, self.port = str(conf), BROKER_HOST, port
        self.proc = None

    def start(self):
        if port_open(self.host, self.port):
            raise RuntimeError(f"{self.port} 포트를 이미 다른 프로세스가 쓰고 있습니다")
        self.proc = subprocess.Popen([self.exe, "-c", self.conf],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_until(lambda: port_open(self.host, self.port) or self.proc.poll() is not None, 5):
            raise RuntimeError("mosquitto 시작 시간 초과")
        if self.proc.poll() is not None:
            raise RuntimeError(f"mosquitto가 바로 종료됨 (코드 {self.proc.returncode}, 설정 {self.conf})")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        wait_until(lambda: not port_open(self.host, self.port), 5)


def _write_conf(path, port, extra=""):
    path.write_text(f"listener {port} {BROKER_HOST}\n{extra}", encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def broker():
    """환경 확인용: mosquitto/local-test.conf 그대로 (익명, 18830 포트). (host, port)를 돌려준다."""
    if find_mosquitto() is None:
        pytest.skip("mosquitto 실행 파일을 찾을 수 없습니다")
    b = Broker(BROKER_CONF, BROKER_PORT)
    b.start()
    yield b.host, b.port
    b.stop()


@pytest.fixture
def anon_broker(tmp_path):
    """테스트마다 새로 띄우는 익명 브로커 (껐다 켤 수 있음)."""
    if find_mosquitto() is None:
        pytest.skip("mosquitto 실행 파일을 찾을 수 없습니다")
    port = free_port()
    b = Broker(_write_conf(tmp_path / "anon.conf", port,
                           "allow_anonymous true\npersistence false\n"), port)
    b.start()
    yield b
    b.stop()


@pytest.fixture
def secure_broker(tmp_path):
    """파이용 ACL 파일(mosquitto/pi/acl)을 그대로 쓰는 인증 브로커. 비밀번호는 'pw-<사용자>'."""
    exe, passwd_exe = find_mosquitto(), find_mosquitto("mosquitto_passwd")
    if exe is None or passwd_exe is None:
        pytest.skip("mosquitto / mosquitto_passwd 실행 파일을 찾을 수 없습니다")
    passwd = tmp_path / "passwd"
    for i, user in enumerate(SECURE_USERS):
        args = [passwd_exe, "-b"] + (["-c"] if i == 0 else []) + [str(passwd), user, f"pw-{user}"]
        subprocess.run(args, check=True, capture_output=True)
    port = free_port()
    conf = _write_conf(tmp_path / "secure.conf", port,
                       "per_listener_settings false\nallow_anonymous false\npersistence false\n"
                       f"password_file {passwd.as_posix()}\nacl_file {ACL_FILE.as_posix()}\n")
    b = Broker(conf, port)
    b.start()
    yield b
    b.stop()


# ---------------------------------------------------------------- 메시지 수집기
class Collector:
    """구독해서 받은 메시지를 모아 둔다. 끊기면 자동 재연결 후 다시 구독한다."""

    def __init__(self, host, port, topic="warehouse/#", username=None, password=None):
        self.topic = topic
        self.received = []          # (topic, payload, retain)
        self.lock = threading.Lock()
        self.subscribed = threading.Event()
        self.connect_result = None  # (ok, reason)
        c = self.client = make_client(f"collector-{uuid.uuid4().hex[:8]}")
        if username:
            c.username_pw_set(username, password)

        def on_connect(ok, reason):
            self.connect_result = (ok, reason)
            if ok:
                c.subscribe(topic, qos=1)

        set_callbacks(c, on_connect=on_connect, on_message=self._on_message)
        c.on_subscribe = lambda *a: self.subscribed.set()  # 인자 형식이 버전마다 달라 *a로 받음
        c.reconnect_delay_set(min_delay=1, max_delay=2)
        c.connect_async(host, port)
        c.loop_start()

    def _on_message(self, msg):
        with self.lock:
            self.received.append((msg.topic, bytes(msg.payload), bool(msg.retain)))

    def wait_ready(self, timeout=5):
        assert self.subscribed.wait(timeout), f"구독 실패: {self.connect_result}"
        return self

    def scans(self):
        with self.lock:
            items = list(self.received)
        return [messages.parse_scan(p) for t, p, _ in items if t.endswith("/scan")]

    def scan_ids(self):
        return {m["id"] for m in self.scans()}

    def statuses(self):
        with self.lock:
            items = list(self.received)
        return [messages.parse_status(p) for t, p, _ in items if t.endswith("/status")]

    def raw(self):
        with self.lock:
            return list(self.received)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def collectors():
    made = []

    def factory(*args, **kwargs):
        c = Collector(*args, **kwargs)
        made.append(c)
        return c

    yield factory
    for c in made:
        c.close()


# ---------------------------------------------------------------- 네트워크 단절 흉내
class TcpProxy:
    """
    클라이언트 ↔ 브로커 사이의 TCP 중계기. cut()을 부르면 현재 연결을 모두 끊고,
    restore() 전까지 새 연결도 받자마자 끊는다 (스캐너 쪽 네트워크 단절 흉내).
    브로커는 그대로 살아 있으므로 수집기의 구독은 유지된다.
    """

    def __init__(self, target_host, target_port):
        self.target = (target_host, target_port)
        self.sock = socket.socket()
        self.sock.bind((BROKER_HOST, 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._alive = True
        self._blocked = threading.Event()
        self._conns = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while self._alive:
            try:
                down, _ = self.sock.accept()
            except OSError:
                return
            if self._blocked.is_set():
                down.close()
                continue
            try:
                up = socket.create_connection(self.target, timeout=2)
                up.settimeout(None)
            except OSError:
                down.close()
                continue
            with self._lock:
                self._conns.append((down, up))
            for a, b in ((down, up), (up, down)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass

    def cut(self):
        self._blocked.set()
        with self._lock:
            conns, self._conns = self._conns, []
        for pair in conns:
            for s in pair:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()

    def restore(self):
        self._blocked.clear()

    def close(self):
        self._alive = False
        self.cut()
        self.sock.close()


@pytest.fixture
def make_proxy():
    made = []

    def factory(host, port):
        p = TcpProxy(host, port)
        made.append(p)
        return p

    yield factory
    for p in made:
        p.close()
