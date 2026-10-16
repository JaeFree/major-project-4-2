"""4단계: 파이 DB → 대시보드 전송. 실제 대시보드 대신 같은 API를 흉내 내는 가짜 서버로 시험한다."""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from barcode_system import forwarder_app, messages
from barcode_system.dashboard_forwarder import (FAILED, SENDING, SENT, SKIPPED, DashboardClient,
                                                Forwarder, ForwardState, to_api_timestamp,
                                                unsendable_reason)
from barcode_system.store import ScanStore

OLD = "2000-01-01T00:00:00.000+00:00"   # 전송 기준 시각보다 앞선 수신 시각


class FakeDashboard:
    """팀원 서버(server/src)와 같은 동작: POST /api/scan → 이력 추가·재고 +1, 중복 방지 없음.
    mode로 장애를 흉내 낸다.
      down     : 503 응답 (기록 안 함)
      drop     : 기록한 뒤 응답 없이 연결을 끊음 (결과를 알 수 없는 경우)
      slow     : 기록한 뒤 delay초 기다렸다 응답 (클라이언트 시간 초과)
      reject   : 400 응답
      list_down: GET 이력 조회만 503
    once=True면 장애를 한 번만 내고 정상으로 돌아온다."""

    def __init__(self):
        self.tx = {}            # barcode → [이력]
        self.posts = 0
        self.agents = set()     # 받은 User-Agent (실제 서버 앞 Cloudflare가 urllib 기본값을 막음)
        self.mode, self.once, self.delay = None, False, 0.0
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _fault(self, *modes):
                with fake.lock:
                    fake.agents.add(self.headers.get("User-Agent"))
                    m = fake.mode if fake.mode in modes else None
                    if m and fake.once:
                        fake.mode = None
                return m

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/scan":
                    return self._send(404, {"error": "not found"})
                fault = self._fault("down", "drop", "slow", "reject")
                if fault == "down":
                    return self._send(503, {"error": "unavailable"})
                if fault == "reject" or not all(body.get(k) for k in ("barcode", "deviceId", "timestamp")):
                    return self._send(400, {"error": "barcode, deviceId, timestamp는 필수입니다."})
                with fake.lock:
                    fake.posts += 1
                    tx_id = f"tx{fake.posts}"
                    fake.tx.setdefault(body["barcode"], []).append(
                        {"id": tx_id, "barcode": body["barcode"], "deviceId": body["deviceId"],
                         "quantityDelta": 1, "type": "IN", "timestamp": body["timestamp"]})
                if fault == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return
                if fault == "slow":
                    time.sleep(fake.delay)
                self._send(200, {"ok": True, "barcode": body["barcode"], "transactionId": tx_id})

            def do_GET(self):
                parts = self.path.split("/")   # /api/products/<barcode>/transactions
                if len(parts) != 5 or parts[1:3] != ["api", "products"] or parts[4] != "transactions":
                    return self._send(404, {"error": "not found"})
                if self._fault("list_down"):
                    return self._send(503, {"error": "unavailable"})
                barcode = urllib.parse.unquote(parts[3])
                with fake.lock:
                    rows = sorted(fake.tx.get(barcode, []), key=lambda t: t["timestamp"], reverse=True)
                self._send(200, rows[:50])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stock(self, barcode):
        return len(self.tx.get(barcode, []))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def dash():
    d = FakeDashboard()
    yield d
    d.close()


@pytest.fixture
def scans(tmp_path):
    s = ScanStore(str(tmp_path / "barcodes.db"))
    yield s
    s.close()


