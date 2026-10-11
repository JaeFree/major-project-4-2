"""같은 바코드의 중복 저장/발행 방지."""

# 같은 값의 인식이 이 시간(초) 이상 끊기면 연속 인식 횟수를 처음부터 다시 센다
CONFIRM_WINDOW = 2.0
# presence 모드에서 '사라졌다'고 보려면 최소 이 시간(초)은 보이지 않아야 한다.
# 연속된 두 프레임 사이에도 항상 시간 간격이 있으므로, cooldown=0을 그대로 쓰면
# 매 프레임이 '새 등장'이 되어 계속 저장되는 문제가 있었다.
MIN_ABSENCE = 0.5


class ScanTracker:
    """
    바코드 값별 인식 이력을 보고 '지금 저장할지'를 판단한다.

    ※ 쿨다운은 마지막 '인식' 시각이 아니라 마지막 '저장' 시각을 기준으로 계산한다.
      인식 시각을 기준으로 하면 바코드가 매 프레임 인식될 때마다 쿨다운이 새로 시작되므로,
      계속 비추고 있는 동안에는 쿨다운이 끝나지 않아 다시 저장되지 않는다.

    confirm  : 같은 값이 CONFIRM_WINDOW초 이내 간격으로 confirm번 인식돼야 유효하다
               (한 프레임에서만 튀는 오인식 방지. I25, Codabar처럼 체크섬 없는 코드에서 특히 유용)
    interval : 유효한 인식이 이어지는 동안 cooldown초마다 저장
    presence : 등장 1번당 1회 저장. max(cooldown, MIN_ABSENCE)초 이상 안 보이다가 다시 보이면
               새로운 등장으로 본다
    시간 값은 time.monotonic()을 쓴다. 라즈베리파이4는 RTC가 없어 부팅 후 NTP 동기화 때 시스템
    시계가 크게 바뀔 수 있는데, time.time()으로 간격을 재면 시계가 뒤로 가는 순간 차이가 음수가
    되어 그만큼 저장이 막힌다.
    """

    class _Track:
        __slots__ = ("last_seen", "appeared", "hits", "last_saved")

        def __init__(self, now):
            self.last_seen = now    # 마지막으로 인식된 시각
            self.appeared = now     # 이번 등장이 시작된 시각 (presence 모드)
            self.hits = 0           # 연속 인식 횟수 (confirm 판단)
            self.last_saved = None  # 마지막으로 저장한 시각

    def __init__(self, cooldown, mode="interval", confirm=1):
        if mode not in ("interval", "presence"):
            raise ValueError(f"알 수 없는 mode: {mode}")
        if cooldown < 0 or confirm < 1:
            raise ValueError("cooldown은 0 이상, confirm은 1 이상이어야 합니다.")
        self.cooldown = cooldown
        self.mode = mode
        self.confirm = confirm
        self.absence = max(cooldown, MIN_ABSENCE)  # presence 모드의 '사라짐' 기준
        self._tracks = {}

    def should_save(self, data, now):
        """이번 인식을 저장해야 하면 True. True를 반환할 때 '저장함'으로 기록까지 한다.
        (실제 저장이 실패해도 저장한 것으로 간주해 매 프레임 재시도·오류 로그 폭주를 막는다.
         실패한 값은 오류 로그에 데이터와 함께 남는다.)"""
        t = self._tracks.get(data)
        if t is None:
            t = self._tracks[data] = self._Track(now)
        else:
            gap = now - t.last_seen
            if gap >= CONFIRM_WINDOW:
                t.hits = 0         # 인식이 오래 끊김 → 연속 인식 횟수를 처음부터
            if gap >= self.absence:
                t.appeared = now   # 충분히 오래 안 보였음 → 새로 나타난 것으로 간주
            t.last_seen = now
        t.hits += 1

        if t.hits < self.confirm:
            return False           # 아직 확정되지 않음

        if self.mode == "presence":
            # 이번 등장 이후 아직 저장하지 않았을 때만
            ok = t.last_saved is None or t.last_saved < t.appeared
        else:
            # 마지막 '저장' 이후 cooldown이 지났을 때만 → 계속 보이고 있어도 주기적으로 저장된다
            ok = t.last_saved is None or now - t.last_saved >= self.cooldown
        if ok:
            t.last_saved = now
        return ok

    def prune(self, now):
        """판단에 더 이상 영향을 주지 않는 오래된 기록을 지운다 (장시간 운영 시 메모리 증가 방지).
        마지막 인식 후 max(cooldown, absence, CONFIRM_WINDOW)가 지난 기록은 지워도 판단 결과가 같다."""
        limit = max(self.cooldown, self.absence, CONFIRM_WINDOW)
        for key in [k for k, t in self._tracks.items() if now - t.last_seen >= limit]:
            del self._tracks[key]

    def __len__(self):
        return len(self._tracks)
