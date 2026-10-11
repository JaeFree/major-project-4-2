"""
바코드 스캐너 → MQTT 발행 (1단계)

  python -m barcode_system.scanner_app --config config/scanner.ini
  python -m barcode_system.scanner_app --id dock-01 --host 192.168.0.10 --camera picam

설정 우선순위: 명령줄 옵션 > --config INI 파일 > 기본값
비밀번호는 명령줄에 쓰지 않는다 (ps 명령·셸 기록에 남음).
  [mqtt] password_file 의 첫 줄, 없으면 환경 변수 BARCODE_MQTT_PASSWORD
"""
import logging
import sys
import threading

from . import messages
from .camera import BACKEND_CHOICES, CAMERA_KINDS, open_camera
from .config import bool_value, build_parser, read_password, resolve, setup_logging
from .decoding import ENGINE_CHOICES, BarcodeReader
from .outbox import Outbox
from .publisher import Publisher
from .scan_loop import ScanLoop, display_available, install_stop_signals
from .tracker import ScanTracker

log = logging.getLogger("barcode")

PASSWORD_ENV = "BARCODE_MQTT_PASSWORD"

# (INI 섹션, 키, 명령줄 옵션, 형, 기본값, 선택지, 도움말)
OPTIONS = [
    ("scanner", "id", "--id", str, None, None, "스캐너 ID (토픽 이름, 기본 MQTT 사용자 이름)"),
    ("scanner", "camera", "--camera", str, "usb", CAMERA_KINDS, "files는 이미지 폴더(시연·테스트용)"),
    ("scanner", "index", "--index", int, 0, None, "USB 카메라 번호"),
    ("scanner", "width", "--width", int, 1280, None, None),
    ("scanner", "height", "--height", int, 720, None, None),
    ("scanner", "backend", "--backend", str, "auto", BACKEND_CHOICES, "USB 카메라 백엔드"),
    ("scanner", "images", "--images", str, None, None, "--camera files일 때 이미지 폴더"),
    ("scanner", "fps", "--fps", float, 10.0, None, "--camera files일 때 초당 프레임"),
    ("scanner", "engine", "--engine", str, "auto", ENGINE_CHOICES, None),
    ("scanner", "mode", "--mode", str, "interval", ("interval", "presence"),
     "interval: 계속 보이면 cooldown마다 재발행 / presence: 나타날 때마다 1회"),
    ("scanner", "cooldown", "--cooldown", float, 3.0, None, "초"),
    ("scanner", "confirm", "--confirm", int, 2, None, "발행 전에 필요한 연속 인식 횟수"),
    ("scanner", "show", "--show", bool_value, False, None, "미리보기 창 (true/false)"),
    ("mqtt", "host", "--host", str, "127.0.0.1", None, "브로커 주소"),
    ("mqtt", "port", "--port", int, 1883, None, None),
    ("mqtt", "username", "--username", str, None, None, "비우면 스캐너 ID를 사용"),
    ("mqtt", "password_file", "--password-file", str, None, None, "비밀번호 파일 (첫 줄)"),
    ("mqtt", "keepalive", "--keepalive", int, 30, None, "초"),
    ("outbox", "path", "--outbox", str, "data/outbox.db", None, "outbox SQLite 파일"),
    ("outbox", "retention_days", "--retention-days", int, 7, None, "전송 완료 기록 보관 일수"),
]


