"""
디스크 outbox (SQLite): 스캔을 먼저 여기에 기록한 뒤 발행한다 (오프라인 우선).

  add()        스캔 1건 기록. commit이 끝나면 전원이 꺼져도 남는다 (WAL + synchronous=FULL)
  pending()    아직 브로커 확인(PUBACK)을 받지 못한 메시지를 seq 순서대로
  mark_sent()  PUBACK을 받은 메시지에 전송 시각 기록
  purge()      전송 완료 후 보관 기간이 지난 기록 삭제 (전송 전 기록은 절대 지우지 않는다)

seq는 AUTOINCREMENT라서 기록을 지워도 재사용되지 않는다 → 메시지의 seq(스캐너별 일련번호)로 쓴다.
스캔 루프(쓰기)와 발행 스레드(읽기·표시)가 함께 쓰므로 연결 하나를 잠금으로 보호한다.
"""
import os
import sqlite3
import threading
from collections import namedtuple
from datetime import datetime, timedelta, timezone

PendingMessage = namedtuple("PendingMessage", "seq id topic payload")


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Outbox:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        # timeout: 다른 프로그램(sqlite3 CLI 등)이 잠깐 잠그고 있으면 최대 5초 기다린다
        self._db = sqlite3.connect(path, timeout=5.0, check_same_thread=False,
                                   isolation_level=None)  # 트랜잭션은 직접 BEGIN/COMMIT
        self._db.execute("PRAGMA journal_mode=WAL")
        # FULL: commit할 때마다 디스크에 반영 → 갑자기 전원이 꺼져도 commit된 스캔은 남는다
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS outbox (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                id         TEXT NOT NULL UNIQUE,
                topic      TEXT NOT NULL,
                payload    BLOB NOT NULL,
                created_at TEXT NOT NULL,   -- UTC
                sent_at    TEXT             -- UTC, NULL이면 미전송
            );
            CREATE INDEX IF NOT EXISTS outbox_unsent ON outbox(seq) WHERE sent_at IS NULL;
        """)

    def add(self, make_message):
        """
        make_message(seq) → (id, topic, payload) 로 메시지를 만들어 기록하고 seq를 반환한다.
        payload에 seq를 넣어야 해서, 같은 트랜잭션 안에서 seq를 먼저 받은 뒤 내용을 채운다.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "INSERT INTO outbox (id, topic, payload, created_at) VALUES (?, '', x'', ?)",
                    (f"tmp-{threading.get_ident()}", _utc_now()))
                seq = cur.lastrowid
                msg_id, topic, payload = make_message(seq)
                self._db.execute("UPDATE outbox SET id = ?, topic = ?, payload = ? WHERE seq = ?",
                                 (msg_id, topic, payload, seq))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return seq

    def pending(self, limit=100, exclude=()):
        """미전송 메시지를 seq 순서로 최대 limit개. exclude의 seq(이미 발행 중)는 제외."""
        exclude = set(exclude)
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, id, topic, payload FROM outbox WHERE sent_at IS NULL "
                "ORDER BY seq LIMIT ?", (limit + len(exclude),)).fetchall()
        return [PendingMessage(s, i, t, bytes(p)) for s, i, t, p in rows if s not in exclude][:limit]

    def mark_sent(self, seq):
        with self._lock:
            self._db.execute("UPDATE outbox SET sent_at = ? WHERE seq = ? AND sent_at IS NULL",
                             (_utc_now(), seq))

    def pending_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]

    def purge(self, retention_days):
        """전송 완료 후 retention_days가 지난 기록을 지우고 지운 개수를 반환한다."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="milliseconds")
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (cutoff,))
            return cur.rowcount

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
