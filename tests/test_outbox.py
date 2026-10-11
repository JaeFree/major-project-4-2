import threading

import pytest

from barcode_system import messages
from barcode_system.outbox import Outbox


def add_scan(box, data="8801234567893"):
    def make(seq):
        msg = messages.build_scan("dock-01", seq, "EAN13", data)
        return msg["id"], messages.scan_topic("dock-01"), messages.encode(msg)
    return box.add(make)


@pytest.fixture
def box(tmp_path):
    b = Outbox(str(tmp_path / "sub" / "outbox.db"))  # 없는 폴더도 만든다
    yield b
    b.close()


def test_seq_increases_and_is_in_payload(box):
    seqs = [add_scan(box) for _ in range(3)]
    assert seqs == [1, 2, 3]
    rows = box.pending()
    assert [r.seq for r in rows] == seqs
    assert [messages.parse_scan(r.payload)["seq"] for r in rows] == seqs
    assert len({r.id for r in rows}) == 3


def test_pending_limit_exclude_and_mark_sent(box):
    for _ in range(5):
        add_scan(box)
    assert [r.seq for r in box.pending(limit=2)] == [1, 2]
    assert [r.seq for r in box.pending(limit=2, exclude={1, 2})] == [3, 4]
    box.mark_sent(1)
    box.mark_sent(3)
    assert [r.seq for r in box.pending()] == [2, 4, 5]
    assert box.pending_count() == 3


def test_unsent_survive_reopen(tmp_path):
    path = str(tmp_path / "o.db")
    b = Outbox(path)
    for _ in range(3):
        add_scan(b)
    b.mark_sent(2)
    b.close()
    b = Outbox(path)
    assert [r.seq for r in b.pending()] == [1, 3]
    assert add_scan(b) == 4          # 일련번호가 이어진다
    b.close()


def test_seq_not_reused_after_purge(box):
    for _ in range(3):
        add_scan(box)
        box.mark_sent(box.pending()[0].seq)
    box._db.execute("UPDATE outbox SET sent_at = '2000-01-01T00:00:00.000+00:00'")
    assert box.purge(retention_days=7) == 3
    assert add_scan(box) == 4


def test_purge_keeps_unsent_and_recent(box):
    for _ in range(3):
        add_scan(box)
    box.mark_sent(1)
    box.mark_sent(2)
    box._db.execute("UPDATE outbox SET sent_at = '2000-01-01T00:00:00.000+00:00' WHERE seq = 1")
    assert box.purge(retention_days=7) == 1
    assert [r.seq for r in box.pending()] == [3]
    assert box._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2


def test_failed_make_rolls_back(box):
    with pytest.raises(RuntimeError):
        box.add(lambda seq: (_ for _ in ()).throw(RuntimeError("boom")))
    assert box.pending_count() == 0
    assert add_scan(box) >= 1


def test_concurrent_adds(box):
    def worker():
        for _ in range(100):
            add_scan(box)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = box.pending(limit=1000)
    assert len(rows) == 400 and len({r.seq for r in rows}) == 400
