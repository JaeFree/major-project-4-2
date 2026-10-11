"""개발 환경 확인용 테스트: 필수 패키지, 바코드 디코더, 로컬 브로커 왕복."""
import queue
import uuid

import cv2
import numpy as np
import pytest

from barcode_images import camera_like, ean13_image


# ---------------------------------------------------------------- 테스트
def test_packages_import():
    import docx  # noqa: F401
    import paho.mqtt  # noqa: F401
    import serial  # noqa: F401
    assert np.__version__.startswith("1."), "파이 Bookworm과 맞추기 위해 numpy<2"


def test_opencv_barcode_decodes_ean13():
    assert hasattr(cv2, "barcode"), "cv2.barcode 없음 (OpenCV 4.8+ 필요)"
    expected, img = ean13_image("880123456789")
    img = camera_like(img)  # 경계가 완전히 날카로운 합성 이미지는 OpenCV가 놓친다
    ok, infos, types, _ = cv2.barcode.BarcodeDetector().detectAndDecodeWithType(img)
    assert ok and expected in infos


def test_pyzbar_decodes_ean13():
    # Windows에서 VC++ 2013 런타임이 없으면 여기서 ImportError/OSError가 난다
    from pyzbar import pyzbar
    expected, img = ean13_image("880123456789")
    found = [(o.data.decode(), o.type) for o in pyzbar.decode(img)]
    assert (expected, "EAN13") in found


@pytest.mark.timeout(15)
def test_local_broker_roundtrip(broker):
    import paho.mqtt.client as mqtt
    host, port = broker
    topic = f"test/env/{uuid.uuid4().hex}"
    got = queue.Queue()

    def make_client():
        # paho-mqtt 2.x는 CallbackAPIVersion을 요구, 1.x에는 없다
        if hasattr(mqtt, "CallbackAPIVersion"):
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        return mqtt.Client()

    sub = make_client()
    sub.on_message = lambda c, u, m: got.put(m.payload)
    sub.connect(host, port)
    sub.subscribe(topic, qos=1)
    sub.loop_start()
    pub = make_client()
    pub.connect(host, port)
    pub.loop_start()
    try:
        # 구독이 등록될 시간을 두고, 메시지가 올 때까지 몇 번 재발행
        for _ in range(20):
            pub.publish(topic, b"hello", qos=1).wait_for_publish(2)
            try:
                assert got.get(timeout=0.5) == b"hello"
                return
            except queue.Empty:
                continue
        pytest.fail("브로커를 통해 메시지를 받지 못했습니다")
    finally:
        for c in (pub, sub):
            c.loop_stop()
            c.disconnect()
