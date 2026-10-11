"""테스트용 합성 바코드 이미지.

EAN-13은 직접 그리고(규격 그대로의 막대), 그 밖의 종류는 python-barcode로 만든다.
OpenCV 검출기는 밝기 기울기로 위치를 찾아서 경계가 완전히 날카로운 합성 이미지를 놓치므로,
camera_like()로 여백·약한 흐림을 주어 실제 카메라 영상에 가깝게 만든다.
"""
import cv2
import numpy as np

_L = ["0001101", "0011001", "0010011", "0111101", "0100011",
      "0110001", "0101111", "0111011", "0110111", "0001011"]
_G = ["".join("1" if b == "0" else "0" for b in code)[::-1] for code in _L]
_R = ["".join("1" if b == "0" else "0" for b in code) for code in _L]
_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
           "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def ean13_check(code12):
    digits = [int(c) for c in code12]
    return (10 - sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits)) % 10) % 10


def ean13_image(code12, module=3, height=120, quiet=12):
    """체크 숫자를 붙인 13자리 값과 흑백 이미지(흰 바탕, 검은 막대)를 돌려준다."""
    digits = [int(c) for c in code12] + [ean13_check(code12)]
    first, left, right = digits[0], digits[1:7], digits[7:]
    bits = "101"
    bits += "".join((_L if p == "L" else _G)[d] for p, d in zip(_PARITY[first], left))
    bits += "01010"
    bits += "".join(_R[d] for d in right)
    bits += "101"
    row = np.array([0 if b == "1" else 255 for b in bits], dtype=np.uint8)
    row = np.pad(np.repeat(row, module), quiet * module, constant_values=255)
    img = np.tile(row, (height, 1))
    img = np.pad(img, ((20, 20), (0, 0)), constant_values=255)
    return "".join(map(str, digits)), img


def symbol_image(kind, data):
    """python-barcode로 만든 흑백 이미지. kind: 'code128', 'code39' 등."""
    import barcode
    from barcode.writer import ImageWriter
    cls = barcode.get_barcode_class(kind)
    kwargs = {"add_checksum": False} if kind == "code39" else {}
    img = cls(data, writer=ImageWriter(), **kwargs).render(
        {"module_width": 0.3, "module_height": 12.0, "quiet_zone": 4.0, "dpi": 200,
         "write_text": False})
    return np.array(img.convert("L"))


def camera_like(img, pad=100, blur=3):
    """여백을 두르고 약하게 흐리게 한다."""
    img = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    return cv2.GaussianBlur(img, (blur, blur), 0) if blur else img
