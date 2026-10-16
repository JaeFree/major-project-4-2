"""
파이 DB → 외부 재고 대시보드 전송 서비스 (4단계)

  python -m barcode_system.forwarder_app --config config/forwarder.ini
  python -m barcode_system.forwarder_app --config config/forwarder.ini --once     보낼 것을 다 보내고 끝냄
  python -m barcode_system.forwarder_app --config config/forwarder.ini --status   전송 기록 보기

설정 우선순위: 명령줄 옵션 > --config INI 파일 > 기본값
처음 실행할 때 그 시각을 기록해, 그 뒤에 받은 스캔만 보낸다 (send_existing = true면 기존 기록도 전부).
"""
import logging
import sys
import threading

from .config import bool_value, build_parser, resolve, setup_logging
from .dashboard_forwarder import DashboardClient, Forwarder, ForwardState
from .scan_loop import install_stop_signals

log = logging.getLogger("barcode.fwd")

OPTIONS = [
    ("api", "url", "--api-url", str, "https://api.oswdev.cloud", None, "대시보드 API 주소"),
    ("api", "timeout", "--timeout", float, 10.0, None, "요청 제한 시간(초)"),
    ("forward", "types", "--types", str, "CODE128", None, "보낼 바코드 종류 (쉼표로 구분)"),
    ("forward", "device_id", "--device-id", str, None, None, "대시보드에 보낼 deviceId (비우면 스캐너 ID)"),
    ("forward", "send_existing", "--send-existing", bool_value, False, None,
     "처음 실행할 때 기존 기록도 보낼지 (true/false)"),
    ("forward", "poll", "--poll", float, 2.0, None, "새 스캔 확인 간격(초)"),
    ("db", "path", "--db", str, "data/barcodes.db", None, "스캔 DB (읽기 전용)"),
    ("db", "state", "--state", str, "data/forwarder.db", None, "전송 기록 DB"),
]


def load_config(argv=None):
    ap = build_parser("SQLite scans → dashboard API (forwarder)", OPTIONS)
    ap.add_argument("--once", action="store_true", help="보낼 것을 다 보내면(또는 오류가 나면) 종료")
    ap.add_argument("--status", action="store_true", help="전송 기록을 보여주고 종료")
    args = ap.parse_args(argv)
    cfg = resolve(ap, args, OPTIONS)
    cfg.once, cfg.status = args.once, args.status
    cfg.forward_types = tuple(t.strip().upper() for t in cfg.forward_types.split(",") if t.strip())
    if not cfg.forward_types:
        ap.error("types가 비었습니다.")
    if not cfg.api_url.startswith(("http://", "https://")):
        ap.error(f"api url은 http:// 또는 https://로 시작해야 합니다: {cfg.api_url}")
    if cfg.api_timeout <= 0 or cfg.forward_poll <= 0:
        ap.error("timeout과 poll은 0보다 커야 합니다.")
    return cfg


def print_status(state):
    s = state.summary()
    print(f"전송 기준: {state.start_after or '처음부터'} 이후 받은 스캔, 종류 {', '.join(state.types)}")
    print(f"대기 {s['waiting']} / 결과 확인 중 {s['sending']} / 완료 {s['sent']} / "
          f"거부 {s['failed']} / 제외 {s['skipped']}")
    rows = state.recent()
    if not rows:
        return
    print()
    print(f"{'last_try_at':<29}  {'state':<7}  {'barcode':<16}  {'deviceId':<10}  {'timestamp':<24}  "
          f"try  transactionId / error")
    for r in rows:
        print(f"{r['last_try_at'] or '-':<29}  {r['state']:<7}  {r['barcode']:<16}  {r['device_id']:<10}  "
              f"{r['timestamp']:<24}  {r['attempts']:>3}  {r['transaction_id'] or r['error'] or ''}")


def main(argv=None):
    cfg = load_config(argv)
    setup_logging(cfg.debug)
    stop_event = threading.Event()
    install_stop_signals(stop_event)

    state = None
    try:
        state = ForwardState(cfg.db_state, cfg.db_path, cfg.forward_types, cfg.forward_send_existing)
        if cfg.status:
            print_status(state)
            return 0
        if state.first_run:
            log.info("처음 실행: %s", "기존 기록도 모두 보냅니다" if not state.start_after
                     else f"{state.start_after} 이후 받은 스캔부터 보냅니다")
        forwarder = Forwarder(state, DashboardClient(cfg.api_url, cfg.api_timeout),
                              device_id=cfg.forward_device_id, poll=cfg.forward_poll)
        log.info("대시보드 전송 시작 (API: %s, 종류: %s, DB: %s). 종료: Ctrl+C",
                 cfg.api_url, ",".join(cfg.forward_types), cfg.db_path)
        ok = forwarder.run(stop_event, once=cfg.once)
        return 0 if ok else 2
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        log.error("실행 실패: %s", e)
        return 1
    finally:
        if state is not None:
            state.close()
        if not cfg.status:
            log.info("종료")


if __name__ == "__main__":
    sys.exit(main())