@pytest.fixture
def make_state(tmp_path, scans):
    made = []

    def make(**kw):
        st = ForwardState(str(tmp_path / "forwarder.db"), scans.path, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


_seq = iter(range(1, 10_000))


def add(scans, data, type_="CODE128", received_at=None, scanner="dock-01",
        ts="2026-10-16T19:54:00.015+09:00"):
    m = messages.build_scan(scanner, next(_seq), type_, data, ts=ts)
    scans.save_scan(m, received_at=received_at)
    return m


def drain(fwd, limit=50):
    """오류가 나도 기다리지 않고 보낼 것이 없을 때까지 처리 (결과 목록 반환)."""
    results = []
    for _ in range(limit):
        r = fwd.step()
        results.append(r)
        if r == "idle":
            return results
    raise AssertionError(f"끝나지 않음: {results}")


def states(st):
    return [(r["barcode"], r["state"]) for r in reversed(st.recent())]


# ---------------------------------------------------------------- 변환·검사
def test_to_api_timestamp():
    assert to_api_timestamp("2026-10-16T01:54:00.015+00:00") == "2026-10-16T01:54:00.015Z"
    with pytest.raises(ValueError):
        to_api_timestamp("2026-10-16T10:54:00.015+09:00")


@pytest.mark.parametrize("value,ok", [("Apple123", True), ("pants2", True), ("TEST-001", True),
                                      ("a/b", False), (".", False), ("..", False), ("__x__", False),
                                      ("", False)])
def test_unsendable_reason(value, ok):
    assert (unsendable_reason(value) is None) == ok


# ---------------------------------------------------------------- 기본 전송
def test_sends_new_code128_only(dash, scans, make_state):
    add(scans, "OLD-1", received_at=OLD)              # 처음 실행 전 기록 → 보내지 않음
    st = make_state()
    assert st.first_run and st.start_after
    m = add(scans, "Apple123")
    add(scans, "55425674", type_="EAN8")              # 잘못 읽은 EAN → 보내지 않음
    add(scans, "pants2")
    fwd = Forwarder(st, DashboardClient(dash.url, 3))
    assert drain(fwd) == ["done", "done", "idle"]

    assert dash.stock("Apple123") == 1 and dash.stock("pants2") == 1
    assert dash.stock("OLD-1") == 0 and dash.stock("55425674") == 0
    tx = dash.tx["Apple123"][0]
    assert tx["deviceId"] == "dock-01"
    assert tx["timestamp"] == "2026-10-16T10:54:00.015Z"   # UTC, 밀리초, Z
    assert states(st) == [("Apple123", SENT), ("pants2", SENT)]
    assert st.recent()[-1]["transaction_id"] == "tx1"
    assert st.summary() == {"waiting": 0, SENDING: 0, SENT: 2, FAILED: 0, SKIPPED: 0}
    assert dash.agents == {DashboardClient.USER_AGENT}
    assert m["id"] in {r[0] for r in st._db.execute("SELECT scan_id FROM forwards")}


def test_start_after_kept_across_restarts(dash, scans, make_state):
    st = make_state()
    start = st.start_after
    st.close()
    st2 = make_state(send_existing=True)              # 두 번째 실행에서는 설정이 바뀌어도 기준 유지
    assert not st2.first_run and st2.start_after == start


def test_send_existing(dash, scans, make_state):
    add(scans, "OLD-1", received_at=OLD)
    st = make_state(send_existing=True)
    assert st.start_after == ""
    assert drain(Forwarder(st, DashboardClient(dash.url, 3))) == ["done", "idle"]
    assert dash.stock("OLD-1") == 1


def test_device_id_override_and_types(dash, scans, make_state):
    st = make_state(types=("CODE128", "EAN13"))
    add(scans, "Apple123")
    add(scans, "8801234567893", type_="EAN13")
    drain(Forwarder(st, DashboardClient(dash.url, 3), device_id="rpi-01"))
    assert dash.tx["Apple123"][0]["deviceId"] == "rpi-01"
    assert dash.stock("8801234567893") == 1


def test_unsendable_barcode_skipped(dash, scans, make_state):
    st = make_state()
    add(scans, "A/1")
    add(scans, "Apple123")
    assert drain(Forwarder(st, DashboardClient(dash.url, 3))) == ["done", "done", "idle"]
    assert dash.posts == 1
    assert states(st) == [("A/1", SKIPPED), ("Apple123", SENT)]


def test_rejected_not_retried(dash, scans, make_state):
    st = make_state()
    add(scans, "Apple123")
    add(scans, "pants2")
    dash.mode, dash.once = "reject", True
    assert drain(Forwarder(st, DashboardClient(dash.url, 3))) == ["done", "done", "idle"]
    assert states(st) == [("Apple123", FAILED), ("pants2", SENT)]
    assert "400" in st.recent()[-1]["error"]


# ---------------------------------------------------------------- 장애: 재고가 두 번 오르지 않는지
def test_server_down_then_recovers(dash, scans, make_state):
    st = make_state()
    add(scans, "Apple123")
    add(scans, "pants2")
    fwd = Forwarder(st, DashboardClient(dash.url, 3))
    dash.mode = "down"
    assert fwd.step() == "retry" and fwd.step() == "retry"
    assert st.summary()[SENDING] == 1                 # 다음 건으로 넘어가지 않음
    assert fwd._backoff == 4.0
    dash.mode = None
    assert drain(fwd) == ["done", "done", "idle"]
    assert dash.stock("Apple123") == 1 and dash.stock("pants2") == 1
    assert [t["barcode"] for b in ("Apple123", "pants2") for t in dash.tx[b]] == ["Apple123", "pants2"]
    assert fwd._backoff == 0.0
    assert st.recent()[-1]["attempts"] == 3           # 503 두 번 + 성공


def test_connection_dropped_after_commit(dash, scans, make_state):
    """서버는 기록했는데 응답을 못 받은 경우 → 이력을 확인하고 다시 보내지 않는다."""
    st = make_state()
    add(scans, "Apple123")
    fwd = Forwarder(st, DashboardClient(dash.url, 3))
    dash.mode, dash.once = "drop", True
    assert fwd.step() == "retry"
    assert drain(fwd) == ["done", "idle"]
    assert dash.posts == 1 and dash.stock("Apple123") == 1
    row = st.recent()[0]
    assert row["state"] == SENT and row["transaction_id"] == "tx1" and row["attempts"] == 1


def test_timeout_after_commit(dash, scans, make_state):
    st = make_state()
    add(scans, "pants2")
    fwd = Forwarder(st, DashboardClient(dash.url, 0.5))
    dash.mode, dash.once, dash.delay = "slow", True, 1.5
    assert fwd.step() == "retry"
    assert "연결 오류" in st.recent()[0]["error"]
    assert drain(fwd) == ["done", "idle"]
    assert dash.stock("pants2") == 1


def test_history_check_fails_then_recovers(dash, scans, make_state):
    """결과를 모르는 건은 이력 확인이 될 때까지 다시 보내지 않는다."""
    st = make_state()
    add(scans, "Apple123")
    fwd = Forwarder(st, DashboardClient(dash.url, 3))
    dash.mode, dash.once = "drop", True
    assert fwd.step() == "retry"
    dash.mode, dash.once = "list_down", False
    assert fwd.step() == "retry" and fwd.step() == "retry"
    assert dash.posts == 1
    dash.mode = None
    assert drain(fwd) == ["done", "idle"]
    assert dash.stock("Apple123") == 1


def test_restart_with_sending_left(dash, scans, make_state):
    """'sending' 기록 직후 프로그램이 멈춘 경우: 서버에 없으면 보내고, 있으면 보내지 않는다."""
    st = make_state()
    a = add(scans, "Apple123")
    b = add(scans, "pants2")
    for m in (a, b):
        st.mark_sending(m["id"], m["data"], "dock-01", to_api_timestamp(
            scans.scans(ids=[m["id"]])[0]["scanned_at"]))
    # pants2는 서버까지 갔던 것으로 만든다
    DashboardClient(dash.url, 3).post_scan("pants2", "dock-01", "2026-10-16T10:54:00.015Z")
    st.close()

    st2 = make_state()
    assert drain(Forwarder(st2, DashboardClient(dash.url, 3))) == ["done", "done", "idle"]
    assert dash.stock("Apple123") == 1 and dash.stock("pants2") == 1
    assert dash.posts == 2


def test_new_scans_while_running(dash, scans, make_state):
    st = make_state()
    fwd = Forwarder(st, DashboardClient(dash.url, 3), poll=0.05)
    stop = threading.Event()
    t = threading.Thread(target=fwd.run, args=(stop,), daemon=True)
    t.start()
    try:
        for name in ("Apple123", "pants2", "Apple123"):
            add(scans, name)
        deadline = time.time() + 5
        while dash.posts < 3 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        t.join(3)
    assert not t.is_alive()
    assert dash.stock("Apple123") == 2 and dash.stock("pants2") == 1


# ---------------------------------------------------------------- 실행 프로그램
def test_app_once_and_status(dash, scans, tmp_path, capsys):
    ini = tmp_path / "forwarder.ini"
    ini.write_text(f"[api]\nurl = {dash.url}\ntimeout = 3\n[forward]\nsend_existing = true\n"
                   f"[db]\npath = {scans.path}\nstate = {tmp_path / 'fw.db'}\n", encoding="utf-8")
    add(scans, "Apple123")
    add(scans, "B/2")
    assert forwarder_app.main(["--config", str(ini), "--once"]) == 0
    assert dash.stock("Apple123") == 1

    assert forwarder_app.main(["--config", str(ini), "--status"]) == 0
    out = capsys.readouterr().out
    assert "완료 1" in out and "제외 1" in out and "tx1" in out

    dash.mode = "down"
    add(scans, "pants2")
    assert forwarder_app.main(["--config", str(ini), "--once"]) == 2   # 오류로 끝남


def test_app_missing_source_db(tmp_path):
    assert forwarder_app.main(["--db", str(tmp_path / "none.db"), "--state", str(tmp_path / "f.db"),
                               "--once"]) == 1


def test_app_config_errors(tmp_path):
    with pytest.raises(SystemExit):
        forwarder_app.load_config(["--api-url", "api.oswdev.cloud"])
    with pytest.raises(SystemExit):
        forwarder_app.load_config(["--types", " , "])
    cfg = forwarder_app.load_config(["--types", "code128, ean13"])
    assert cfg.forward_types == ("CODE128", "EAN13")
    assert cfg.api_url == "https://api.oswdev.cloud" and cfg.forward_send_existing is False


def test_example_ini_matches_defaults():
    from conftest import ROOT
    cfg = forwarder_app.load_config(["--config", str(ROOT / "config" / "forwarder.example.ini")])
    assert cfg.api_url == "https://api.oswdev.cloud"
    assert cfg.forward_types == ("CODE128",) and cfg.forward_device_id is None
    assert cfg.forward_send_existing is False
    assert (cfg.db_path, cfg.db_state) == ("data/barcodes.db", "data/forwarder.db")
