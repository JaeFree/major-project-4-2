"""
MQTT 구독 → DB 저장 서비스 (2단계)

  python -m barcode_system.db_writer_app --config config/db_writer.ini

설정 우선순위: 명령줄 옵션 > --config INI 파일 > 기본값
비밀번호: [mqtt] password_file 의 첫 줄, 없으면 환경 변수 BARCODE_DB_MQTT_PASSWORD
한 브로커에 같은 client_id로 두 개를 실행하면 서로 접속을 빼앗으므로 한 개만 실행한다.
"""
import logging
import sys
import threading

from .config import build_parser, read_password, resolve, setup_logging
from .scan_loop import install_stop_signals
from .spool import Spool
from .store import ScanStore
from .subscriber import DbWriter

log = logging.getLogger("barcode.db")

PASSWORD_ENV = "BARCODE_DB_MQTT_PASSWORD"

OPTIONS = [
    ("mqtt", "host", "--host", str, "127.0.0.1", None, "브로커 주소"),
    ("mqtt", "port", "--port", int, 1883, None, None),
    ("mqtt", "username", "--username", str, "db-writer", None, None),
    ("mqtt", "password_file", "--password-file", str, None, None, "비밀번호 파일 (첫 줄)"),
    ("mqtt", "client_id", "--client-id", str, "db-writer", None, "영속 세션 이름 (바꾸면 보관 메시지를 못 받음)"),
    ("mqtt", "keepalive", "--keepalive", int, 30, None, "초"),
    ("db", "path", "--db", str, "data/barcodes.db", None, "SQLite DB 파일"),
    ("db", "spool", "--spool", str, "data/db_spool.jsonl", None, "DB 기록 실패 시 보조 파일"),
]


def load_config(argv=None):
    ap = build_parser("MQTT subscriber → SQLite (db-writer)", OPTIONS)
    cfg = resolve(ap, ap.parse_args(argv), OPTIONS)
    if not 0 < cfg.mqtt_port < 65536:
        ap.error("port가 잘못됐습니다.")
    if not cfg.mqtt_client_id:
        ap.error("client_id가 필요합니다.")
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

    store = writer = None
    try:
        store = ScanStore(cfg.db_path)
        writer = DbWriter(store, Spool(cfg.db_spool), cfg.mqtt_host, cfg.mqtt_port,
                          cfg.mqtt_username, cfg.mqtt_password, client_id=cfg.mqtt_client_id,
                          keepalive=cfg.mqtt_keepalive)
        writer.start()
        log.info("DB 저장 시작 (DB: %s). 종료: Ctrl+C", cfg.db_path)
        # 메인 스레드는 종료 신호만 기다린다 (Ctrl+C를 받을 수 있도록 짧게 나눠 대기)
        while not stop_event.wait(0.5):
            pass
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        log.error("실행 실패: %s", e)
        return 1
    finally:
        if writer is not None:
            writer.stop()
        if store is not None:
            store.close()
        log.info("종료")


if __name__ == "__main__":
    sys.exit(main())
