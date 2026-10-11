"""
가짜 UNO: 실제 펌웨어(arduino/uno_display)와 같은 프로토콜로 응답하는 TCP 서버.
브리지는 pyserial의 'socket://127.0.0.1:<port>' 주소로 COM 포트 대신 여기에 접속한다.

흉내 낼 수 있는 상황
  boot_delay / send_ready=False : 부팅 지연, R을 보내지 않는 보드
  drop_acks(k)                  : 다음 k개 프레임에 확인(A)을 보내지 않음 → 재전송 확인
  reboot()                      : 연결은 유지한 채 다시 R을 보냄 (UNO 리셋)
  unplug() / plug()             : USB 케이블 분리 (연결 끊고 새 연결 거부) / 다시 연결
  send_raw(bytes)               : 잡음·깨진 줄 전송
"""
import socket
import threading
import time

from barcode_system import uno_protocol


class FakeUno:
    def __init__(self, send_ready=True, boot_delay=0.1, ack=True):
        self.send_ready, self.boot_delay, self.ack = send_ready, boot_delay, ack
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.url = f"socket://127.0.0.1:{self.port}"
        self.frames = []          # 받은 정상 프레임 (필드 목록)
        self.bad_lines = []       # 체크섬·형식 오류 줄
        self.connections = 0
        self._drop = 0
        self._refuse = False
        self._conn = None
        self._lock = threading.Lock()
        self._alive = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # ------------------------------------------------------------ 조작
    def drop_acks(self, k):
        with self._lock:
            self._drop = k

    def reboot(self):
        time.sleep(self.boot_delay)
        self._send(uno_protocol.encode("R", uno_protocol.FIRMWARE_NAME, uno_protocol.PROTOCOL_VERSION))

    def unplug(self):
        with self._lock:
            self._refuse = True
            conn, self._conn = self._conn, None
        if conn:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def plug(self):
        with self._lock:
            self._refuse = False

    def send_raw(self, data):
        self._send(data)

    def kinds(self, kind):
        with self._lock:
            return [f for f in self.frames if f[0] == kind]

    def close(self):
        self._alive = False
        self.unplug()
        self.sock.close()

    # ------------------------------------------------------------ 내부
    def _send(self, data):
        with self._lock:
            conn = self._conn
        if conn:
            try:
                conn.sendall(data)
            except OSError:
                pass

    def _accept_loop(self):
        while self._alive:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self._lock:
                if self._refuse:
                    conn.close()
                    continue
                old, self._conn = self._conn, conn
                self.connections += 1
            if old:
                old.close()
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        if self.send_ready:
            self.reboot()          # 포트를 열면 UNO가 리셋되고 R을 보낸다
        buf = b""
        while True:
            try:
                chunk = conn.recv(256)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._on_line(line)

    def _on_line(self, line):
        if len(line.rstrip(b"\r")) > uno_protocol.LINE_MAX:
            self._send(uno_protocol.encode("E", "LEN"))
            return
        try:
            fields = uno_protocol.decode(line)
        except ValueError:
            with self._lock:
                self.bad_lines.append(line)
            self._send(uno_protocol.encode("E", "CK"))
            return
        with self._lock:
            self.frames.append(fields)
            drop = self._drop > 0
            if drop:
                self._drop -= 1
        if self.ack and not drop and len(fields) >= 2:
            self._send(uno_protocol.encode("A", fields[1]))
