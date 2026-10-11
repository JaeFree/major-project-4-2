#!/usr/bin/env python3
"""
라즈베리파이4 1D 바코드 스캐너 (OpenCV + pyzbar)

  카메라 : Pi 카메라 모듈(Picamera2) 또는 USB 웹캠(cv2.VideoCapture / V4L2)
  인식   : OpenCV BarcodeDetector(EAN/UPC 계열) + pyzbar(ZBar: Code128, Code39, I25 등)
  저장   : CSV + SQLite (컬럼: timestamp, type, data)

  중복 저장 방지 (--mode)
    interval : 계속 비추고 있으면 --cooldown 초마다 다시 저장 (기본값)
    presence : 화면에 나타날 때마다 1회만 저장. --cooldown 초 이상 사라졌다가
               다시 나타나야 새로 저장

사용 예
  python3 barcode_scanner.py --camera usb --show
  python3 barcode_scanner.py --camera picam --mode presence
  python3 barcode_scanner.py --camera picam --engine zbar --confirm 1

설치 (Raspberry Pi OS Bookworm)
  sudo apt install -y python3-opencv python3-picamera2 libzbar0
  pip install pyzbar --break-system-packages
"""
import argparse
import csv
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime

import cv2
import numpy as np

# pyzbar는 선택 사항이다. 설치되지 않았거나 libzbar0가 없으면 OpenCV만 사용한다.
# (libzbar0가 없을 때도 ImportError가 발생한다)
try:
    from pyzbar import pyzbar
    from pyzbar.pyzbar import ZBarSymbol
    ZBAR_IMPORT_ERROR = None
except ImportError as e:
    pyzbar = None
    ZBarSymbol = None
    ZBAR_IMPORT_ERROR = e

log = logging.getLogger("barcode")

# ---------------------------------------------------------------- 동작 상수
CONFIRM_WINDOW = 2.0    # 같은 값의 인식이 이 시간(초) 이상 끊기면 연속 인식 횟수를 처음부터 다시 센다
PRUNE_INTERVAL = 60.0   # 오래된 바코드 이력을 정리하는 주기(초)
READ_FAIL_SLEEP = 0.05  # 프레임 획득 실패 시 대기(초). 실패가 반복돼도 CPU를 100% 점유하지 않도록
MAX_READ_FAILS = 50     # 연속 실패가 이 횟수에 도달하면 카메라 재연결 시도 (약 3초)
RECONNECT_DELAY = 2.0   # 재연결 실패 후 다음 시도까지 대기(초)
WINDOW_NAME = "Barcode Scanner"


