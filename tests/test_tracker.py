import pytest

from barcode_system.tracker import CONFIRM_WINDOW, MIN_ABSENCE, ScanTracker

FRAME = 1 / 30  # 30fps


def feed(tracker, data, start, end, step=FRAME):
    """start~end 동안 매 프레임 인식됐다고 가정하고 저장 판정 시각 목록을 돌려준다."""
    saved, t = [], start
    while t < end:
        if tracker.should_save(data, t):
            saved.append(round(t, 3))
        t += step
    return saved


def test_interval_saves_every_cooldown_while_visible():
    tr = ScanTracker(cooldown=1.0, mode="interval", confirm=1)
    saved = feed(tr, "A", 0.0, 3.5)
    assert len(saved) == 4                     # 0, 1, 2, 3초 무렵
    assert all(b - a >= 1.0 for a, b in zip(saved, saved[1:]))


def test_confirm_requires_consecutive_hits():
    tr = ScanTracker(cooldown=5.0, mode="interval", confirm=3)
    assert not tr.should_save("A", 0.0)
    assert not tr.should_save("A", 0.1)
    assert tr.should_save("A", 0.2)


def test_confirm_resets_after_long_gap():
    tr = ScanTracker(cooldown=0.0, mode="interval", confirm=2)
    assert not tr.should_save("A", 0.0)
    # CONFIRM_WINDOW 이상 끊겼다가 다시 한 번 → 처음부터 다시 세므로 저장 안 됨 (한 번 튄 오인식)
    assert not tr.should_save("A", CONFIRM_WINDOW + 0.1)
    assert tr.should_save("A", CONFIRM_WINDOW + 0.2)


def test_values_are_tracked_independently():
    tr = ScanTracker(cooldown=10.0, mode="interval", confirm=1)
    assert tr.should_save("A", 0.0)
    assert tr.should_save("B", 0.0)
    assert not tr.should_save("A", 0.1)


def test_presence_saves_once_per_appearance():
    tr = ScanTracker(cooldown=1.0, mode="presence", confirm=1)
    assert feed(tr, "A", 0.0, 5.0) == [0.0]              # 계속 보이는 동안 1회
    assert feed(tr, "A", 5.5, 6.0) == []                 # 0.5초 공백 < cooldown → 같은 등장
    assert len(feed(tr, "A", 7.5, 8.0)) == 1             # 1.5초 공백 → 새 등장


def test_presence_cooldown_zero_does_not_save_every_frame():
    """회귀 테스트: 예전에는 cooldown=0이면 매 프레임이 새 등장으로 처리돼 계속 저장됐다."""
    tr = ScanTracker(cooldown=0.0, mode="presence", confirm=1)
    assert len(feed(tr, "A", 0.0, 2.0)) == 1
    # MIN_ABSENCE 이상 사라졌다가 다시 나타나면 새 등장
    assert len(feed(tr, "A", 2.0 + MIN_ABSENCE + 0.1, 3.0)) == 1


def test_prune_removes_old_entries_without_changing_decisions():
    tr = ScanTracker(cooldown=1.0, mode="presence", confirm=1)
    assert tr.should_save("A", 0.0)
    tr.prune(1.0)
    assert len(tr) == 1                  # 아직 판단에 필요
    tr.prune(100.0)
    assert len(tr) == 0
    assert tr.should_save("A", 100.0)    # 지운 뒤에도 '새 등장'으로 동일하게 판단


@pytest.mark.parametrize("kwargs", [{"cooldown": -1}, {"cooldown": 1, "confirm": 0},
                                    {"cooldown": 1, "mode": "bogus"}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        ScanTracker(**kwargs)
