"""
스캔 기록 DB 관리 도구.

  python -m barcode_system.db_tool --db data/barcodes.db scans --limit 20
  python -m barcode_system.db_tool scanners                 스캐너별 상태·건수
  python -m barcode_system.db_tool gaps                     seq 빈 번호(누락 의심) 구간
  python -m barcode_system.db_tool rejected                 거부된 메시지
  python -m barcode_system.db_tool export out.csv --since 2026-10-01
  python -m barcode_system.db_tool delete --before 2026-10-01            (미리보기: 지울 건수만 표시)
  python -m barcode_system.db_tool delete --before 2026-10-01 --yes --backup data/backup.db

날짜는 'YYYY-MM-DD' 또는 'YYYY-MM-DDTHH:MM[:SS]' (시간대가 없으면 이 컴퓨터의 지역 시각).
기간 조건은 스캔 시각(scanned_at) 기준이며 --since 이상, --until/--before 미만이다.
db-writer가 실행 중이어도 사용할 수 있다 (SQLite WAL).
"""
import argparse
import csv
import os
import sys
from datetime import datetime

from .store import ScanStore


def local(ts):
    """UTC ISO 문자열 → 지역 시각 'YYYY-MM-DD HH:MM:SS.mmm' (사람이 읽는 출력용)."""
    if not ts:
        return "-"
    return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def date_arg(s):
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜 형식이 아닙니다: {s!r} (예: 2026-10-01)")
    return dt if dt.tzinfo else dt.astimezone()


def print_table(rows, cols):
    if not rows:
        print("(없음)")
        return
    text = [[str(r[c]) if r[c] is not None else "-" for c in cols] for r in rows]
    widths = [max(len(c), *(len(t[i]) for t in text)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for t in text:
        print("  ".join(v.ljust(w) for v, w in zip(t, widths)))


def add_filters(p, before=False):
    p.add_argument("--scanner", help="스캐너 ID")
    p.add_argument("--data", help="바코드 값 (정확히 일치)")
    p.add_argument("--since", type=date_arg, help="이 시각 이후 (포함)")
    p.add_argument("--until", type=date_arg, help="이 시각 이전 (미포함)")
    if before:
        p.add_argument("--before", type=date_arg, help="--until과 같음")
        p.add_argument("--id", action="append", dest="ids", help="스캔 id (여러 번 지정 가능)")


def filters(args):
    until = args.until or getattr(args, "before", None)
    return {"scanner_id": args.scanner, "data": args.data, "since": args.since, "until": until,
            "ids": getattr(args, "ids", None)}


def build_parser():
    ap = argparse.ArgumentParser(description="스캔 기록 DB 관리 도구")
    ap.add_argument("--db", default="data/barcodes.db", help="SQLite DB 파일 (기본 data/barcodes.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scans", help="스캔 기록 (최신순)")
    p.add_argument("--limit", type=int, default=20)
    add_filters(p)
    sub.add_parser("scanners", help="스캐너별 상태와 건수")
    p = sub.add_parser("gaps", help="seq 빈 번호 구간")
    p.add_argument("--scanner")
    p = sub.add_parser("rejected", help="거부된 메시지")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("export", help="CSV로 내보내기 (오래된 순, Excel용 UTF-8 BOM)")
    p.add_argument("csv")
    add_filters(p)

    p = sub.add_parser("delete", help="스캔 기록 삭제 (기본은 미리보기)")
    add_filters(p, before=True)
    p.add_argument("--all", action="store_true", help="조건 없이 전체 삭제")
    p.add_argument("--rejected", action="store_true", help="스캔 대신 거부된 메시지 기록을 모두 삭제")
    p.add_argument("--yes", action="store_true", help="확인 없이 실제로 삭제")
    p.add_argument("--backup", help="삭제 전에 DB를 이 파일로 백업")
    p.add_argument("--vacuum", action="store_true", help="삭제 후 파일 크기 줄이기")
    return ap


def cmd_delete(store, args, ap):
    f = filters(args)
    has_filter = any(v for v in f.values())
    if args.rejected:
        if has_filter or args.all:
            ap.error("--rejected는 다른 조건과 함께 쓸 수 없습니다.")
        target, n = "거부된 메시지", len(store.rejected())
    else:
        if not has_filter and not args.all:
            ap.error("삭제 조건이 없습니다. 전체를 지우려면 --all을 지정하세요.")
        if has_filter and args.all:
            ap.error("--all과 다른 조건은 함께 쓸 수 없습니다.")
        target, n = "스캔 기록", store.count(**f)

    if not args.yes:
        print(f"[미리보기] {target} {n}건이 삭제 대상입니다. 실제로 지우려면 --yes를 붙이세요.")
        return 0
    if n == 0:
        print(f"삭제할 {target}이 없습니다.")
        return 0
    if args.backup:
        store.backup(args.backup)
        print(f"백업: {args.backup}")
    deleted = store.delete_rejected() if args.rejected else store.delete_scans(all=args.all, **f)
    print(f"{target} {deleted}건 삭제")
    if args.vacuum:
        store.vacuum()
        print("파일 크기 정리(VACUUM) 완료")
    return 0


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not os.path.isfile(args.db):  # 경로를 잘못 써서 빈 DB가 새로 만들어지는 것을 막는다
        ap.error(f"DB 파일이 없습니다: {args.db}")
    store = ScanStore(args.db)
    try:
        if args.cmd == "scans":
            rows = store.scans(limit=args.limit, **filters(args))
            for r in rows:
                r["scanned_at"] = local(r["scanned_at"])
            print_table(rows, ["scanned_at", "scanner_id", "seq", "type", "data"])
            print(f"\n전체 {store.count(**filters(args))}건 중 {len(rows)}건 표시")
        elif args.cmd == "scanners":
            rows = store.scanners()
            for r in rows:
                r["state_at"], r["last_scan_at"] = local(r["state_at"]), local(r["last_scan_at"])
            print_table(rows, ["scanner_id", "state", "state_at", "last_scan_at", "last_seq", "scan_count"])
        elif args.cmd == "gaps":
            print_table(store.gaps(args.scanner), ["scanner_id", "gap_start", "gap_end"])
        elif args.cmd == "rejected":
            rows = store.rejected(args.limit)
            for r in rows:
                r["received_at"] = local(r["received_at"])
                r["payload"] = bytes(r["payload"])[:60].decode("utf-8", "replace")
            print_table(rows, ["received_at", "topic", "reason", "payload"])
        elif args.cmd == "export":
            rows = store.scans(newest_first=False, **filters(args))
            with open(args.csv, "w", newline="", encoding="utf-8-sig") as fp:
                w = csv.writer(fp)
                w.writerow(["scanned_at", "scanner_id", "seq", "type", "data", "received_at", "id"])
                for r in rows:
                    w.writerow([local(r["scanned_at"]), r["scanner_id"], r["seq"], r["type"], r["data"],
                                local(r["received_at"]), r["id"]])
            print(f"{len(rows)}건을 {args.csv}에 저장")
        elif args.cmd == "delete":
            return cmd_delete(store, args, ap)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
