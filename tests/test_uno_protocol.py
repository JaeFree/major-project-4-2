import pytest

from barcode_system import uno_protocol as up


def test_known_frame():
    assert up.ping(7) == b"P|7*1B\n"          # C++ 테스트(test_protocol.cpp)와 같은 값
    assert up.decode(b"P|7*1B\r\n") == ["P", "7"]


def test_roundtrip_and_sanitize():
    line = up.saved(12, "CODE128", "dock-01", "A|B*C 한글\t")
    fields = up.decode(line)
    assert fields == ["S", "12", "CODE128", "dock-01", "A?B?C ???"]   # '한','글','\t' → '?' 3개
    assert line.endswith(b"\n") and line.isascii()


def test_long_data_is_truncated_to_fit():
    line = up.saved(65535, "CODE128", "dock-01", "X" * 200)
    assert len(line) - 1 == up.LINE_MAX               # 줄바꿈 제외 63바이트
    fields = up.decode(line)
    assert fields[:4] == ["S", "65535", "CODE128", "dock-01"] and set(fields[4]) == {"X"}


def test_rejected_without_scanner():
    assert up.decode(up.rejected(1, None, "bad"))[2] == "?"


@pytest.mark.parametrize("fields", [(), ("SS", 1), ("S",) * 7, ("S", "x" * 70, "y")])
def test_encode_errors(fields):
    with pytest.raises(ValueError):
        up.encode(*fields)


@pytest.mark.parametrize("line", [b"P|7*1C", b"P|7", b"*1B", b"P|7*ZZ", b"PP|7*" + b"00",
                                  "P|7*1B".encode("utf-16"), b"S|1|2|3|4|5|6*00"])
def test_decode_errors(line):
    with pytest.raises(ValueError):
        up.decode(line)
