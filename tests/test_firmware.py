"""
UNO 펌웨어 검증 (하드웨어 없이):
  - arduino/uno_display/protocol.h 를 PC용 g++로 빌드해 C++ 단위 테스트 실행
  - Python(uno_protocol)이 만든 프레임을 C++ 수신 코드(LineReader+parse)에 넣어 양쪽 해석이 일치하는지 대조
  - arduino-cli로 UNO용 컴파일, 메모리 사용량 확인
"""
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from barcode_system import uno_protocol as up
from conftest import ROOT

FW_DIR = ROOT / "tests" / "firmware"
SKETCH = ROOT / "arduino" / "uno_display"
ARDUINO_CONFIG = Path(r"D:\Tools\arduino-data\arduino-cli.yaml")


def find_tool(env, name, default):
    for cand in (os.environ.get(env), shutil.which(name), default):
        if cand and Path(cand).is_file():
            return str(cand)
    return None


GXX = find_tool("CXX", "g++", r"D:\Tools\w64devkit\bin\g++.exe")
ARDUINO_CLI = find_tool("ARDUINO_CLI", "arduino-cli", r"D:\Tools\arduino-cli\arduino-cli.exe")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if GXX is None:
        pytest.skip("g++를 찾을 수 없습니다 (w64devkit)")
    out = tmp_path_factory.mktemp("fw")
    exes = {}
    for name in ("test_protocol", "proto_cli"):
        exe = out / (name + ".exe")
        # UNO의 avr-gcc와 같은 C++11, 경고도 오류로
        r = subprocess.run([GXX, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O1",
                            "-o", str(exe), str(FW_DIR / f"{name}.cpp")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        exes[name] = str(exe)
    return exes


def test_cpp_unit_tests(built):
    r = subprocess.run([built["test_protocol"]], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def _expected(line):
    """C++ proto_cli가 출력해야 할 줄 (Python 구현 기준)."""
    body = line.rstrip(b"\n").replace(b"\r", b"")
    if not body:
        return None                       # 빈 줄은 건너뜀
    if len(body) > up.LINE_MAX:
        return "OVERFLOW"
    try:
        return "OK\t" + "\t".join(up.decode(body))
    except ValueError:
        return "ERR"


def test_python_and_cpp_agree(built):
    rnd = random.Random(42)
    alphabet = "ABCxyz019 -_./:|*\t한글"
    lines = []
    for i in range(300):
        kind = rnd.choice("SXTP")
        fields = {"S": (rnd.choice(["EAN13", "CODE128"]), "dock-01",
                        "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 80)))),
                  "X": ("dock-02", "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 40)))),
                  "T": ("dock-03", rnd.choice(["online", "lost"])),
                  "P": ()}[kind]
        line = up.encode(kind, rnd.randint(1, 65535), *fields)
        r = rnd.random()
        if r < 0.15:                                   # 한 바이트 손상
            pos = rnd.randrange(len(line) - 1)
            line = line[:pos] + bytes([line[pos] ^ 0x01]) + line[pos + 1:]
        elif r < 0.25:                                 # CRLF
            line = line[:-1] + b"\r\n"
        lines.append(line)
    lines += [b"\n", b"x" * 64 + b"\n", b"y" * 300 + b"\n", b"no checksum\n", up.ping(1)]
    stream = b"".join(lines)

    r = subprocess.run([built["proto_cli"]], input=stream, capture_output=True)
    assert r.returncode == 0
    got = r.stdout.decode("ascii").splitlines()
    expected = [e for e in map(_expected, lines) if e is not None]
    # C++는 오류 코드 번호까지 출력하므로 'ERR'까지만 비교
    got = [g.split("\t")[0] if g.startswith("ERR") else g for g in got]
    assert len(got) == len(expected)
    mismatches = [(i, e, g) for i, (e, g) in enumerate(zip(expected, got)) if e != g]
    assert not mismatches, mismatches[:5]
    assert sum(g.startswith("OK") for g in got) > 200


def test_sketch_compiles_for_uno(tmp_path):
    if ARDUINO_CLI is None:
        pytest.skip("arduino-cli를 찾을 수 없습니다")
    env = dict(os.environ)
    if ARDUINO_CONFIG.is_file():
        env["ARDUINO_CONFIG_FILE"] = str(ARDUINO_CONFIG)
    r = subprocess.run([ARDUINO_CLI, "compile", "--profile", "uno", "--warnings", "all",
                        "--build-path", str(tmp_path / "build"), str(SKETCH)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                       timeout=600)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    # 이 프로젝트 파일에서 나온 경고는 없어야 한다 (코어·라이브러리 경고는 제외)
    own = [ln for ln in out.splitlines()
           if re.search(r"uno_display[\\/](uno_display\.ino|protocol\.h|pins\.h):\d+.*warning", ln)]
    assert not own, own
    flash = int(re.search(r"Sketch uses (\d+) bytes", out).group(1))
    sram = int(re.search(r"Global variables use (\d+) bytes", out).group(1))
    print(f"Flash {flash} / 32256 bytes, SRAM {sram} / 2048 bytes")
    assert flash < 32256 * 0.8
    assert sram < 1024, "SRAM 전역 변수는 절반 이하로 (스택·LCD 동적 할당 여유)"
