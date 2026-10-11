#!/usr/bin/env python3
"""
라즈베리파이4 1D 바코드 스캐너 — 단독 실행판 (MQTT 없이 CSV + SQLite에 직접 저장)

  카메라 : Pi 카메라 모듈(Picamera2), USB 웹캠(Linux V4L2 / Windows DirectShow), 이미지 폴더
  인식   : OpenCV BarcodeDetector(EAN/UPC 계열) + pyzbar(ZBar: Code128, Code39, I25 등)
  저장   : CSV + SQLite (컬럼: timestamp, type, data)

  카메라·인식·중복 판단·메인 루프는 barcode_system 패키지와 공유한다.
  MQTT로 발행하는 1단계 스캐너는 python -m barcode_system.scanner_app 을 사용한다.

  중복 저장 방지 (--mode)
    interval : 계속 비추고 있으면 --cooldown 초마다 다시 저장 (기본값)
    presence : 화면에 나타날 때마다 1회만 저장. max(--cooldown, 0.5) 초 이상 사라졌다가
               다시 나타나야 새로 저장

사용 예
  python3 barcode_scanner.py --camera usb --show
  python3 barcode_scanner.py --camera picam --mode presence
  python3 barcode_scanner.py --camera picam --engine zbar --confirm 1

설치는 requirements-pi.txt 참조
"""
import argparse
import csv
import logging
import os
import sqlite3
import sys
import threading

from barcode_system.camera import BACKEND_CHOICES, CAMERA_KINDS, open_camera
from barcode_system.decoding import ENGINE_CHOICES, BarcodeReader
from barcode_system.messages import now_iso
from barcode_system.scan_loop import ScanLoop, display_available, install_stop_signals
from barcode_system.tracker import ScanTracker

log = logging.getLogger("barcode")


# ================================================================ 저장소
class Storage:
    """
    CSV와 SQLite에 함께 기록한다. 한쪽이 실패해도 다른 쪽 기록과 스캔은 계속된다.
    컬럼 구성(timestamp, type, data)은 이전에 만든 파일과 호환되도록 그대로 유지한다.
    """

    def __init__(self, csv_path, db_path):
        for path in (csv_path, db_path):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        # 파일이 없거나 비어 있을 때만 헤더를 쓴다 (빈 파일이 이미 있으면 헤더가 빠지던 문제 방지)
        need_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        self.csv_file = open(csv_path, "a", newline="", encoding="utf-8")
        self.writer = csv.writer(self.csv_file)
        if need_header:
            self.writer.writerow(["timestamp", "type", "data"])
            self.csv_file.flush()

        # timeout: 다른 프로그램이 DB를 쓰고 있으면 최대 5초 기다린다
        self.db = sqlite3.connect(db_path, timeout=5.0)
        # WAL 모드: 다른 프로그램이 DB를 읽는 중에도 쓰기가 막히지 않는다
        # (실행 중에는 barcodes.db-wal, barcodes.db-shm 파일이 함께 생긴다)
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS scans (
                                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                                   timestamp TEXT NOT NULL,
                                   type TEXT,
                                   data TEXT NOT NULL)""")

    def save(self, btype, data):
        """한 곳 이상 저장에 성공하면 타임스탬프 문자열, 모두 실패하면 None을 반환한다."""
        # 밀리초와 시간대(+09:00 등)까지 기록해 같은 초에 들어온 스캔도 구분한다.
        # ※ 라즈베리파이4는 RTC가 없어 네트워크(NTP) 시각 동기화 전에는 시각이 틀릴 수 있다.
        ts = now_iso()
        ok = False
        try:
            self.writer.writerow([ts, btype, data])
            self.csv_file.flush()
            os.fsync(self.csv_file.fileno())  # 갑자기 전원이 꺼져도 기록이 남도록 디스크에 즉시 반영
            ok = True
        except (OSError, csv.Error) as e:
            log.error("CSV 저장 실패 [%s] %s: %s", btype, data, e)
        try:
            with self.db:  # 성공하면 commit, 오류가 나면 rollback
                self.db.execute("INSERT INTO scans (timestamp, type, data) VALUES (?, ?, ?)",
                                (ts, btype, data))
            ok = True
        except sqlite3.Error as e:  # database is locked, 디스크 가득 참 등
            log.error("DB 저장 실패 [%s] %s: %s", btype, data, e)
        return ts if ok else None

    def close(self):
        for closer in (self.csv_file.close, self.db.close):
            try:
                closer()
            except Exception:
                pass


# ================================================================ 메인
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Raspberry Pi 1D barcode scanner (standalone)")
    ap.add_argument("--camera", choices=CAMERA_KINDS, default="usb",
                    help="files는 이미지 폴더(시연·테스트용, --images 필요)")
    ap.add_argument("--index", type=int, default=0, help="USB 카메라 번호")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--backend", choices=BACKEND_CHOICES, default="auto",
                    help="USB 카메라 백엔드 (auto: Linux=v4l2, Windows=dshow)")
    ap.add_argument("--images", help="--camera files일 때 이미지 폴더")
    ap.add_argument("--engine", choices=ENGINE_CHOICES, default="auto")
    ap.add_argument("--mode", choices=["interval", "presence"], default="interval",
                    help="interval: 계속 보이면 cooldown마다 재저장 / presence: 나타날 때마다 1회 저장")
    ap.add_argument("--cooldown", type=float, default=3.0,
                    help="interval: 재저장 간격(초) / presence: 이 시간 이상 사라져야 새 등장으로 봄")
    ap.add_argument("--confirm", type=int, default=2,
                    help="저장 전에 필요한 연속 인식 횟수 (1이면 한 번만 읽혀도 바로 저장)")
    ap.add_argument("--csv", default="barcodes.csv")
    ap.add_argument("--db", default="barcodes.db")
    ap.add_argument("--show", action="store_true", help="미리보기 창 표시 (모니터 연결 시)")
    ap.add_argument("--debug", action="store_true", help="상세 로그 출력")
    args = ap.parse_args(argv)
    if args.cooldown < 0:
        ap.error("--cooldown은 0 이상이어야 합니다.")
    if args.confirm < 1:
        ap.error("--confirm은 1 이상이어야 합니다.")
    if args.width <= 0 or args.height <= 0:
        ap.error("--width, --height는 양수여야 합니다.")
    if args.camera == "files" and not args.images:
        ap.error("--camera files에는 --images가 필요합니다.")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    stop_event = threading.Event()
    install_stop_signals(stop_event)  # SIGTERM / Windows Ctrl+Break → 정상 종료

    show = args.show
    if show and not display_available():
        log.warning("디스플레이가 없어 --show를 무시합니다 (SSH 등).")
        show = False

    cam = store = None
    started = False
    try:
        reader = BarcodeReader(args.engine)
        tracker = ScanTracker(args.cooldown, args.mode, args.confirm)
        store = Storage(args.csv, args.db)
        cam = open_camera(args.camera, args.index, args.width, args.height, args.backend, args.images)
        started = True
        log.info("스캔 시작 (mode=%s, cooldown=%.1f초, confirm=%d). 종료: Ctrl+C%s",
                 args.mode, args.cooldown, args.confirm, " 또는 창에서 q" if show else "")
        ScanLoop(cam, reader, tracker, store.save, show=show, stop_event=stop_event).run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        if started:
            log.exception("예상치 못한 오류로 종료합니다.")
        else:
            log.error("초기화 실패: %s", e)
        return 1
    finally:
        if cam is not None:
            cam.release()
        if store is not None:
            store.close()
        log.info("종료")


if __name__ == "__main__":
    sys.exit(main())
