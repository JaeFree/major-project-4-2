"""공용 pytest 픽스처: 로컬 Mosquitto 브로커를 테스트 동안 실행한다."""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BROKER_CONF = ROOT / "mosquitto" / "local-test.conf"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18830


def find_mosquitto():
    """MOSQUITTO_EXE 환경 변수 → PATH → 기본 설치 경로 순으로 찾는다."""
    for cand in (os.environ.get("MOSQUITTO_EXE"), shutil.which("mosquitto"),
                 r"D:\Tools\mosquitto\mosquitto.exe"):
        if cand and Path(cand).is_file():
            return cand
    return None


def _port_open(host, port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def broker():
    """(host, port)를 돌려준다. mosquitto가 없으면 해당 테스트는 건너뛴다."""
    exe = find_mosquitto()
    if exe is None:
        pytest.skip("mosquitto 실행 파일을 찾을 수 없습니다")
    if _port_open(BROKER_HOST, BROKER_PORT):
        pytest.fail(f"{BROKER_PORT} 포트를 이미 다른 프로세스가 쓰고 있습니다")

    proc = subprocess.Popen([exe, "-c", str(BROKER_CONF)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not _port_open(BROKER_HOST, BROKER_PORT):
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail("로컬 mosquitto를 시작하지 못했습니다")
            time.sleep(0.05)
        yield BROKER_HOST, BROKER_PORT
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
