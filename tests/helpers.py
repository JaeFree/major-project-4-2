"""여러 테스트 파일이 함께 쓰는 도우미: MQTT 구성 요소 생성, 실제 프로세스 실행·종료, DB 읽기."""
import os
import signal
import sqlite3
import subprocess
import sys

from barcode_system import messages
from barcode_system.outbox import Outbox
from barcode_system.publisher import Publisher
from barcode_system.spool import Spool
from barcode_system.subscriber import DbWriter
from conftest import ROOT


def creds(user):
    """conftest.make_secure_broker가 만든 사용자의 접속 정보."""
    return {"username": user, "password": f"pw-{user}"}


def make_writer(store, tmp_path, broker, **kw):
    kw.setdefault("spool_interval", 0.5)
    w = DbWriter(store, Spool(str(tmp_path / "spool.jsonl")), broker.host, broker.port,
                 keepalive=5, **creds("db-writer"), **kw)
    w.start()
    assert w.wait_ready(10), "db-writer 구독 실패"
    return w


def make_publisher(tmp_path, broker, name="o.db"):
    box = Outbox(str(tmp_path / name))
    pub = Publisher(box, "dock-01", broker.host, broker.port, keepalive=5, ack_timeout=3,
                    retention_days=None, **creds("dock-01"))
    return box, pub


def add_scans(box, n, scanner="dock-01"):
    ids = []
    for _ in range(n):
        def make(seq):
            m = messages.build_scan(scanner, seq, "CODE128", f"ITEM-{seq:05d}")
            ids.append(m["id"])
            return m["id"], messages.scan_topic(scanner), messages.encode(m)
        box.add(make)
    return ids


def db_ids(store):
    return {r["id"] for r in store.scans()}


# ---------------------------------------------------------------- 실제 프로세스
def start_process(module_args, log_path):
    """python -m <모듈> ... 을 프로젝트 폴더에서 실행. 로그는 파일로."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m"] + module_args, cwd=ROOT, stdout=log,
                            stderr=subprocess.STDOUT, creationflags=flags,
                            env=dict(os.environ, PYTHONUTF8="1"))
    proc.log_file, proc.log_path = log, log_path
    return proc


def graceful_stop(proc):
    """Windows는 Ctrl+Break, 그 외는 SIGTERM → 앱이 정상 종료 경로(finally)를 탄다."""
    proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
    code = proc.wait(timeout=20)
    proc.log_file.close()
    return code


def hard_kill(proc):
    proc.kill()
    proc.wait()
    proc.log_file.close()


def read_log(proc):
    return open(proc.log_path, encoding="utf-8").read()


def scanner_args(images, port, outbox, *extra):
    return ["barcode_system.scanner_app", "--id", "dock-01", "--camera", "files",
            "--images", str(images), "--fps", "10", "--mode", "interval", "--cooldown", "0.3",
            "--confirm", "1", "--host", "127.0.0.1", "--port", str(port), "--keepalive", "5",
            "--outbox", str(outbox), *extra]


def query(db_path, sql):
    """실행 중인 프로그램의 SQLite DB를 읽기 전용으로 조회한다."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return db.execute(sql).fetchall()
    except sqlite3.OperationalError:  # 아직 테이블이 없음
        return []
    finally:
        db.close()
