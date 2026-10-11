import pytest

from barcode_system.spool import Spool


def test_append_and_drain(tmp_path):
    sp = Spool(str(tmp_path / "s" / "spool.jsonl"))
    sp.append("t/1", b"\x00\xffbinary", "r1")
    sp.append("t/2", "한글".encode(), "r2")
    assert len(sp) == 2
    got = []
    assert sp.drain(lambda *a: got.append(a)) == 2
    assert got == [("t/1", b"\x00\xffbinary", "r1"), ("t/2", "한글".encode(), "r2")]
    assert len(sp) == 0 and not (tmp_path / "s" / "spool.jsonl").exists()


def test_drain_stops_at_failure_and_keeps_rest(tmp_path):
    sp = Spool(str(tmp_path / "spool.jsonl"))
    for i in range(4):
        sp.append(f"t/{i}", b"x", "r")
    seen = []

    def handler(topic, payload, received_at):
        if topic == "t/2":
            raise RuntimeError("db locked")
        seen.append(topic)

    with pytest.raises(RuntimeError):
        sp.drain(handler)
    assert seen == ["t/0", "t/1"]
    assert len(sp) == 2                                   # t/2, t/3 남음
    assert sp.drain(lambda t, p, r: seen.append(t)) == 2
    assert seen[-2:] == ["t/2", "t/3"]


def test_corrupt_line_is_skipped(tmp_path, caplog):
    path = tmp_path / "spool.jsonl"
    sp = Spool(str(path))
    sp.append("t/ok", b"x", "r")
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"topic": "t/cut", "payl')               # 쓰다가 전원이 꺼져 잘린 줄
    got = []
    assert sp.drain(lambda t, p, r: got.append(t)) == 2
    assert got == ["t/ok"]
    assert "손상된 줄" in caplog.text
