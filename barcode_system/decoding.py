"""
1D 바코드 인식: OpenCV BarcodeDetector(EAN/UPC 계열) + pyzbar(ZBar: Code128, Code39, I25 등).
바코드 종류 표기는 ZBar 방식(EAN13, CODE128 등)으로 통일한다.
"""
import logging

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

ENGINE_CHOICES = ("auto", "opencv", "zbar")


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


def canonical(data, btype):
    """
    엔진마다 다른 같은 바코드의 표기를 하나로 맞춘다 → (data, type)
      UPC-A: OpenCV는 12자리 "UPCA", ZBar는 앞에 0을 붙인 13자리 "EAN13"으로 보고한다.
             그대로 두면 auto 모드에서 같은 바코드가 값이 다른 두 건으로 저장된다.
             ZBar 표기(= GTIN-13)로 통일한다.
    """
    if btype == "UPCA" and len(data) == 12 and data.isdigit():
        return "0" + data, "EAN13"
    return data, btype


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
        results = [canonical(data, btype) + (pts,) for data, btype, pts in results]

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
