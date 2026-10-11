"""
MQTT → 아두이노 UNO 시리얼 브리지 (3단계)

  python -m barcode_system.uno_bridge_app --config config/uno_bridge.ini
  python -m barcode_system.uno_bridge_app --serial-port COM5 --host 192.168.0.10   (Windows에서 시험)

설정 우선순위: 명령줄 옵션 > --config INI 파일 > 기본값
비밀번호: [mqtt] password_file 의 첫 줄, 없으면 환경 변수 BARCODE_UNO_MQTT_PASSWORD
"""
import logging
import sys
import threading

from .config import build_parser, read_password, resolve, setup_logging
from .scan_loop import install_stop_signals
from .serial_bridge import UnoBridge, UnoLink

log = logging.getLogger("barcode.uno")

PASSWORD_ENV = "BARCODE_UNO_MQTT_PASSWORD"

OPTIONS = [
    ("mqtt", "host", "--host", str, "127.0.0.1", None, "브로커 주소"),
    ("mqtt", "port", "--port", int, 1883, None, None),
    ("mqtt", "username", "--username", str, "uno-bridge", None, None),
    ("mqtt", "password_file", "--password-file", str, None, None, "비밀번호 파일 (첫 줄)"),
    ("mqtt", "client_id", "--client-id", str, "uno-bridge", None, None),
    ("mqtt", "keepalive", "--keepalive", int, 30, None, "초"),
    ("serial", "port", "--serial-port", str, "auto", None,
     "auto(CH340/CH341/정품 UNO 자동 검색), COM5, /dev/ttyUSB0, socket://host:port(시험용)"),
    ("serial", "baud", "--baud", int, 115200, None, "UNO 스케치의 SERIAL_BAUD와 같아야 함"),
    ("display", "max_age", "--max-age", float, 30.0, None, "이보다 오래된(초) 결과는 표시하지 않음"),
    ("display", "ping_interval", "--ping-interval", float, 5.0, None, "연결 확인 주기(초)"),
]


def load_config(argv=None):
    ap = build_parser("MQTT → Arduino UNO serial bridge", OPTIONS)
    cfg = resolve(ap, ap.parse_args(argv), OPTIONS)
    if not 0 < cfg.mqtt_port < 65536:
        ap.error("port가 잘못됐습니다.")
    if cfg.display_max_age <= 0 or cfg.display_ping_interval <= 0:
        ap.error("max_age, ping_interval은 양수여야 합니다.")
    cfg.mqtt_password = read_password(ap, cfg.mqtt_password_file, PASSWORD_ENV)
    return cfg


def main(argv=None):
    cfg = load_config(argv)
    setup_logging(cfg.debug)
    stop_event = threading.Event()
    install_stop_signals(stop_event)
    if cfg.mqtt_password is None:
        log.warning("MQTT 비밀번호가 없습니다 (익명 접속 시도). password_file 또는 %s 설정 필요",
                    PASSWORD_ENV)
    bridge = None
    try:
        link = UnoLink(cfg.serial_port, cfg.serial_baud)
        bridge = UnoBridge(link, cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_username, cfg.mqtt_password,
                           client_id=cfg.mqtt_client_id, keepalive=cfg.mqtt_keepalive,
                           max_age=cfg.display_max_age, ping_interval=cfg.display_ping_interval)
        bridge.start()
        log.info("UNO 브리지 시작 (포트 %s). 종료: Ctrl+C", cfg.serial_port)
        while not stop_event.wait(0.5):
            pass
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        log.error("실행 실패: %s", e)
        return 1
    finally:
        if bridge is not None:
            bridge.stop()
        log.info("종료")


if __name__ == "__main__":
    sys.exit(main())