# ================================================================ 카메라
class Camera:
    """Picamera2와 USB 웹캠을 같은 방식(read / release / reopen)으로 다루기 위한 래퍼."""

    def __init__(self, kind="usb", index=0, width=1280, height=720):
        self.kind = kind
        self.index = index
        self.size = (width, height)
        self.cam = None
        self._open()

    def _open(self):
        width, height = self.size
        if self.kind == "picam":
            try:
                from picamera2 import Picamera2
            except ImportError as e:
                raise RuntimeError("picamera2가 없습니다: sudo apt install python3-picamera2") from e
            cam = Picamera2()
            try:
                # "RGB888"은 메모리상 B,G,R 순서라서 OpenCV의 BGR 배열로 그대로 쓸 수 있다.
                cfg = cam.create_preview_configuration(
                    main={"size": (width, height), "format": "RGB888"})
                cam.configure(cfg)
                cam.start()
                self._enable_autofocus(cam)
            except Exception:
                cam.close()
                raise
            time.sleep(1.0)  # 자동 노출·화이트밸런스가 안정될 때까지 대기
        else:
            cam = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
            if not cam.isOpened():
                cam.release()
                raise RuntimeError(f"USB 카메라 {self.index}번을 열 수 없습니다.")
            # MJPG: 대부분의 웹캠은 고해상도에서 YUYV보다 FPS가 높다 (지원하지 않으면 무시됨)
            cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            # 버퍼 1장: 디코딩이 느려도 밀려 있는 옛 프레임이 아니라 최신 프레임을 받도록 한다
            cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            log.info("USB 카메라 실제 해상도: %dx%d",
                     cam.get(cv2.CAP_PROP_FRAME_WIDTH), cam.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.cam = cam

    @staticmethod
    def _enable_autofocus(cam):
        """카메라 모듈 3처럼 AF를 지원하는 모델은 연속 AF를 켠다.
        Picamera2에서는 AF가 자동으로 켜지지 않아 가까운 바코드가 흐리게 찍힐 수 있다."""
        try:
            from libcamera import controls
            if "AfMode" in cam.camera_controls:
                cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
                log.info("연속 오토포커스 활성화")
        except Exception as e:  # AF 미지원 모델이거나 라이브러리 버전 차이 → 무시
            log.debug("오토포커스 설정 생략: %s", e)

    def read(self):
        """프레임 1장을 반환한다. 실패하면 예외 대신 None (재시도/재연결은 호출부에서 판단)."""
        if self.cam is None:
            return None
        try:
            if self.kind == "picam":
                return self.cam.capture_array()
            ok, frame = self.cam.read()
            return frame if ok else None
        except Exception as e:
            log.debug("프레임 읽기 오류: %s", e)
            return None

    def release(self):
        cam, self.cam = self.cam, None
        if cam is None:
            return
        try:
            if self.kind == "picam":
                cam.stop()
                cam.close()
            else:
                cam.release()
        except Exception as e:
            log.debug("카메라 해제 중 오류(무시): %s", e)

    def reopen(self):
        """USB 케이블 접촉 불량 등으로 프레임이 끊겼을 때 장치를 다시 연다."""
        self.release()
        time.sleep(1.0)  # 장치가 완전히 해제될 시간
        self._open()


# ================================================================ 프레임 검증 / 변환
def validate_frame(frame):
    """
    카메라 프레임을 검증하고 이후 처리(cvtColor, 디코딩, 그리기)에 안전한 형태로 맞춘다.

    반환: dtype=uint8, C-연속 메모리, 쓰기 가능한 (H, W) 또는 (H, W, 3|4) 배열
          처리할 수 없는 입력이면 None → 호출부에서 해당 프레임을 건너뛴다.
    이미 올바른 형식(일반적인 카메라 프레임)이면 복사 없이 그대로 반환하므로 비용이 거의 없다.
    """
    # 1) numpy 배열이 아니거나(읽기 실패 시 None 등) 비어 있으면 거부
    if not isinstance(frame, np.ndarray) or frame.size == 0:
        return None

    # 2) 채널 구성: (H, W, 1)은 (H, W)로 펴고, 그 외에는 흑백 / BGR / BGRA만 허용
    if frame.ndim == 3 and frame.shape[2] == 1:
        frame = frame[:, :, 0]
    if not (frame.ndim == 2 or (frame.ndim == 3 and frame.shape[2] in (3, 4))):
        return None

    # 3) 자료형: 디코더와 그리기 함수는 8비트 기준이다.
    #    16비트·실수형 등은 NaN/Inf를 0으로 바꾼 뒤 최소~최대 값을 0~255로 늘려 변환한다.
    if frame.dtype != np.uint8:
        if frame.dtype.kind not in "biuf":  # bool / 부호 있는 정수 / 부호 없는 정수 / 실수만 허용
            return None
        f = np.nan_to_num(frame.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        frame = cv2.normalize(f, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

    # 4) 메모리 배치: 채널 순서를 뒤집은 뷰(frame[..., ::-1]), 간격을 두고 잘라낸 배열(frame[:, ::2]),
    #    전치된 배열처럼 메모리 배치가 특이한 배열에 cv2 그리기 함수를 쓰면 오류가 나므로 연속 배열로 복사한다.
    if not frame.flags.c_contiguous:
        frame = np.ascontiguousarray(frame)
    if not frame.flags.writeable:  # 읽기 전용 버퍼 → 외곽선을 그릴 수 있도록 복사
        frame = frame.copy()
    return frame


def to_gray(img):
    """validate_frame() 결과 → 흑백 단일 채널 (디코딩용)"""
    if img.ndim == 2:
        return img
    code = cv2.COLOR_BGR2GRAY if img.shape[2] == 3 else cv2.COLOR_BGRA2GRAY
    return cv2.cvtColor(img, code)


def to_bgr(img):
    """validate_frame() 결과 → 3채널 BGR (미리보기·그리기용)"""
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


# ================================================================ 외곽선 좌표 정규화 / 그리기
_COORD_LIMIT = 1_000_000  # 비정상적으로 큰 좌표로 인한 int32 오버플로 방지용 상한


def normalize_points(pts):
    """
    엔진마다 다른 외곽선 좌표 표현을 cv2.polylines용 (N, 1, 2) int32 배열로 통일한다.

    지원하는 입력
      OpenCV : (4, 2) / (1, 4, 2) / (N, 1, 2) 형태의 float32 배열
      pyzbar : Point(x, y) 목록(polygon), Rect(left, top, width, height)
      기타   : [(x, y), ...] 목록, [x1, y1, x2, y2, ...] 평면 목록
    NaN/Inf 좌표는 버린다. 해석할 수 없거나 유효한 점이 2개 미만이면 None.
    """
    if pts is None:
        return None
    # pyzbar Rect → 네 꼭짓점. Rect도 숫자 4개짜리 튜플이라 그대로 두면 점 2개로 잘못 해석된다.
    if all(hasattr(pts, a) for a in ("left", "top", "width", "height")):
        l, t, w, h = pts.left, pts.top, pts.width, pts.height
        pts = [(l, t), (l + w, t), (l + w, t + h), (l, t + h)]
    # x, y 속성을 가진 점 객체(pyzbar Point 등)의 목록 → (x, y) 튜플 목록
    elif isinstance(pts, (list, tuple)):
        pts = [(p.x, p.y) if hasattr(p, "x") and hasattr(p, "y") else p for p in pts]

    try:
        arr = np.asarray(pts, dtype=np.float64)
    except (TypeError, ValueError):  # 길이가 제각각인 목록, 숫자가 아닌 값 등
        return None
    if arr.size == 0 or arr.size % 2:
        return None
    if arr.ndim > 1 and arr.shape[-1] != 2:  # 마지막 축이 (x, y)가 아니면 해석할 수 없음
        return None

    arr = arr.reshape(-1, 2)
    arr = arr[np.isfinite(arr).all(axis=1)]  # NaN/Inf가 섞인 점 제거
    if len(arr) < 2:
        return None
    arr = np.clip(np.rint(arr), -_COORD_LIMIT, _COORD_LIMIT)  # 버림(astype) 대신 반올림
    return arr.astype(np.int32).reshape(-1, 1, 2)


def draw(frame, pts, text, slot=0):
    """
    인식 결과를 frame(BGR, uint8)에 그린다. 좌표 형식이 무엇이든 예외 없이 동작한다.
    좌표가 없거나 해석할 수 없으면 외곽선 없이 좌상단에 slot 순서대로 글자만 표시한다.
    """
    h, w = frame.shape[:2]
    poly = normalize_points(pts)
    if poly is not None:
        # 점이 3개 이상이면 닫힌 다각형, 2개(ZBar의 1D 스캔선 등)면 선분으로 그린다
        cv2.polylines(frame, [poly], len(poly) >= 3, (0, 255, 0), 2)
        x = int(poly[:, 0, 0].min())
        y = int(poly[:, 0, 1].min()) - 10
    else:
        x, y = 10, 30 + 25 * slot

    # Hershey 폰트는 ASCII만 그릴 수 있으므로 그 밖의 문자는 '?'로 바꾸고, 너무 길면 자른다
    label = text if len(text) <= 40 else text[:37] + "..."
    label = label.encode("ascii", "replace").decode("ascii")
    # 글자가 화면 밖으로 나가지 않도록 위치 보정
    x = min(max(x, 0), max(w - 1, 0))
    y = min(max(y, 20), max(h - 5, 20))
    cv2.putText(frame, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)


# ================================================================ 디코더
# 바코드 종류 표기를 ZBar 방식으로 통일한다 (OpenCV는 "EAN_13", 구버전 contrib은 정수 enum)
_CV_TYPE_NAMES = {
    "EAN_8": "EAN8", "EAN_13": "EAN13", "UPC_A": "UPCA", "UPC_E": "UPCE",
    1: "EAN8", 2: "EAN13", 3: "UPCA", 4: "UPCE", 5: "UPC_EAN_EXTENSION",
}
# ZBar에서 켤 1D 바코드 종류. QR 등 2D를 끄면 불필요한 결과가 없어지고 속도도 빨라진다.
# (설치된 pyzbar 버전에 없는 이름은 자동으로 건너뜀)
_ZBAR_1D = ("EAN8", "EAN13", "UPCE", "I25", "CODABAR", "CODE39", "CODE93", "CODE128",
            "DATABAR", "DATABAR_EXP")


class BarcodeReader:
    """
    engine="auto"   : OpenCV와 ZBar를 매 프레임 모두 실행해 결과를 합친다.
                      한쪽이 찾으면 다른 쪽을 건너뛰는 방식은, 예를 들어 EAN-13과 Code128이
                      같이 보일 때 OpenCV가 EAN-13만 찾으면 Code128을 계속 놓치게 된다.
                      바코드가 없는 프레임에서는 원래도 두 엔진이 모두 실행되므로 추가 부담은 작다.
    engine="opencv" : OpenCV만 / engine="zbar" : ZBar만 (속도가 더 필요할 때)
    """

    def __init__(self, engine="auto"):
        self.cv_detector = None
        self.cv_with_type = False
        self.use_zbar = False
        self.zbar_symbols = None

        if engine in ("auto", "opencv"):
            if hasattr(cv2, "barcode"):
                self.cv_detector = cv2.barcode.BarcodeDetector()
                # OpenCV 4.8+: detectAndDecodeWithType / 구버전 contrib: detectAndDecode (반환값 4개)
                self.cv_with_type = hasattr(self.cv_detector, "detectAndDecodeWithType")
            elif engine == "opencv":
                raise RuntimeError(f"OpenCV {cv2.__version__}에는 cv2.barcode가 없습니다 (4.8 이상 권장).")

        if engine in ("auto", "zbar"):
            if pyzbar is not None:
                self.use_zbar = True
                self.zbar_symbols = [getattr(ZBarSymbol, n) for n in _ZBAR_1D if hasattr(ZBarSymbol, n)]
            elif engine == "zbar":
                raise RuntimeError(f"pyzbar를 불러올 수 없습니다: {ZBAR_IMPORT_ERROR}")

        if self.cv_detector is None and not self.use_zbar:
            raise RuntimeError("사용 가능한 디코더가 없습니다 (OpenCV 4.8+ 또는 pyzbar + libzbar0 필요).")
        log.info("디코더: OpenCV %s(%s), ZBar(%s)", cv2.__version__,
                 "사용" if self.cv_detector is not None else "없음",
                 "사용" if self.use_zbar else "없음")

    def decode(self, image):
        """이미지에서 1D 바코드를 찾아 [(data, type, points), ...]를 반환한다. 같은 data는 한 번만."""
        img = validate_frame(image)  # cvtColor 전에 배열 형식을 검증
        if img is None:
            return []
        gray = to_gray(img)

        results = []
        if self.cv_detector is not None:
            results += self._decode_opencv(gray)
        if self.use_zbar:
            results += self._decode_zbar(gray)

        # 두 엔진이 같은 바코드를 모두 찾았거나 한 엔진이 중복 보고한 경우 첫 번째(OpenCV 우선)만 사용
        unique, seen = [], set()
        for item in results:
            if item[0] not in seen:
                seen.add(item[0])
                unique.append(item)
        return unique

    def _decode_opencv(self, gray):
        try:
            if self.cv_with_type:
                ok, infos, types, points = self.cv_detector.detectAndDecodeWithType(gray)
            else:
                ok, infos, types, points = self.cv_detector.detectAndDecode(gray)
        except cv2.error as e:
            # 특정 프레임에서만 나는 일시적 오류 → 이번 프레임만 건너뜀
            log.debug("OpenCV 디코드 오류: %s", e)
            return []
        except (ValueError, TypeError) as e:
            # 반환 형식이 예상과 다른 OpenCV 빌드 → 매 프레임 예외가 나지 않도록 OpenCV 엔진을 끈다
            log.warning("OpenCV 바코드 API 형식이 예상과 달라 OpenCV 엔진을 끕니다: %s", e)
            self.cv_detector = None
            if not self.use_zbar:
                raise RuntimeError("사용 가능한 디코더가 없습니다.") from e
            return []
        if not ok or infos is None:
            return []

        out = []
        for i, data in enumerate(infos):
            if not data:  # 위치는 검출됐지만 해독에 실패한 영역
                continue
            raw_type = types[i] if types is not None and i < len(types) else None
            pts = points[i] if points is not None and i < len(points) else None
            out.append((str(data), self._cv_type_name(raw_type), pts))
        return out

    @staticmethod
    def _cv_type_name(t):
        if t is None:
            return "UNKNOWN"
        try:
            key = t if isinstance(t, str) else int(t)
        except (TypeError, ValueError):
            return str(t)
        return _CV_TYPE_NAMES.get(key, str(t))

    def _decode_zbar(self, gray):
        try:
            objs = pyzbar.decode(gray, symbols=self.zbar_symbols or None)
        except Exception as e:
            log.debug("ZBar 디코드 오류: %s", e)
            return []

        out = []
        for obj in objs:
            if obj.type in ("QRCODE", "SQCODE", "PDF417"):  # 2D 코드는 제외
                continue
            data = obj.data.decode("utf-8", errors="replace")
            # 1D 바코드의 polygon은 스캔선이 지나간 점들로 만들어져 점이 2개 이하(선분)일 수 있다.
            # 그런 경우에는 사각형(rect)을 외곽선으로 쓴다.
            polygon = getattr(obj, "polygon", None)
            pts = polygon if polygon and len(polygon) >= 3 else getattr(obj, "rect", None)
            out.append((data, obj.type, pts))
        return out


# ================================================================ 중복 저장 판단
class ScanTracker:
    """
    바코드 값별 인식 이력을 보고 '지금 저장할지'를 판단한다.

    ※ 쿨다운은 마지막 '인식' 시각이 아니라 마지막 '저장' 시각을 기준으로 계산한다.
      인식 시각을 기준으로 하면 바코드가 매 프레임 인식될 때마다 쿨다운이 새로 시작되므로,
      계속 비추고 있는 동안에는 쿨다운이 끝나지 않아 다시 저장되지 않는다.

    confirm  : 같은 값이 CONFIRM_WINDOW초 이내 간격으로 confirm번 인식돼야 유효하다
               (한 프레임에서만 튀는 오인식 방지. I25, Codabar처럼 체크섬 없는 코드에서 특히 유용)
    interval : 유효한 인식이 이어지는 동안 cooldown초마다 저장
    presence : 등장 1번당 1회 저장. cooldown초 이상 안 보이다가 다시 보이면 새로운 등장으로 본다
    시간 값은 time.monotonic()을 쓴다. 라즈베리파이4는 RTC가 없어 부팅 후 NTP 동기화 때 시스템
    시계가 크게 바뀔 수 있는데, time.time()으로 간격을 재면 시계가 뒤로 가는 순간 차이가 음수가
    되어 그만큼 저장이 막힌다.
    """

    class _Track:
        __slots__ = ("last_seen", "appeared", "hits", "last_saved")

        def __init__(self, now):
            self.last_seen = now    # 마지막으로 인식된 시각
            self.appeared = now     # 이번 등장이 시작된 시각 (presence 모드)
            self.hits = 0           # 연속 인식 횟수 (confirm 판단)
            self.last_saved = None  # 마지막으로 저장한 시각 (interval 모드)

    def __init__(self, cooldown, mode="interval", confirm=1):
        self.cooldown = cooldown
        self.mode = mode
        self.confirm = confirm
        self._tracks = {}

    def should_save(self, data, now):
        """이번 인식을 저장해야 하면 True. True를 반환할 때 '저장함'으로 기록까지 한다.
        (실제 저장이 실패해도 저장한 것으로 간주해 매 프레임 재시도·오류 로그 폭주를 막는다.
         실패한 값은 오류 로그에 데이터와 함께 남는다.)"""
        t = self._tracks.get(data)
        if t is None:
            t = self._tracks[data] = self._Track(now)
        else:
            gap = now - t.last_seen
            if gap >= CONFIRM_WINDOW:
                t.hits = 0         # 인식이 오래 끊김 → 연속 인식 횟수를 처음부터
            if gap >= self.cooldown:
                t.appeared = now   # cooldown 이상 안 보였음 → 새로 나타난 것으로 간주
            t.last_seen = now
        t.hits += 1

        if t.hits < self.confirm:
            return False           # 아직 확정되지 않음

        if self.mode == "presence":
            # 이번 등장 이후 아직 저장하지 않았을 때만
            ok = t.last_saved is None or t.last_saved < t.appeared
        else:
            # 마지막 '저장' 이후 cooldown이 지났을 때만 → 계속 보이고 있어도 주기적으로 저장된다
            ok = t.last_saved is None or now - t.last_saved >= self.cooldown
        if ok:
            t.last_saved = now
        return ok

    def prune(self, now):
        """판단에 더 이상 영향을 주지 않는 오래된 기록을 지운다 (장시간 운영 시 메모리 증가 방지).
        마지막 인식 후 max(cooldown, CONFIRM_WINDOW)가 지난 기록은 지워도 판단 결과가 같다."""
        limit = max(self.cooldown, CONFIRM_WINDOW)
        for key in [k for k, t in self._tracks.items() if now - t.last_seen >= limit]:
            del self._tracks[key]


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
        ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
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
def display_available():
    """모니터 세션(X11/Wayland)이 있는지 확인한다. SSH처럼 화면이 없는 환경에서
    imshow를 호출하면 프로그램이 강제 종료될 수 있어 미리 막는다."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _on_sigterm(signum, frame):
    # systemd stop 등으로 SIGTERM을 받으면 Ctrl+C와 같은 경로로 종료 → finally에서 정리
    raise KeyboardInterrupt


def parse_args():
    ap = argparse.ArgumentParser(description="Raspberry Pi 1D barcode scanner")
    ap.add_argument("--camera", choices=["usb", "picam"], default="usb")
    ap.add_argument("--index", type=int, default=0, help="USB 카메라 번호")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--engine", choices=["auto", "opencv", "zbar"], default="auto")
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
    args = ap.parse_args()
    if args.cooldown < 0:
        ap.error("--cooldown은 0 이상이어야 합니다.")
    if args.confirm < 1:
        ap.error("--confirm은 1 이상이어야 합니다.")
    if args.width <= 0 or args.height <= 0:
        ap.error("--width, --height는 양수여야 합니다.")
    return args


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    signal.signal(signal.SIGTERM, _on_sigterm)

    show = args.show
    if show and not display_available():
        log.warning("디스플레이가 없어 --show를 무시합니다 (SSH 등).")
        show = False

    cam = store = None
    started = False
    window_opened = False
    try:
        reader = BarcodeReader(args.engine)
        tracker = ScanTracker(args.cooldown, args.mode, args.confirm)
        store = Storage(args.csv, args.db)
        cam = Camera(args.camera, args.index, args.width, args.height)
        started = True
        log.info("스캔 시작 (mode=%s, cooldown=%.1f초, confirm=%d). 종료: Ctrl+C%s",
                 args.mode, args.cooldown, args.confirm, " 또는 창에서 q" if show else "")

        fails = 0
        last_prune = time.monotonic()
        while True:
            # ---- 1) 프레임 획득 + 형식 검증
            raw = cam.read()
            frame = validate_frame(raw)
            if frame is None:
                fails += 1
                if fails == 1 and raw is not None:  # 실패가 시작될 때 한 번만 원인 기록
                    log.warning("처리할 수 없는 프레임 형식: shape=%s, dtype=%s",
                                getattr(raw, "shape", None), getattr(raw, "dtype", type(raw).__name__))
                if fails >= MAX_READ_FAILS:
                    log.warning("정상 프레임을 %d번 연속 받지 못해 카메라를 다시 연결합니다.", fails)
                    try:
                        cam.reopen()
                        log.info("카메라 재연결 성공")
                    except Exception as e:
                        log.error("카메라 재연결 실패: %s (%.0f초 후 재시도)", e, RECONNECT_DELAY)
                        time.sleep(RECONNECT_DELAY)
                    fails = 0
                else:
                    time.sleep(READ_FAIL_SLEEP)
                continue
            fails = 0

            # ---- 2) 디코딩 + 중복 판단 + 저장
            results = reader.decode(frame)
            now = time.monotonic()  # 간격 계산은 시스템 시계가 아닌 monotonic 시계로
            for data, btype, _ in results:
                if tracker.should_save(data, now) and store.save(btype, data):
                    log.info("저장 [%s] %s", btype, data)

            if now - last_prune >= PRUNE_INTERVAL:
                tracker.prune(now)
                last_prune = now

            # ---- 3) 미리보기 (선택)
            if show:
                view = to_bgr(frame)
                for slot, (data, btype, pts) in enumerate(results):
                    draw(view, pts, f"{btype}: {data}", slot)
                try:
                    cv2.imshow(WINDOW_NAME, view)
                    window_opened = True
                    if (cv2.waitKey(1) & 0xFF) == ord("q"):
                        break
                except cv2.error as e:
                    log.warning("미리보기 창을 표시할 수 없어 끕니다: %s", e)
                    show = False
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
        if window_opened:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
        log.info("종료")


if __name__ == "__main__":
    sys.exit(main())
