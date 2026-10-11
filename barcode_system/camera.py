"""
카메라 래퍼: Picamera2, USB 웹캠, 이미지 폴더(시연·테스트용)를 같은 방식(read / release / reopen)으로 다룬다.

USB 웹캠 백엔드 (--backend)
  auto  : Linux → V4L2, Windows → DirectShow, 그 외 → OpenCV 자동 선택
  v4l2 / dshow / msmf / any : 직접 지정
  Windows 기본값을 MSMF가 아닌 DirectShow로 둔 이유: MSMF는 일부 웹캠에서 장치를 여는 데
  수 초~수십 초가 걸리고 해상도 지정이 무시되는 경우가 있다.
"""
import logging
import sys
import time
from pathlib import Path

import cv2

log = logging.getLogger("barcode")

_BACKENDS = {"v4l2": "CAP_V4L2", "dshow": "CAP_DSHOW", "msmf": "CAP_MSMF", "any": "CAP_ANY"}
BACKEND_CHOICES = ("auto",) + tuple(_BACKENDS)
CAMERA_KINDS = ("usb", "picam", "files")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp")


def resolve_backend(name="auto", platform=None):
    """백엔드 이름 → (이름, cv2.CAP_* 값). auto는 실행 중인 OS에 맞춰 고른다."""
    platform = platform or sys.platform
    if name == "auto":
        if platform.startswith("linux"):
            name = "v4l2"
        elif platform == "win32":
            name = "dshow"
        else:
            name = "any"
    if name not in _BACKENDS:
        raise ValueError(f"알 수 없는 카메라 백엔드: {name}")
    return name, getattr(cv2, _BACKENDS[name])


class Camera:
    """Picamera2와 USB 웹캠을 같은 방식(read / release / reopen)으로 다루기 위한 래퍼."""

    def __init__(self, kind="usb", index=0, width=1280, height=720, backend="auto"):
        self.kind = kind
        self.index = index
        self.size = (width, height)
        self.backend = backend
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
            name, api = resolve_backend(self.backend)
            cam = cv2.VideoCapture(self.index, api)
            if not cam.isOpened():
                cam.release()
                raise RuntimeError(f"USB 카메라 {self.index}번을 열 수 없습니다 (백엔드: {name}).")
            # MJPG: 대부분의 웹캠은 고해상도에서 YUYV보다 FPS가 높다 (지원하지 않으면 무시됨)
            cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            # 버퍼 1장: 디코딩이 느려도 밀려 있는 옛 프레임이 아니라 최신 프레임을 받도록 한다
            cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            log.info("USB 카메라 백엔드 %s, 실제 해상도: %dx%d", name,
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


class ImageFolderCamera:
    """
    폴더의 이미지 파일을 차례로 반복해서 돌려주는 가짜 카메라 (웹캠 없이 시연·테스트할 때).
    fps로 프레임 간격을 흉내 낸다. 0이면 기다리지 않는다.
    """

    def __init__(self, folder, fps=10.0):
        self.paths = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        if not self.paths:
            raise RuntimeError(f"이미지가 없습니다: {folder}")
        # 매 프레임 디스크에서 읽지 않도록 미리 불러 둔다
        self.frames = [cv2.imread(str(p), cv2.IMREAD_UNCHANGED) for p in self.paths]
        self.interval = 1.0 / fps if fps > 0 else 0.0
        self._i = 0
        log.info("이미지 폴더 카메라: %s (%d장)", folder, len(self.frames))

    def read(self):
        if self.interval:
            time.sleep(self.interval)
        frame = self.frames[self._i % len(self.frames)]
        self._i += 1
        return frame

    def release(self):
        pass

    def reopen(self):
        pass


def open_camera(kind, index=0, width=1280, height=720, backend="auto", images=None, fps=10.0):
    if kind == "files":
        if not images:
            raise RuntimeError("--camera files에는 --images 폴더가 필요합니다.")
        return ImageFolderCamera(images, fps)
    return Camera(kind, index, width, height, backend)
