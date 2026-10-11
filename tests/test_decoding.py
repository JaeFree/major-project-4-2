import numpy as np
import pytest

from barcode_images import camera_like, ean13_image, symbol_image
from barcode_system.decoding import BarcodeReader, draw, normalize_points, validate_frame


@pytest.fixture(scope="module")
def reader():
    return BarcodeReader("auto")


def test_ean13_both_engines(reader):
    expected, img = ean13_image("880123456789")
    got = reader.decode(camera_like(img))
    assert [(d, t) for d, t, _ in got] == [(expected, "EAN13")]  # 두 엔진 결과가 하나로 합쳐짐


_CODES = [f"{(i * 7919 * 104729) % 10**12:012d}" for i in range(1, 31)]


@pytest.mark.parametrize("code12", ["036000291452", "000000007919"])
def test_upca_reported_once_as_ean13(reader, code12):
    """회귀 테스트: 첫 자리가 0인 EAN-13(=UPC-A)을 OpenCV는 12자리 UPCA, ZBar는 13자리 EAN13으로
    보고해 같은 바코드가 두 건으로 나오던 문제."""
    expected, img = ean13_image(code12)
    got = [(d, t) for d, t, _ in reader.decode(camera_like(img, blur=5))]
    assert got == [(expected, "EAN13")]


def test_upca_from_python_barcode(reader):
    img = symbol_image("upca", "03600029145")   # 11자리 + 체크 숫자
    got = [(d, t) for d, t, _ in reader.decode(camera_like(img))]
    assert got == [("0036000291452", "EAN13")]  # UPC-A 036000291452 → GTIN-13


@pytest.mark.parametrize("module,blur", [(2, 3), (3, 5), (4, 5)])
def test_ean13_recognition_rate(reader, module, blur):
    """
    OpenCV 단독은 합성 이미지에서 값에 따라 놓치는 경우가 있다 (측정: 조건이 맞을 때 약 85~90%).
    ZBar를 함께 쓰는 auto는 모두 읽어야 한다 → 두 엔진을 같이 돌리는 이유.
    """
    cv_only = BarcodeReader("opencv")
    cv_ok = auto_ok = 0
    for code in _CODES:
        expected, img = ean13_image(code, module=module, height=40 * module)
        frame = camera_like(img, blur=blur)
        cv_ok += expected in [d for d, _, _ in cv_only.decode(frame)]
        auto_ok += expected in [d for d, _, _ in reader.decode(frame)]
    assert auto_ok == len(_CODES)
    assert cv_ok >= 0.6 * len(_CODES), f"OpenCV 인식률 {cv_ok}/{len(_CODES)}"


@pytest.mark.parametrize("kind,data,ztype", [
    ("code128", "LOC-A01-03", "CODE128"),
    ("code128", "PALLET0001234", "CODE128"),
    ("code39", "BOX42", "CODE39"),
])
def test_other_symbologies_via_zbar(reader, kind, data, ztype):
    got = reader.decode(camera_like(symbol_image(kind, data)))
    assert (data, ztype) in [(d, t) for d, t, _ in got]


def test_two_barcodes_in_one_frame(reader):
    expected, ean = ean13_image("880123456789")
    code = symbol_image("code128", "LOC-B02")
    w = max(ean.shape[1], code.shape[1])
    pad = lambda a: np.pad(a, ((0, 0), (0, w - a.shape[1])), constant_values=255)
    frame = camera_like(np.vstack([pad(ean), np.full((80, w), 255, np.uint8), pad(code)]))
    found = {d for d, _, _ in reader.decode(frame)}
    assert {expected, "LOC-B02"} <= found


def test_color_and_bgra_frames(reader):
    expected, img = ean13_image("880123456789")
    gray = camera_like(img)
    bgr = np.dstack([gray] * 3)
    bgra = np.dstack([gray] * 3 + [np.full_like(gray, 255)])
    for frame in (bgr, bgra, bgr[..., ::-1]):  # 마지막은 비연속 메모리 뷰
        assert expected in [d for d, _, _ in reader.decode(frame)]


def test_blank_frame_returns_nothing(reader):
    assert reader.decode(np.full((480, 640), 255, np.uint8)) == []


@pytest.mark.parametrize("frame", [None, np.zeros((0, 0), np.uint8), np.zeros((10, 10, 5), np.uint8),
                                   np.zeros((10, 10), dtype=complex), "not an array"])
def test_validate_frame_rejects(frame):
    assert validate_frame(frame) is None


def test_validate_frame_converts():
    f16 = (np.arange(100, dtype=np.uint16).reshape(10, 10) * 600)
    out = validate_frame(f16)
    assert out.dtype == np.uint8 and out.max() == 255
    flt = np.full((4, 4), np.nan, np.float32)
    assert validate_frame(flt).dtype == np.uint8
    one = np.zeros((5, 5, 1), np.uint8)
    assert validate_frame(one).shape == (5, 5)
    ro = np.zeros((5, 5, 3), np.uint8)
    ro.flags.writeable = False
    assert validate_frame(ro).flags.writeable


class _P:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _Rect(tuple):
    def __new__(cls, l, t, w, h):
        obj = super().__new__(cls, (l, t, w, h))
        obj.left, obj.top, obj.width, obj.height = l, t, w, h
        return obj


@pytest.mark.parametrize("pts,n", [
    (np.array([[0, 0], [10, 0], [10, 5], [0, 5]], np.float32), 4),
    (np.zeros((1, 4, 2), np.float32), 4),
    ([_P(1, 2), _P(3, 4)], 2),
    (_Rect(1, 2, 30, 40), 4),
    ([1, 2, 3, 4, 5, 6], 3),
    ([(0, 0), (np.nan, 1), (5, 5)], 2),
])
def test_normalize_points(pts, n):
    out = normalize_points(pts)
    assert out.shape == (n, 1, 2) and out.dtype == np.int32


@pytest.mark.parametrize("pts", [None, [], [1, 2, 3], [(1, 2)], [[1, 2, 3], [4, 5, 6]], "abc"])
def test_normalize_points_rejects(pts):
    assert normalize_points(pts) is None


def test_draw_never_raises():
    frame = np.zeros((100, 100, 3), np.uint8)
    for pts in (None, [(1e12, -1e12), (5, 5)], "x", [_P(1, 1)]):
        draw(frame, pts, "한글-and-a-very-long-label-" * 3, slot=2)
