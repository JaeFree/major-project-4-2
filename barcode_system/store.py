"""
스캔 원본 기록 DB (SQLite, WAL).

테이블
  scans     스캔 1건 = 1행. id(uuid)가 PRIMARY KEY → 같은 메시지가 여러 번 와도 1행 (멱등성)
  scanners  스캐너별 현재 상태(online/offline/lost)와 마지막 수신
  rejected  형식이 틀렸거나 토픽과 내용이 맞지 않는 메시지 (버리지 않고 원본 보관)

시각
  scanned_at  스캐너가 찍은 스캔 시각을 UTC로 바꾼 값 (정렬·기간 조회용)
  scan_ts     스캐너가 보낸 원래 문자열 (시간대 포함)
  received_at 이 서버가 받은 시각 (UTC). 파이는 RTC가 없어 스캐너 시각이 틀릴 수 있으므로 함께 남긴다
  모든 시각은 'YYYY-MM-DDTHH:MM:SS.mmm+00:00' 형식이라 문자열 비교로 정렬된다.

WAL 모드라서 나중에 웹 페이지 등 다른 프로그램이 읽는 동안에도 기록이 막히지 않는다.
PRAGMA user_version으로 스키마 버전을 남겨, 기말 업무 규칙 추가 때 이전(migration)할 수 있게 한다.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          TEXT PRIMARY KEY,         -- 메시지 uuid
    scanner_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,         -- 스캐너별 일련번호
    type        TEXT NOT NULL,            -- EAN13, CODE128 ...
    data        TEXT NOT NULL,            -- 바코드 값
    scanned_at  TEXT NOT NULL,            -- 스캔 시각 (UTC)
    scan_ts     TEXT NOT NULL,            -- 스캐너가 보낸 원래 시각 문자열
    received_at TEXT NOT NULL             -- 수신 시각 (UTC)
);
CREATE INDEX IF NOT EXISTS scans_data        ON scans(data);
CREATE INDEX IF NOT EXISTS scans_scanned_at  ON scans(scanned_at);
CREATE INDEX IF NOT EXISTS scans_scanner_seq ON scans(scanner_id, seq);

CREATE TABLE IF NOT EXISTS scanners (
    scanner_id   TEXT PRIMARY KEY,
    state        TEXT,                    -- online / offline / lost
    state_at     TEXT,                    -- 상태 메시지를 받은 시각 (UTC)
    last_scan_at TEXT,                    -- 마지막 스캔 수신 시각 (UTC)
    last_seq     INTEGER                  -- 지금까지 받은 가장 큰 seq
);

CREATE TABLE IF NOT EXISTS rejected (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    payload     BLOB NOT NULL,
    reason      TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def utc_now():
    return to_utc(datetime.now(timezone.utc))


def to_utc(dt):
    """aware datetime 또는 ISO 문자열 → UTC ISO 문자열(밀리초). 시간대가 없으면 이 컴퓨터의 지역 시각으로 본다."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


