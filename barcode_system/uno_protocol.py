"""
파이(브리지) ↔ 아두이노 UNO 시리얼 프로토콜 (UNO 쪽: arduino/uno_display/protocol.h).

한 줄 = 프레임 1개, ASCII:  <종류>|<필드>|<필드>...*<체크섬 16진 2자리>\n
  체크섬 = '*' 앞 모든 바이트의 XOR.  예) P|7*4F
  줄 길이(줄바꿈 제외)는 최대 63바이트 — UNO 수신 버퍼가 64바이트(끝의 NUL 포함)이기 때문.

파이 → UNO  (n = 브리지가 붙이는 1~65535 일련번호, UNO가 A|n으로 확인)
  S|n|<type>|<scanner>|<data>   DB 저장됨
  X|n|<scanner>|<reason>        거부됨
  T|n|<scanner>|<state>         스캐너 상태 (online/offline/lost)
  P|n                           연결 확인
UNO → 파이
  R|uno-display|<버전>           부팅 완료 (시리얼 포트를 열면 UNO가 리셋된다)
  A|n                           수신 확인
  E|<코드>                      오류 알림 (CK: 체크섬, FMT: 형식, LEN: 줄이 너무 김)

필드에는 출력 가능한 ASCII(0x20~0x7E)만 쓰며 '|', '*'와 그 밖의 문자는 '?'로 바꾼다
(LCD도 ASCII만 표시할 수 있다). 줄이 길면 마지막 필드(바코드 값 등)를 자른다.
"""
LINE_MAX = 63                 # 줄바꿈 제외 최대 길이
MAX_FIELDS = 6
BODY_MAX = LINE_MAX - 3       # '*HH' 3바이트
FIRMWARE_NAME = "uno-display"
PROTOCOL_VERSION = "1"


def sanitize(value):
    return "".join(c if " " <= c <= "~" and c not in "|*" else "?" for c in str(value))


def checksum(body):
    if isinstance(body, str):
        body = body.encode("ascii")
    c = 0
    for b in body:
        c ^= b
    return c


def encode(*fields):
    """필드들로 한 줄(bytes, 줄바꿈 포함)을 만든다. 너무 길면 마지막 필드를 자른다."""
    if not 1 <= len(fields) <= MAX_FIELDS:
        raise ValueError(f"필드 수는 1~{MAX_FIELDS}개: {len(fields)}")
    parts = [sanitize(f) for f in fields]
    if len(parts[0]) != 1:
        raise ValueError(f"종류는 한 글자: {parts[0]!r}")
    excess = len("|".join(parts)) - BODY_MAX
    if excess > 0:
        if len(parts) == 1 or len(parts[-1]) < excess:
            raise ValueError("고정 필드만으로 줄 길이를 넘습니다")
        parts[-1] = parts[-1][:len(parts[-1]) - excess]
    body = "|".join(parts)
    return f"{body}*{checksum(body):02X}\n".encode("ascii")


def decode(line):
    """한 줄 → 필드 목록. 형식·체크섬이 틀리면 ValueError."""
    if isinstance(line, (bytes, bytearray)):
        try:
            line = line.decode("ascii")
        except UnicodeDecodeError as e:
            raise ValueError("ASCII가 아닌 바이트") from e
    line = line.rstrip("\r\n")
    body, star, ck = line.rpartition("*")
    if not star or not body or len(ck) != 2:
        raise ValueError(f"형식 오류: {line!r}")
    try:
        expected = int(ck, 16)
    except ValueError:
        raise ValueError(f"체크섬 형식 오류: {line!r}") from None
    if checksum(body) != expected:
        raise ValueError(f"체크섬 불일치: {line!r}")
    fields = body.split("|")
    if len(fields) > MAX_FIELDS or len(fields[0]) != 1:
        raise ValueError(f"필드 오류: {line!r}")
    return fields


# ---------------------------------------------------------------- 프레임 생성 (파이 → UNO)
def saved(n, btype, scanner, data):
    return encode("S", n, btype, scanner, data)


def rejected(n, scanner, reason):
    return encode("X", n, scanner or "?", reason)


def status(n, scanner, state):
    return encode("T", n, scanner, state)


def ping(n):
    return encode("P", n)