def load_config(argv=None):
    ap = build_parser("Barcode scanner → MQTT publisher", OPTIONS)
    cfg = resolve(ap, ap.parse_args(argv), OPTIONS)

    if not cfg.scanner_id:
        ap.error("스캐너 ID가 필요합니다 (--id 또는 [scanner] id)")
    try:
        messages.validate_scanner_id(cfg.scanner_id)
    except ValueError as e:
        ap.error(str(e))
    if cfg.scanner_cooldown < 0:
        ap.error("cooldown은 0 이상이어야 합니다.")
    if cfg.scanner_confirm < 1:
        ap.error("confirm은 1 이상이어야 합니다.")
    if cfg.scanner_width <= 0 or cfg.scanner_height <= 0:
        ap.error("width, height는 양수여야 합니다.")
    if not 0 < cfg.mqtt_port < 65536:
        ap.error("port가 잘못됐습니다.")
    if cfg.scanner_camera == "files" and not cfg.scanner_images:
        ap.error("--camera files에는 --images 폴더가 필요합니다.")

    cfg.mqtt_username = cfg.mqtt_username or cfg.scanner_id
    cfg.mqtt_password = read_password(ap, cfg.mqtt_password_file, PASSWORD_ENV)
    return cfg


def main(argv=None):
    cfg = load_config(argv)
    setup_logging(cfg.debug)
    stop_event = threading.Event()
    install_stop_signals(stop_event)

    show = cfg.scanner_show
    if show and not display_available():
        log.warning("디스플레이가 없어 --show를 무시합니다 (SSH 등).")
        show = False
    if cfg.mqtt_username != cfg.scanner_id:
        # 브로커 ACL은 '사용자 이름 = scanner_id'일 때만 쓰기를 허용한다. MQTT 3.1.1에서는 거부된 발행도
        # PUBACK을 받으므로 스캐너는 전송 성공으로 기록하고, 메시지는 브로커에서 조용히 버려진다.
        log.warning("MQTT 사용자 이름(%s)이 scanner_id(%s)와 다릅니다. 브로커 ACL에서 발행이 "
                    "거부될 수 있습니다 (mosquitto/pi/acl 참조).", cfg.mqtt_username, cfg.scanner_id)
    if cfg.mqtt_password is None:
        log.warning("MQTT 비밀번호가 없습니다 (익명 접속 시도). password_file 또는 %s 설정 필요",
                    PASSWORD_ENV)

    outbox = publisher = cam = None
    started = False
    try:
        reader = BarcodeReader(cfg.scanner_engine)
        tracker = ScanTracker(cfg.scanner_cooldown, cfg.scanner_mode, cfg.scanner_confirm)
        outbox = Outbox(cfg.outbox_path)
        publisher = Publisher(outbox, cfg.scanner_id, cfg.mqtt_host, cfg.mqtt_port,
                              cfg.mqtt_username, cfg.mqtt_password, keepalive=cfg.mqtt_keepalive,
                              retention_days=cfg.outbox_retention_days)
        cam = open_camera(cfg.scanner_camera, cfg.scanner_index, cfg.scanner_width,
                          cfg.scanner_height, cfg.scanner_backend, cfg.scanner_images, cfg.scanner_fps)
        topic = messages.scan_topic(cfg.scanner_id)

        def on_scan(btype, data):
            def make(seq):
                msg = messages.build_scan(cfg.scanner_id, seq, btype, data)
                return msg["id"], topic, messages.encode(msg)
            seq = outbox.add(make)   # 디스크 기록이 끝난 뒤에 발행 → 브로커가 없어도 유실 없음
            publisher.notify()
            log.debug("outbox 기록 seq=%d", seq)
            return True

        publisher.start()
        started = True
        log.info("스캔 시작 (id=%s, mode=%s, cooldown=%.1f초, confirm=%d). 종료: Ctrl+C%s",
                 cfg.scanner_id, cfg.scanner_mode, cfg.scanner_cooldown, cfg.scanner_confirm,
                 " 또는 창에서 q" if show else "")
        ScanLoop(cam, reader, tracker, on_scan, show=show, stop_event=stop_event).run()
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
        if publisher is not None and started:
            publisher.stop(flush_timeout=2.0)  # 남은 메시지는 outbox에 있어 다음 실행 때 전송
        if outbox is not None:
            outbox.close()
        log.info("종료")


if __name__ == "__main__":
    sys.exit(main())