class ScanStore:
    INSERTED, DUPLICATE, CONFLICT = "inserted", "duplicate", "conflict"

    def __init__(self, path, timeout=5.0):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        # isolation_level=None: 트랜잭션을 직접 BEGIN/COMMIT. timeout: 다른 프로그램이 잠그면 기다림
        self._db = sqlite3.connect(path, timeout=timeout, check_same_thread=False,
                                   isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")  # commit 후 전원이 꺼져도 남도록
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"DB 스키마 버전 {version}이 이 프로그램({SCHEMA_VERSION})보다 새롭습니다.")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------ 기록
    def _tx(self, fn):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self._db)
                self._db.execute("COMMIT")
                return result
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def save_scan(self, msg, received_at=None):
        """검증된 scan 메시지(messages.parse_scan 결과)를 저장한다 → INSERTED / DUPLICATE / CONFLICT."""
        received_at = received_at or utc_now()
        row = (msg["id"], msg["scanner_id"], msg["seq"], msg["type"], msg["data"],
               to_utc(msg["ts"]), msg["ts"], received_at)

        def work(db):
            cur = db.execute("INSERT OR IGNORE INTO scans (id, scanner_id, seq, type, data, scanned_at,"
                             " scan_ts, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
            if cur.rowcount == 0:
                old = db.execute("SELECT scanner_id, seq, type, data, scan_ts FROM scans WHERE id = ?",
                                 (msg["id"],)).fetchone()
                same = old == (msg["scanner_id"], msg["seq"], msg["type"], msg["data"], msg["ts"])
                return self.DUPLICATE if same else self.CONFLICT
            db.execute("""INSERT INTO scanners (scanner_id, last_scan_at, last_seq) VALUES (?, ?, ?)
                          ON CONFLICT(scanner_id) DO UPDATE SET
                              last_scan_at = excluded.last_scan_at,
                              last_seq = MAX(COALESCE(last_seq, 0), excluded.last_seq)""",
                       (msg["scanner_id"], received_at, msg["seq"]))
            return self.INSERTED

        return self._tx(work)

    def update_status(self, msg, received_at=None):
        """status 메시지를 받은 순서대로 반영한다.
        (ts로 비교하지 않는 이유: LWT 'lost'의 ts는 연결할 때 미리 만든 값이라 실제 끊긴 시각보다 이르다)"""
        received_at = received_at or utc_now()
        self._tx(lambda db: db.execute(
            """INSERT INTO scanners (scanner_id, state, state_at) VALUES (?, ?, ?)
               ON CONFLICT(scanner_id) DO UPDATE SET state = excluded.state, state_at = excluded.state_at""",
            (msg["scanner_id"], msg["state"], received_at)))

    def reject(self, topic, payload, reason, received_at=None):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._tx(lambda db: db.execute(
            "INSERT INTO rejected (topic, payload, reason, received_at) VALUES (?, ?, ?, ?)",
            (topic, bytes(payload), reason, received_at or utc_now())))

    # ------------------------------------------------------------ 조회
    def _query(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    @staticmethod
    def _where(scanner_id=None, data=None, since=None, until=None, ids=None):
        cond, args = [], []
        if scanner_id:
            cond.append("scanner_id = ?")
            args.append(scanner_id)
        if data:
            cond.append("data = ?")
            args.append(data)
        if since:
            cond.append("scanned_at >= ?")
            args.append(to_utc(since))
        if until:
            cond.append("scanned_at < ?")
            args.append(to_utc(until))
        if ids:
            cond.append(f"id IN ({','.join('?' * len(ids))})")
            args.extend(ids)
        return (" WHERE " + " AND ".join(cond)) if cond else "", args

    def scans(self, limit=None, newest_first=True, **filters):
        where, args = self._where(**filters)
        sql = (f"SELECT * FROM scans{where} ORDER BY scanned_at {'DESC' if newest_first else 'ASC'}, "
               f"scanner_id, seq")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._query(sql, args)

    def count(self, **filters):
        where, args = self._where(**filters)
        return self._query(f"SELECT COUNT(*) AS n FROM scans{where}", args)[0]["n"]

    def scanners(self):
        return self._query("""SELECT s.*, (SELECT COUNT(*) FROM scans c WHERE c.scanner_id = s.scanner_id)
                              AS scan_count FROM scanners s ORDER BY scanner_id""")

    def rejected(self, limit=None):
        sql = "SELECT * FROM rejected ORDER BY rowid DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._query(sql)

    def gaps(self, scanner_id=None):
        """
        스캐너별 seq에서 빠진 구간 [(scanner_id, 시작, 끝), ...].
        저장된 seq 사이의 빈 번호만 찾는다. 삭제 기능으로 지운 구간도 빈 번호로 보인다.
        (재전송 때문에 도착 순서가 뒤바뀔 수 있어 실시간 경보 대신 조회로 확인한다)
        """
        where, args = ("WHERE scanner_id = ?", (scanner_id,)) if scanner_id else ("", ())
        return self._query(f"""
            SELECT scanner_id, prev + 1 AS gap_start, seq - 1 AS gap_end FROM (
                SELECT scanner_id, seq,
                       LAG(seq) OVER (PARTITION BY scanner_id ORDER BY seq) AS prev
                FROM (SELECT DISTINCT scanner_id, seq FROM scans {where}))
            WHERE seq - prev > 1 ORDER BY scanner_id, gap_start""", args)

    # ------------------------------------------------------------ 삭제·백업
    def delete_scans(self, all=False, **filters):
        """조건에 맞는 스캔 기록을 지우고 지운 개수를 반환한다. 조건 없이 지우려면 all=True가 필요하다."""
        where, args = self._where(**filters)
        if not where and not all:
            raise ValueError("삭제 조건이 없습니다 (전체 삭제는 all=True)")
        return self._tx(lambda db: db.execute(f"DELETE FROM scans{where}", args).rowcount)

    def delete_rejected(self):
        return self._tx(lambda db: db.execute("DELETE FROM rejected").rowcount)

    def backup(self, dest):
        """실행 중에도 일관된 복사본을 만든다 (SQLite 온라인 백업 API)."""
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()

    def vacuum(self):
        """삭제 후 파일 크기를 줄인다 (DB 전체를 다시 쓰므로 큰 DB에서는 시간이 걸린다)."""
        with self._lock:
            self._db.execute("VACUUM")

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
