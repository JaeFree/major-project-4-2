"""
재사용 가능한 스캔 메인 루프: 카메라 → 인식 → 중복 판단 → on_scan 콜백.

저장 방식(CSV/SQLite, MQTT outbox 등)은 on_scan(btype, data) 콜백으로 주입한다.
  on_scan이 참 같은 값을 돌려주면 '저장됨'으로 로그를 남긴다.
  on_scan에서 예외가 나도 스캔은 계속된다 (오류 로그에 값이 남는다).
"""
import logging
import os
import signal
import sys
import threading
import time

import cv2

from .decoding import draw, to_bgr, validate_frame

log = logging.getLogger("barcode")

PRUNE_INTERVAL = 60.0   # 오래된 바코드 이력을 정리하는 주기(초)
READ_FAIL_SLEEP = 0.05  # 프레임 획득 실패 시 대기(초). 실패가 반복돼도 CPU를 100% 점유하지 않도록
MAX_READ_FAILS = 50     # 연속 실패가 이 횟수에 도달하면 카메라 재연결 시도 (약 3초)
RECONNECT_DELAY = 2.0   # 재연결 실패 후 다음 시도까지 대기(초)
WINDOW_NAME = "Barcode Scanner"


def display_available():
    """미리보기 창을 띄울 수 있는지. Linux는 모니터 세션(X11/Wayland)이 있어야 한다.
    SSH처럼 화면이 없는 환경에서 imshow를 호출하면 프로그램이 강제 종료될 수 있어 미리 막는다.
    (기존 코드는 Windows에서도 DISPLAY 변수를 확인해 미리보기가 항상 꺼지는 문제가 있었다)"""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def install_stop_signals(stop_event):
    """SIGTERM(systemd stop 등)과 Windows의 Ctrl+Break(SIGBREAK)를 받으면 stop_event를 세워
    루프를 멈추고 호출부의 finally에서 정리하게 한다. Ctrl+C는 KeyboardInterrupt로 처리된다."""
    handler = lambda signum, frame: stop_event.set()  # noqa: E731
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)


class ScanLoop:
    def __init__(self, camera, reader, tracker, on_scan, show=False, stop_event=None,
                 clock=time.monotonic):
        self.camera = camera
        self.reader = reader
        self.tracker = tracker
        self.on_scan = on_scan
        self.show = show
        self.stop_event = stop_event or threading.Event()
        self.clock = clock  # 간격 계산은 시스템 시계가 아닌 monotonic 시계로
        self._fails = 0
        self._last_prune = clock()
        self._window_opened = False

    def stop(self):
        self.stop_event.set()

    def run(self):
        """stop()이 호출되거나 미리보기 창에서 q를 누를 때까지 반복한다."""
        try:
            while not self.stop_event.is_set():
                if not self.step():
                    break
        finally:
            if self._window_opened:
                try:
                    cv2.destroyAllWindows()
                except cv2.error:
                    pass

    def step(self):
        """프레임 1장 처리. 루프를 끝내야 하면 False."""
        # ---- 1) 프레임 획득 + 형식 검증
        raw = self.camera.read()
        frame = validate_frame(raw)
        if frame is None:
            self._on_read_fail(raw)
            return True
        self._fails = 0

        # ---- 2) 디코딩 + 중복 판단 + 저장
        results = self.process(frame)

        # ---- 3) 미리보기 (선택)
        if self.show:
            return self._preview(frame, results)
        return True

    def process(self, frame):
        """검증된 프레임에서 바코드를 찾아 저장 대상이면 on_scan을 호출한다. 인식 결과를 반환."""
        results = self.reader.decode(frame)
        now = self.clock()
        for data, btype, _ in results:
            if not self.tracker.should_save(data, now):
                continue
            try:
                if self.on_scan(btype, data):
                    log.info("저장 [%s] %s", btype, data)
            except Exception:
                log.exception("저장 처리 중 오류 [%s] %s", btype, data)

        if now - self._last_prune >= PRUNE_INTERVAL:
            self.tracker.prune(now)
            self._last_prune = now
        return results

    def _on_read_fail(self, raw):
        self._fails += 1
        if self._fails == 1 and raw is not None:  # 실패가 시작될 때 한 번만 원인 기록
            log.warning("처리할 수 없는 프레임 형식: shape=%s, dtype=%s",
                        getattr(raw, "shape", None), getattr(raw, "dtype", type(raw).__name__))
        if self._fails >= MAX_READ_FAILS:
            log.warning("정상 프레임을 %d번 연속 받지 못해 카메라를 다시 연결합니다.", self._fails)
            try:
                self.camera.reopen()
                log.info("카메라 재연결 성공")
            except Exception as e:
                log.error("카메라 재연결 실패: %s (%.0f초 후 재시도)", e, RECONNECT_DELAY)
                self.stop_event.wait(RECONNECT_DELAY)
            self._fails = 0
        else:
            self.stop_event.wait(READ_FAIL_SLEEP)

    def _preview(self, frame, results):
        view = to_bgr(frame)
        if view is frame:  # 원본 프레임에 그리지 않도록 (카메라 버퍼를 재사용하는 경우 대비)
            view = frame.copy()
        for slot, (data, btype, pts) in enumerate(results):
            draw(view, pts, f"{btype}: {data}", slot)
        try:
            cv2.imshow(WINDOW_NAME, view)
            self._window_opened = True
            if (cv2.waitKey(1) & 0xFF) == ord("q"):
                return False
        except cv2.error as e:
            log.warning("미리보기 창을 표시할 수 없어 끕니다: %s", e)
            self.show = False
        return True
