"""
파이 DB의 스캔 → 외부 재고 대시보드 전송 (4단계)

대시보드 API (팀원 제작, 수정하지 않음)
  POST <url>/api/scan                         {barcode, deviceId, timestamp} → {ok, barcode, transactionId}
  GET  <url>/api/products/<바코드>/transactions  최근 50건 [{id, deviceId, timestamp, ...}] (timestamp 내림차순)
  서버는 받을 때마다 재고 +1·입고 이력 1건을 만들고, 중복을 막지 않는다.

같은 스캔이 두 번 들어가 재고가 두 번 오르지 않도록
  - 보낸 기록은 별도 DB(state)에 남긴다. db-writer의 DB(barcodes.db)는 읽기만 한다.
  - 보내기 직전에 'sending'으로 기록하고, 성공 응답을 받으면 'sent'로 바꾼다.
    'sending'으로 남은 건(응답 없음·5xx·프로그램 중단)은 결과를 알 수 없으므로, 다시 보내기 전에
    대시보드 이력에서 같은 deviceId+timestamp를 찾아보고 있으면 'sent'로 처리한다.
  - 한 번에 한 건씩 받은 순서대로 보낸다. 앞의 건이 해결되기 전에는 다음 건으로 넘어가지 않으므로,
    이력 확인 대상이 항상 그 상품의 최근 이력(50건 안)에 있다.
  - timestamp는 스캔 시각(UTC)을 밀리초까지 'Z' 형식으로 보내 스캔마다 값이 다르게 한다.

보내지 않는 것
  - types에 없는 종류 (기본 CODE128만. 웹캠이 흐릴 때 잘못 읽은 EAN 기록이 재고에 들어가지 않게)
  - 처음 실행하기 전에 저장돼 있던 스캔 (send_existing = false일 때. 시험 기록이 재고에 들어가지 않게)
  - Firestore 문서 ID로 쓸 수 없는 바코드 ('/' 포함, '.', '..', '__x__') → 'skipped'로 기록
"""
import json
import logging
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .store import utc_now

log = logging.getLogger("barcode.fwd")

