#!/usr/bin/env bash
# systemd 서비스 설치: 부팅 시 자동 실행, 비정상 종료 시 자동 재시작
#
#   sudo deploy/install_services.sh db-writer          DB 저장 서비스만
#   sudo deploy/install_services.sh scanner db-writer uno-bridge  전부
#
# 프로젝트 폴더 소유자를 실행 사용자로 쓴다. 미리 준비할 것:
#   .venv (requirements-pi.txt), config/db_writer.ini, config/scanner.ini, 비밀번호 파일
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "sudo로 실행하세요." >&2
    exit 1
fi
if [[ $# -lt 1 ]]; then
    echo "사용법: sudo $0 <scanner|db-writer|uno-bridge> ..." >&2
    exit 1
fi

DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(stat -c %U "$DIR")"

for name in "$@"; do
    case "$name" in
        scanner)   conf=config/scanner.ini ;;
        db-writer) conf=config/db_writer.ini ;;
        uno-bridge) conf=config/uno_bridge.ini ;;
        *) echo "알 수 없는 서비스: $name" >&2; exit 1 ;;
    esac
    [[ -x "$DIR/.venv/bin/python" ]] || { echo "$DIR/.venv 가 없습니다 (requirements-pi.txt 참조)" >&2; exit 1; }
    [[ -f "$DIR/$conf" ]] || { echo "$DIR/$conf 가 없습니다 (config/*.example.ini 복사)" >&2; exit 1; }

    unit="barcode-$name.service"
    sed -e "s|@USER@|$RUN_USER|g" -e "s|@DIR@|$DIR|g" \
        "$DIR/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
    echo "설치: /etc/systemd/system/$unit (사용자 $RUN_USER, 폴더 $DIR)"
done

systemctl daemon-reload
for name in "$@"; do
    systemctl enable --now "barcode-$name.service"
    systemctl --no-pager --lines=5 status "barcode-$name.service" || true
done
echo
echo "로그 보기: journalctl -u barcode-<scanner|db-writer|uno-bridge> -f"
