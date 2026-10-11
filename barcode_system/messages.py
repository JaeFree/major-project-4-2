"""
MQTT 토픽과 메시지(JSON) 형식.

토픽
  warehouse/scanner/{scanner_id}/scan    스캔 이벤트   QoS 1, retain X
  warehouse/scanner/{scanner_id}/status  상태          QoS 1, retain O

  warehouse/db/result                    DB 저장 결과 QoS 1, retain X (3단계, db-writer가 발행)

scan   {"v":1, "id":uuid, "scanner_id", "seq", "ts", "type", "data"}
status {"v":1, "scanner_id", "state": online|offline|lost, "ts"}
  online : 연결됨 / offline : 정상 종료 / lost : 비정상 끊김 (브로커가 LWT로 대신 발행)
result {"v":1, "result": inserted|duplicate|conflict, "ts", "id", "scanner_id", "seq", "type", "data"}
       {"v":1, "result": rejected, "ts", "topic", "reason", "scanner_id"(토픽에서 알 수 있으면)}
  ts는 db-writer가 처리한 시각. 표시 장치(UNO)는 오래된 결과를 버리는 데 쓴다.
"""
import json
import re
import uuid
from datetime import datetime

SCHEMA_VERSION = 1
TOPIC_ROOT = "warehouse/scanner"
RESULT_TOPIC = "warehouse/db/result"
STATUS_STATES = ("online", "offline", "lost")
RESULTS = ("inserted", "duplicate", "conflict", "rejected")

# scanner_id는 토픽 단계와 MQTT 사용자 이름으로 쓰이므로 와일드카드(+, #)나 '/'가 들어가면 안 된다
_SCANNER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DATA_LEN = 4096


def validate_scanner_id(scanner_id):
    if not isinstance(scanner_id, str) or not _SCANNER_ID_RE.match(scanner_id):
        raise ValueError(f"scanner_id는 영문/숫자/_/- 1~64자여야 합니다: {scanner_id!r}")
    return scanner_id


def scan_topic(scanner_id):
    return f"{TOPIC_ROOT}/{validate_scanner_id(scanner_id)}/scan"


def status_topic(scanner_id):
    return f"{TOPIC_ROOT}/{validate_scanner_id(scanner_id)}/status"


def now_iso():
    """밀리초와 시간대(+09:00 등)까지 포함한 현재 시각.
    ※ 라즈베리파이4는 RTC가 없어 NTP 동기화 전에는 시각이 틀릴 수 있다."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_id():
    return str(uuid.uuid4())


def build_scan(scanner_id, seq, btype, data, msg_id=None, ts=None):
    return {
        "v": SCHEMA_VERSION,
        "id": msg_id or new_id(),
        "scanner_id": validate_scanner_id(scanner_id),
        "seq": int(seq),
        "ts": ts or now_iso(),
        "type": str(btype),
        "data": str(data),
    }


def build_status(scanner_id, state, ts=None):
    if state not in STATUS_STATES:
        raise ValueError(f"알 수 없는 상태: {state}")
    return {"v": SCHEMA_VERSION, "scanner_id": validate_scanner_id(scanner_id),
            "state": state, "ts": ts or now_iso()}


def encode(msg):
    # ensure_ascii=False: 한글 등이 \uXXXX로 늘어나지 않게. 공백 없는 구분자로 크기를 줄인다
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _load(payload):
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    msg = json.loads(payload)
    if not isinstance(msg, dict):
        raise ValueError("JSON 객체가 아닙니다")
    if msg.get("v") != SCHEMA_VERSION:
        raise ValueError(f"지원하지 않는 메시지 버전: {msg.get('v')!r}")
    return msg


def parse_scan(payload):
    """scan 메시지를 해석·검증한다. 형식이 틀리면 ValueError (json 오류·인코딩 오류 포함)."""
    msg = _load(payload)
    try:
        uuid.UUID(msg["id"])
        validate_scanner_id(msg["scanner_id"])
        if not isinstance(msg["seq"], int) or isinstance(msg["seq"], bool) or msg["seq"] < 1:
            raise ValueError("seq")
        datetime.fromisoformat(msg["ts"])
        for key in ("type", "data"):
            if not isinstance(msg[key], str) or not msg[key]:
                raise ValueError(key)
        if len(msg["data"]) > MAX_DATA_LEN:
            raise ValueError("data 길이 초과")
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise ValueError(f"잘못된 scan 메시지 ({e})") from e
    return msg


def build_result(result, scan=None, topic=None, reason=None, scanner_id=None, ts=None):
    """DB 저장 결과. scan 결과면 scan 메시지(dict)를, rejected면 topic·reason을 넣는다."""
    if result not in RESULTS:
        raise ValueError(f"알 수 없는 결과: {result}")
    msg = {"v": SCHEMA_VERSION, "result": result, "ts": ts or now_iso()}
    if result == "rejected":
        msg.update(topic=str(topic), reason=str(reason)[:200])
        if scanner_id:
            msg["scanner_id"] = scanner_id
    else:
        msg.update({k: scan[k] for k in ("id", "scanner_id", "seq", "type", "data")})
    return msg


def parse_result(payload):
    msg = _load(payload)
    try:
        if msg["result"] not in RESULTS:
            raise ValueError("result")
        datetime.fromisoformat(msg["ts"])
        if msg["result"] == "rejected":
            if not isinstance(msg["reason"], str):
                raise ValueError("reason")
            if "scanner_id" in msg:
                validate_scanner_id(msg["scanner_id"])
        else:
            validate_scanner_id(msg["scanner_id"])
            for key in ("type", "data"):
                if not isinstance(msg[key], str):
                    raise ValueError(key)
            if not isinstance(msg["seq"], int):
                raise ValueError("seq")
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"잘못된 result 메시지 ({e})") from e
    return msg


def parse_status(payload):
    msg = _load(payload)
    try:
        validate_scanner_id(msg["scanner_id"])
        if msg["state"] not in STATUS_STATES:
            raise ValueError("state")
        datetime.fromisoformat(msg["ts"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"잘못된 status 메시지 ({e})") from e
    return msg