SENDING, SENT, FAILED, SKIPPED = "sending", "sent", "failed", "skipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forwards (
    scan_id        TEXT PRIMARY KEY,      -- barcodes.db scans.id
    barcode        TEXT NOT NULL,
    device_id      TEXT NOT NULL,
    timestamp      TEXT NOT NULL,         -- 대시보드로 보낸 값
    state          TEXT NOT NULL,         -- sending / sent / failed / skipped
    attempts       INTEGER NOT NULL DEFAULT 0,
    first_try_at   TEXT,
    last_try_at    TEXT,
    done_at        TEXT,
    transaction_id TEXT,                  -- 대시보드 이력 문서 ID
    error          TEXT
);
CREATE INDEX IF NOT EXISTS forwards_state ON forwards(state);
"""


def to_api_timestamp(scanned_at):
    """'2026-10-16T01:54:00.015+00:00' → '2026-10-16T01:54:00.015Z'"""
    if not scanned_at.endswith("+00:00"):
        raise ValueError(f"UTC 시각이 아닙니다: {scanned_at!r}")
    return scanned_at[:-6] + "Z"


def unsendable_reason(barcode):
    """대시보드가 처리할 수 없는 바코드면 이유, 아니면 None (Firestore 문서 ID 규칙)."""
    if not barcode:
        return "빈 값"
    if "/" in barcode:
        return "'/' 포함"
    if barcode in (".", ".."):
        return "'.' 또는 '..'"
    if re.fullmatch(r"__.*__", barcode):
        return "'__'로 시작하고 끝남"
    return None


# ================================================================ HTTP
class ApiError(Exception):
    """보낸 결과를 알 수 없는 오류 (연결 실패, 시간 초과, 5xx, 예상 밖 응답)."""


class ApiRejected(Exception):
    """서버가 요청을 거부함 (400). 다시 보내도 같으므로 재시도하지 않는다."""


class DashboardClient:
    # 대시보드 API 앞단의 Cloudflare가 urllib 기본 User-Agent("Python-urllib/x.y")를 막는다
    # (403, Error 1010. 2026-10-16 확인) → 프로그램 이름으로 보낸다
    USER_AGENT = "barcode-forwarder/1.0"

    def __init__(self, base_url, timeout=10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json",
                                              "User-Agent": self.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            if e.code == 400:
                raise ApiRejected(f"HTTP 400 {detail}") from None
            raise ApiError(f"HTTP {e.code} {detail}") from None
        except (urllib.error.URLError, OSError) as e:   # 연결 실패, 시간 초과(socket.timeout)
            raise ApiError(f"연결 오류: {getattr(e, 'reason', e)}") from None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(f"JSON이 아닌 응답: {raw[:200]!r}") from None

    def post_scan(self, barcode, device_id, timestamp):
        """성공하면 transactionId. 실패는 ApiError/ApiRejected."""
        res = self._request("POST", "/api/scan",
                            {"barcode": barcode, "deviceId": device_id, "timestamp": timestamp})
        if not isinstance(res, dict) or res.get("ok") is not True:
            raise ApiError(f"예상 밖 응답: {res!r}")
        return res.get("transactionId")

    def find_transaction(self, barcode, device_id, timestamp):
        """대시보드 이력에 같은 deviceId+timestamp가 있으면 그 이력 ID, 없으면 None."""
        res = self._request("GET", f"/api/products/{urllib.parse.quote(barcode, safe='')}/transactions")
        if not isinstance(res, list):
            raise ApiError(f"예상 밖 응답: {res!r}")
        for tx in res:
            if isinstance(tx, dict) and tx.get("deviceId") == device_id and tx.get("timestamp") == timestamp:
                return tx.get("id") or "?"
        return None


# ================================================================ 보낸 기록
class ForwardState:
    """보낸 기록 DB. 원본 스캔 DB(barcodes.db)는 읽기 전용으로 붙여(ATTACH) 함께 조회한다."""

    def __init__(self, state_path, source_path, types=("CODE128",), send_existing=False):
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(source_path).is_file():
            raise FileNotFoundError(f"스캔 DB가 없습니다: {source_path} (db-writer를 먼저 실행)")
        self.types = tuple(types)
        self._lock = threading.Lock()
        # uri=True: ATTACH에 'file:...?mode=ro' 형식을 쓰기 위해 (state_path는 일반 경로 그대로 쓰인다)
        self._db = sqlite3.connect(state_path, timeout=5.0, check_same_thread=False,
                                   isolation_level=None, uri=True)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")  # 'sending' 기록이 전송보다 먼저 디스크에 남도록
        self._db.executescript(_SCHEMA)
        src_uri = Path(source_path).resolve().as_uri() + "?mode=ro"
        self._db.execute("ATTACH DATABASE ? AS src", (src_uri,))

        # 처음 실행한 시각 이후에 받은 스캔만 보낸다 (send_existing이면 전부). 이후 실행에서는 바꾸지 않는다.
        row = self._db.execute("SELECT value FROM meta WHERE key = 'start_after'").fetchone()
        if row is None:
            start = "" if send_existing else utc_now()
            self._db.execute("INSERT INTO meta (key, value) VALUES ('start_after', ?)", (start,))
            self.start_after, self.first_run = start, True
        else:
            self.start_after, self.first_run = row[0], False

    def next_pending(self):
        """다음에 처리할 스캔 1건 (받은 순서). 아직 시도하지 않았거나 결과를 모르는('sending') 건."""
        marks = ",".join("?" * len(self.types))
        with self._lock:
            cur = self._db.execute(f"""
                SELECT s.id, s.scanner_id, s.seq, s.type, s.data, s.scanned_at, f.state, f.attempts
                FROM src.scans s LEFT JOIN forwards f ON f.scan_id = s.id
                WHERE s.type IN ({marks}) AND s.received_at >= ?
                  AND (f.scan_id IS NULL OR f.state = '{SENDING}')
                ORDER BY s.received_at, s.scanner_id, s.seq LIMIT 1""",
                (*self.types, self.start_after))
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([c[0] for c in cur.description], row))

    def _upsert(self, scan_id, barcode, device_id, timestamp, state, *, attempt=False,
                transaction_id=None, error=None):
        now = utc_now()
        done = now if state in (SENT, FAILED, SKIPPED) else None
        with self._lock:
            self._db.execute("""
                INSERT INTO forwards (scan_id, barcode, device_id, timestamp, state, attempts,
                                      first_try_at, last_try_at, done_at, transaction_id, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_id) DO UPDATE SET
                    state = excluded.state,
                    attempts = attempts + ?,
                    first_try_at = COALESCE(first_try_at, excluded.first_try_at),
                    last_try_at = COALESCE(excluded.last_try_at, last_try_at),
                    done_at = excluded.done_at,
                    transaction_id = COALESCE(excluded.transaction_id, transaction_id),
                    error = excluded.error""",
                (scan_id, barcode, device_id, timestamp, state, int(attempt),
                 now if attempt else None, now if attempt else None, done, transaction_id, error,
                 int(attempt)))

    def mark_sending(self, scan_id, barcode, device_id, timestamp):
        self._upsert(scan_id, barcode, device_id, timestamp, SENDING, attempt=True)

    def mark_error(self, scan_id, barcode, device_id, timestamp, error):
        """결과를 모름 → 'sending' 유지, 마지막 오류만 남긴다."""
        self._upsert(scan_id, barcode, device_id, timestamp, SENDING, error=error)

    def mark_done(self, scan_id, barcode, device_id, timestamp, state, transaction_id=None, error=None):
        self._upsert(scan_id, barcode, device_id, timestamp, state,
                     transaction_id=transaction_id, error=error)

    def summary(self):
        """상태별 건수와 아직 보내지 않은 건수."""
        marks = ",".join("?" * len(self.types))
        with self._lock:
            counts = dict(self._db.execute("SELECT state, COUNT(*) FROM forwards GROUP BY state"))
            waiting = self._db.execute(f"""
                SELECT COUNT(*) FROM src.scans s LEFT JOIN forwards f ON f.scan_id = s.id
                WHERE s.type IN ({marks}) AND s.received_at >= ? AND f.scan_id IS NULL""",
                (*self.types, self.start_after)).fetchone()[0]
        return {"waiting": waiting, **{k: counts.get(k, 0) for k in (SENDING, SENT, FAILED, SKIPPED)}}

    def recent(self, limit=20):
        with self._lock:
            cur = self._db.execute("""SELECT last_try_at, state, barcode, device_id, timestamp, attempts,
                                             transaction_id, error
                                      FROM forwards ORDER BY COALESCE(last_try_at, done_at) DESC
                                      LIMIT ?""", (int(limit),))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass


# ================================================================ 전송 루프
class Forwarder:
    def __init__(self, state, client, device_id=None, poll=2.0, backoff_max=60.0):
        self.state = state
        self.client = client
        self.device_id = device_id   # None이면 스캐너 ID를 그대로 deviceId로 보낸다
        self.poll = poll
        self.backoff_max = backoff_max
        self._backoff = 0.0

    def step(self):
        """1건 처리. 반환: 'idle'(보낼 것 없음) / 'done'(처리함) / 'retry'(오류, 나중에 다시)"""
        item = self.state.next_pending()
        if item is None:
            return "idle"
        barcode = item["data"]
        device_id = self.device_id or item["scanner_id"]
        timestamp = to_api_timestamp(item["scanned_at"])
        key = (item["id"], barcode, device_id, timestamp)
        label = f"{barcode} ({item['scanner_id']} #{item['seq']})"

        reason = unsendable_reason(barcode)
        if reason:
            self.state.mark_done(*key, SKIPPED, error=reason)
            log.warning("전송 제외 %s: %s", label, reason)
            return "done"

        try:
            if item["state"] == SENDING:
                # 이전 시도의 결과를 모름 → 대시보드에 이미 들어갔는지 먼저 확인
                tx = self.client.find_transaction(barcode, device_id, timestamp)
                if tx:
                    self.state.mark_done(*key, SENT, transaction_id=tx)
                    log.info("전송 확인 %s → 대시보드 이력에 있음 (transactionId=%s)", label, tx)
                    self._backoff = 0.0
                    return "done"
            self.state.mark_sending(*key)
            tx = self.client.post_scan(barcode, device_id, timestamp)
        except ApiRejected as e:
            self.state.mark_done(*key, FAILED, error=str(e))
            log.error("전송 거부 %s: %s (다시 보내지 않음)", label, e)
            return "done"
        except ApiError as e:
            self.state.mark_error(*key, error=str(e))
            self._backoff = min(self.backoff_max, max(2.0, self._backoff * 2))
            log.warning("전송 실패 %s: %s → %.0f초 뒤 확인 후 재시도", label, e, self._backoff)
            return "retry"

        self.state.mark_done(*key, SENT, transaction_id=tx)
        self._backoff = 0.0
        log.info("전송 [%s] %s → transactionId=%s", item["type"], label, tx)
        return "done"

    def run(self, stop_event, once=False):
        """stop_event가 설 때까지 반복. once면 보낼 것이 없거나 오류가 나면 끝낸다.
        반환: 오류로 끝났으면 False"""
        while not stop_event.is_set():
            result = self.step()
            if result == "done":
                continue
            if once:
                return result == "idle"
            stop_event.wait(self._backoff if result == "retry" else self.poll)
        return True
