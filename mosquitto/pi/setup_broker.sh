#!/usr/bin/env bash
# 라즈베리파이에 Mosquitto 브로커를 설치·설정한다.
#
#   sudo ./setup_broker.sh dock-01 db-writer monitor
#
# 인자로 준 사용자마다 비밀번호를 입력받아 /etc/mosquitto/passwd 에 등록한다.
# (비밀번호는 명령줄에 쓰지 않는다 — 셸 기록과 ps에 남기 때문)
# 이미 있는 사용자는 비밀번호만 바뀐다. 다시 실행해도 안전하다.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "sudo로 실행하세요: sudo $0 <사용자> [<사용자> ...]" >&2
    exit 1
fi
if [[ $# -lt 1 ]]; then
    echo "사용법: sudo $0 <사용자> [<사용자> ...]   예) sudo $0 dock-01 db-writer monitor" >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v mosquitto >/dev/null; then
    apt-get update
    apt-get install -y mosquitto mosquitto-clients
fi

install -m 644 "$HERE/barcode.conf" /etc/mosquitto/conf.d/barcode.conf
install -m 640 -o root -g mosquitto "$HERE/acl" /etc/mosquitto/acl

PASSWD=/etc/mosquitto/passwd
if [[ ! -f $PASSWD ]]; then
    install -m 640 -o root -g mosquitto /dev/null "$PASSWD"
fi
for user in "$@"; do
    echo "== 사용자 '$user' 비밀번호 입력"
    mosquitto_passwd "$PASSWD" "$user"
done
# mosquitto 2.x는 비밀번호 파일을 mosquitto 그룹이 읽을 수 있어야 하고, 다른 사용자가 읽을 수 있으면 경고한다
chown root:mosquitto "$PASSWD"
chmod 640 "$PASSWD"

systemctl enable mosquitto
systemctl restart mosquitto
sleep 1
systemctl --no-pager --lines=5 status mosquitto
echo
echo "확인: mosquitto_sub -h localhost -u monitor -P '<비밀번호>' -t 'warehouse/#' -v"
