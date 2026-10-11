"""
설계서(.docx) 생성 스크립트 — 단계가 끝날 때마다 내용을 추가하고 다시 실행한다.

  .venv\\Scripts\\python docs\\build_design_doc.py        → docs/설계서.docx
"""
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

OUT = Path(__file__).resolve().parent / "설계서.docx"
FONT = "맑은 고딕"
MONO = "Consolas"
DOC_VERSION = "1단계 완료판 (2026-10-11)"


# ================================================================ 서식 도우미
def set_font(run_or_style, name, size=None):
    font = run_or_style.font
    font.name = name
    rpr = run_or_style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), name)
    if size:
        font.size = Pt(size)


def setup_styles(doc):
    set_font(doc.styles["Normal"], FONT, 10)
    for i, size in ((1, 16), (2, 13), (3, 11)):
        style = doc.styles[f"Heading {i}"]
        set_font(style, FONT, size)
        style.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
    set_font(doc.styles["Title"], FONT, 24)


def para(doc, text, bold=False, style=None):
    p = doc.add_paragraph(style=style)
    run = p.add_run(text)
    run.bold = bold
    return p


def bullets(doc, items):
    for item in items:
        p = doc.add_paragraph(style="List Bullet")
        if isinstance(item, tuple):  # (굵은 머리말, 설명)
            p.add_run(item[0]).bold = True
            p.add_run(" — " + item[1])
        else:
            p.add_run(item)


def numbered(doc, items):
    for item in items:
        doc.add_paragraph(item, style="List Number")


def code(doc, text):
    for line in text.strip("\n").splitlines():
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.left_indent = Pt(12)
        set_font(p.add_run(line if line else " "), MONO, 8.5)
    doc.add_paragraph()


def table(doc, header, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for cell, text in zip(t.rows[0].cells, header):
        cell.text = ""
        cell.paragraphs[0].add_run(text).bold = True
    for row in rows:
        cells = t.add_row().cells
        for cell, text in zip(cells, row):
            cell.text = str(text)
    for row in t.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                for r in p.runs:
                    set_font(r, FONT, 9)
    if widths:
        for row in t.rows:
            for cell, w in zip(row.cells, widths):
                cell.width = Pt(w)
    doc.add_paragraph()
    return t


# ================================================================ 내용
def overview(doc):
    doc.add_heading("1. 개요", 1)
    para(doc, "목표: 바코드 인식 → 라즈베리파이4(MQTT 브로커) → 데이터베이스 및 아두이노 UNO")
    para(doc, "물류창고에서 카메라로 1D 바코드(상품 EAN-13, 위치·팔레트 Code128 등)를 인식하고, "
              "MQTT로 중앙(라즈베리파이4)에 전달해 데이터베이스에 기록하며, 아두이노 UNO로 현장 표시·알림을 한다.")
    doc.add_heading("1.1 전체 구성", 2)
    code(doc, """
[카메라] ─► 스캐너(Python, 라즈베리파이) ─► outbox(SQLite) ─► MQTT 발행 (QoS 1)
                                                                  │
                                                     Mosquitto 브로커 (라즈베리파이4)
                                                                  │
                         ┌────────────────────────────────────────┴──────────────┐
                (2단계) DB 저장 서비스 ─► 데이터베이스        (3단계) 시리얼 브리지 ─► 아두이노 UNO
""")
    doc.add_heading("1.2 단계별 진행 현황", 2)
    table(doc, ["단계", "내용", "상태"], [
        ["1단계", "바코드 인식 → MQTT 발행", "완료 (Windows 로컬 검증. 파이·카메라 실측은 미검증)"],
        ["2단계", "MQTT 구독 → DB 저장", "예정"],
        ["3단계", "아두이노 UNO 연동", "예정"],
    ], widths=[50, 170, 230])


def environment(doc):
    doc.add_heading("2. 개발·실행 환경", 1)
    table(doc, ["구분", "내용"], [
        ["Windows 작업 PC", "코드 작성, 단위·통합 테스트, 로컬 Mosquitto 테스트, 설계서 생성. "
                            "Python 3.11.9 (D:\\Tools\\Python311), venv(.venv), requirements-dev.txt"],
        ["라즈베리파이4", "실제 실행 환경. OS 미정 → Bookworm(Python 3.11) 기준으로 작성. "
                         "apt: picamera2·libzbar0·mosquitto / pip(venv --system-site-packages): requirements-pi.txt"],
        ["아두이노 UNO", "Arduino C++ 스케치. arduino-cli 1.5.1 + arduino:avr 1.8.8 (3단계)"],
        ["로컬 브로커", "Mosquitto 2.1.2 (D:\\Tools\\mosquitto). Windows 서비스는 수동 시작으로 두고, "
                       "테스트가 임시 포트로 직접 실행·종료한다"],
        ["버전 관리", "Git + GitHub 비공개 저장소 JaeFree/major-project-4-2"],
    ], widths=[90, 360])
    para(doc, "버전 고정 이유: Bookworm의 apt picamera2는 numpy 1.x로 빌드되어 있어 venv에서 numpy<2를 유지해야 한다. "
              "opencv-python 4.12부터 numpy>=2가 필요하므로 OpenCV는 4.11.0.86으로 고정했다. "
              "또 Bookworm의 apt OpenCV(4.6)에는 cv2.barcode가 없어 파이에서도 pip의 opencv-python-headless를 쓴다.")


def stage1(doc):
    doc.add_heading("3. 1단계: 바코드 인식 → MQTT 발행", 1)

    doc.add_heading("3.1 구현한 기능", 2)
    bullets(doc, [
        ("바코드 인식", "OpenCV BarcodeDetector와 ZBar(pyzbar)를 매 프레임 함께 실행해 결과를 합친다. "
                       "종류 표기는 ZBar 방식(EAN13, CODE128 …)으로 통일"),
        ("중복 발행 방지", "ScanTracker — interval(계속 보이면 cooldown마다) / presence(등장마다 1회), "
                          "confirm(연속 인식 횟수)으로 오인식 차단"),
        ("오프라인 우선 전송", "스캔을 디스크 outbox(SQLite)에 먼저 기록한 뒤 발행. 브로커·네트워크가 끊겨도 "
                              "유실 없이 재연결 시 순서대로 전송"),
        ("멱등성", "메시지마다 uuid(id)와 스캐너별 일련번호(seq). 재전송으로 중복이 생겨도 받는 쪽에서 한 번만 반영"),
        ("상태 알림", "status 토픽(retain): online / offline(정상 종료) / lost(비정상 끊김, 브로커가 LWT로 발행)"),
        ("브로커 보안", "사용자 인증, 토픽 ACL(스캐너는 자기 토픽에만 쓰기), 영속성 설정"),
        ("paho-mqtt 1.x/2.x 호환", "콜백 형식 차이를 mqtt_compat 계층에서 흡수 (둘 다 테스트)"),
        ("카메라", "Picamera2 / USB 웹캠(OS별 백엔드: Linux V4L2, Windows DirectShow) / 이미지 폴더(시연·테스트)"),
        ("코드 재사용", "기존 barcode_scanner.py의 메인 루프를 ScanLoop로 분리. 기존 단독 실행판(CSV+SQLite)도 유지"),
    ])

    doc.add_heading("3.2 구성 모듈", 2)
    table(doc, ["파일", "역할"], [
        ["barcode_system/camera.py", "Camera(Picamera2·USB), ImageFolderCamera, OS별 백엔드 선택"],
        ["barcode_system/decoding.py", "프레임 검증, BarcodeReader(OpenCV+ZBar), 표기 통일(UPC-A→EAN13), 그리기"],
        ["barcode_system/tracker.py", "ScanTracker (중복 판단)"],
        ["barcode_system/scan_loop.py", "ScanLoop: 카메라→인식→중복 판단→on_scan 콜백, 카메라 자동 재연결, 미리보기"],
        ["barcode_system/messages.py", "토픽 이름, 메시지 생성·검증(JSON)"],
        ["barcode_system/outbox.py", "SQLite outbox (기록, 미전송 조회, 전송 완료 표시, 보관 기간 정리)"],
        ["barcode_system/mqtt_compat.py", "paho-mqtt 1.x/2.x 호환 계층"],
        ["barcode_system/publisher.py", "발행 스레드: 재연결, inflight 제한, PUBACK 처리, 재발행, status/LWT"],
        ["barcode_system/scanner_app.py", "실행 진입점, 설정(INI + 명령줄) 처리"],
        ["barcode_scanner.py", "기존 단독 실행판 (CSV+SQLite). 위 모듈을 공유"],
        ["config/scanner.example.ini", "설정 예시"],
        ["mosquitto/pi/barcode.conf, acl, setup_broker.sh", "파이 브로커 설정·설치 스크립트"],
        ["tests/*, scripts/test_paho1.ps1", "테스트 (3.8 참조)"],
    ], widths=[170, 280])

    doc.add_heading("3.3 동작 방식", 2)
    numbered(doc, [
        "ScanLoop가 카메라 프레임을 받아 형식을 검증하고(validate_frame) 두 엔진으로 바코드를 찾는다.",
        "ScanTracker가 '지금 발행할지' 판단한다. 시간 간격은 time.monotonic()으로 계산해 시스템 시계 변경(NTP)에 영향받지 않는다.",
        "발행 대상이면 outbox에 한 트랜잭션으로 기록한다(seq 발급 → uuid·시각을 담은 JSON 작성). "
        "WAL + synchronous=FULL이라 commit 후에는 전원이 꺼져도 남는다.",
        "발행 스레드가 outbox의 미전송 메시지를 seq 순서로 QoS 1 발행한다. 연결이 끊겨 있으면 발행하지 않고 기다린다.",
        "브로커의 PUBACK을 받으면 outbox에 sent_at을 기록한다. 응답 대기 메시지는 최대 20개(inflight)로 제한한다.",
        "30초 안에 PUBACK이 없으면 같은 메시지(같은 id)를 다시 발행한다. 재연결 후에는 paho가 스스로 재전송할 수 있어 기한을 새로 준다.",
        "연결·재연결은 paho 네트워크 스레드가 1초→최대 30초 간격으로 재시도한다. 첫 연결이 실패해도 스캔은 계속된다.",
        "정상 종료 시 남은 메시지를 최대 2초 동안 보내고 offline 상태를 남긴다. 남은 메시지는 다음 실행 때 전송된다.",
        "전송 완료된 기록은 7일(설정 가능) 뒤 정리한다. 미전송 기록은 지우지 않는다.",
    ])
    para(doc, "전달 보장: at-least-once(최소 한 번). 중복은 id로 제거한다(2단계 DB에서 id를 UNIQUE로 저장 예정).", bold=True)
    para(doc, "paho 콜백은 이벤트 큐에 넣기만 하고 실제 처리는 발행 스레드 하나가 한다. "
              "inflight 상태를 스레드 하나만 다루므로 잠금이 필요 없고, publish()가 반환되기 전에 PUBACK이 먼저 오는 경우에도 안전하다.")

    doc.add_heading("3.4 토픽과 메시지 형식", 2)
    table(doc, ["토픽", "방향", "QoS", "retain", "내용"], [
        ["warehouse/scanner/{scanner_id}/scan", "스캐너→브로커", "1", "X", "스캔 이벤트"],
        ["warehouse/scanner/{scanner_id}/status", "스캐너→브로커", "1", "O", "online / offline / lost"],
    ], widths=[170, 70, 30, 40, 140])
    para(doc, "scanner_id는 영문·숫자·_·- 1~64자이며 MQTT 사용자 이름과 같게 둔다(ACL 규칙). "
              "창고가 여러 곳이 되면 warehouse/{site}/scanner/... 로 단계를 추가할 수 있다.")
    code(doc, """
scan   {"v":1,"id":"3f2b9c1e-...","scanner_id":"dock-01","seq":1532,
        "ts":"2026-10-11T20:41:03.123+09:00","type":"EAN13","data":"8801234567893"}
status {"v":1,"scanner_id":"dock-01","state":"online","ts":"2026-10-11T20:40:00.000+09:00"}
""")
    table(doc, ["필드", "설명"], [
        ["v", "메시지 형식 버전 (현재 1)"],
        ["id", "uuid4. outbox 기록 시 생성되고 재전송해도 바뀌지 않는다 → 중복 제거 키"],
        ["seq", "스캐너별 일련번호(1부터, 재시작해도 이어짐) → 2단계에서 누락 확인"],
        ["ts", "스캔 시각(밀리초·시간대 포함). 파이는 RTC가 없어 NTP 동기화 전엔 틀릴 수 있음 → 2단계에서 수신 시각도 저장"],
        ["type / data", "바코드 종류(ZBar 표기) / 값(UTF-8, 최대 4096자)"],
    ], widths=[70, 380])

    doc.add_heading("3.5 브로커(Mosquitto) 설정", 2)
    bullets(doc, [
        ("인증", "allow_anonymous false, password_file. 사용자는 setup_broker.sh가 비밀번호를 입력받아 등록"),
        ("영속성", "persistence true(데비안 기본), autosave_interval 60초, max_queued_messages 100000"),
        ("크기 제한", "message_size_limit 64KB"),
        ("TLS", "이번 단계 미적용 (내부망 전제). 필요 시 8883 리스너로 추가"),
    ])
    table(doc, ["사용자", "권한"], [
        ["스캐너 (dock-01 등)", "warehouse/scanner/<자기 이름>/scan, /status 쓰기 (pattern %u)"],
        ["db-writer (2단계)", "warehouse/scanner/+/scan, +/status 읽기"],
        ["monitor", "warehouse/# 읽기 (점검용)"],
    ], widths=[120, 330])

    doc.add_heading("3.6 설정과 실행", 2)
    code(doc, r"""
# 설정: config/scanner.example.ini 를 config/scanner.ini 로 복사해 수정 (명령줄 옵션이 우선)
python -m barcode_system.scanner_app --config config/scanner.ini
# 웹캠 없이 시연: 이미지 폴더를 카메라처럼 사용
python -m barcode_system.scanner_app --id dock-01 --camera files --images samples --host 127.0.0.1
# 비밀번호: [mqtt] password_file 의 첫 줄 또는 환경 변수 BARCODE_MQTT_PASSWORD (명령줄에는 쓰지 않음)
# 종료: Ctrl+C, systemd stop(SIGTERM), Windows Ctrl+Break → 정상 종료(남은 메시지 전송, offline 발행)
""")

    doc.add_heading("3.7 사용 기술", 2)
    table(doc, ["기술", "용도"], [
        ["Python 3.11", "스캐너 전체"],
        ["OpenCV 4.11 (cv2.barcode)", "EAN/UPC 계열 위치 검출·해독, 영상 처리"],
        ["ZBar (pyzbar 0.1.9)", "Code128, Code39, I25, Codabar 등 1D 바코드 해독"],
        ["Picamera2 / V4L2 / DirectShow", "카메라 입력"],
        ["SQLite (WAL, synchronous=FULL)", "outbox — 전원 차단에도 기록 보존"],
        ["MQTT 3.1.1, QoS 1, retain, LWT", "메시지 전달, 상태 알림"],
        ["Eclipse Mosquitto 2.x", "브로커 (인증, ACL, 영속성)"],
        ["paho-mqtt 1.6.1 / 2.1", "MQTT 클라이언트 (두 버전 호환)"],
        ["pytest, python-barcode", "테스트, 합성 바코드 이미지 생성"],
    ], widths=[170, 280])

    doc.add_heading("3.8 테스트 결과 (Windows, 2026-10-11)", 2)
    para(doc, "전체 112개 통과 (paho-mqtt 2.1.0). MQTT 관련 29개는 paho-mqtt 1.6.1에서도 통과.", bold=True)
    table(doc, ["테스트 파일", "개수", "확인 내용"], [
        ["test_env.py", "4", "패키지, OpenCV·ZBar 인식, 로컬 브로커 왕복"],
        ["test_tracker.py", "10", "interval/presence/confirm, cooldown=0 회귀, 이력 정리"],
        ["test_decoding.py", "32", "EAN13·UPC-A·Code128·Code39, 한 화면 2개, 인식률, 프레임 검증, 좌표 정규화"],
        ["test_camera.py", "8", "OS별 백엔드 선택, 이미지 폴더 카메라"],
        ["test_messages.py", "26", "토픽, 메시지 생성·검증, 잘못된 입력 거부"],
        ["test_outbox.py", "7", "순서, 재시작 후 보존, seq 재사용 없음, 정리, 롤백, 동시 기록"],
        ["test_publisher.py", "9", "inflight 제한, PUBACK 시간 초과 재발행, 끊김 중 발행 중단, "
                                   "네트워크 단절 23건 유실 0, 브로커 재시작·브로커 없이 시작, LWT"],
        ["test_broker_security.py", "5", "익명·틀린 비밀번호 거부, ACL(사칭·읽기 전용 쓰기 차단), 인증 상태에서 LWT"],
        ["test_scanner_app.py", "11", "설정 우선순위·오류, 실제 프로세스 end-to-end, 강제 종료→재시작 유실 0, "
                                     "정상 종료, 기존 단독 실행판 동작"],
    ], widths=[120, 35, 295])
    para(doc, "주요 시나리오 결과")
    bullets(doc, [
        "네트워크 단절(TCP 프록시로 스캐너 연결만 차단) 중 20건 → 단절 동안 outbox에 20건 보관, 복구 후 23건 모두 도착(고유 id 기준).",
        "스캐너 프로세스 강제 종료 → 브로커가 lost 발행 → 재시작 시 남은 메시지 전송, seq 1..N 빈 번호 없음, 수신 누락 0.",
        "합성 EAN-13 인식률(40개): ZBar 40/40, OpenCV 단독 최대 36/40 → 두 엔진 동시 실행(auto)이 필요함을 확인.",
    ])

    doc.add_heading("3.9 발견한 오류와 수정", 2)
    table(doc, ["오류", "원인", "수정"], [
        ["auto 모드에서 UPC-A 바코드가 값이 다른 두 건으로 저장·발행됨 (기존 코드)",
         "OpenCV는 12자리 UPCA, ZBar는 앞에 0을 붙인 13자리 EAN13으로 보고 → 값이 달라 중복 제거 실패",
         "canonical(): 12자리 UPCA를 0+값, EAN13(GTIN-13)으로 통일. 회귀 테스트 추가"],
        ["presence 모드에서 cooldown=0이면 매 프레임 저장 (기존 코드)",
         "연속 프레임 사이에도 시간 간격이 있어 '0초 이상 안 보임' 조건이 항상 참",
         "'사라짐' 기준을 max(cooldown, 0.5초)로. 회귀 테스트 추가"],
        ["Windows에서 --show 미리보기가 항상 꺼짐 (기존 코드)",
         "디스플레이 확인이 DISPLAY 환경 변수(리눅스 전용)만 봄",
         "Linux에서만 DISPLAY/WAYLAND_DISPLAY 확인"],
        ["Windows에서 USB 카메라를 열 수 없음 (알려진 문제)",
         "cv2.CAP_V4L2 고정",
         "OS별 백엔드 자동 선택(--backend로 변경 가능)"],
        ["미리보기를 켜면 원본 프레임에 외곽선이 그려짐",
         "BGR 프레임은 to_bgr()가 같은 배열을 돌려줌 → 이미지 폴더 카메라처럼 프레임을 재사용하면 다음 인식에 영향",
         "그리기 전에 복사"],
        ["합성 이미지 테스트에서 OpenCV가 인식 실패",
         "OpenCV 검출기는 밝기 기울기로 위치를 찾아 경계가 완전히 날카로운 이미지를 놓침",
         "테스트 이미지에 여백·약한 흐림 적용(camera_like). 인식률 테스트로 특성 기록"],
        ["paho 1.x 테스트 스크립트(PowerShell) 실행 시 구문 오류",
         "Windows PowerShell 5.1은 BOM 없는 UTF-8을 CP949로 읽어 한글 문자열이 깨짐",
         "스크립트를 UTF-8(BOM)으로 저장"],
    ], widths=[140, 160, 150])

    doc.add_heading("3.10 미검증 항목 (하드웨어 없음)", 2)
    bullets(doc, [
        "실제 카메라(Pi 카메라 모듈, USB 웹캠)로 실물 바코드 인식 — 초점·조명·거리·움직임에 따른 인식률",
        "라즈베리파이4에서의 설치(requirements-pi.txt, apt), 실행 속도(FPS), CPU 사용률",
        "파이의 Mosquitto 설치 스크립트(setup_broker.sh)와 설정 — Windows의 Mosquitto 2.1.2로 같은 ACL만 검증",
        "실제 네트워크(Wi-Fi) 단절·전원 차단 상황 — 로컬 TCP 프록시와 프로세스 강제 종료로 흉내 냄",
        "UPC-E, I25, Codabar, DataBar — 생성기가 없어 합성 이미지 테스트를 하지 못함",
    ])

    doc.add_heading("3.11 알려진 제약과 이후 과제", 2)
    bullets(doc, [
        "MQTT 3.1.1에서는 ACL로 거부된 발행도 PUBACK을 받아 스캐너는 전송 성공으로 기록한다. "
        "사용자 이름과 scanner_id가 다르면 경고를 출력한다.",
        "ACL의 pattern 규칙은 모든 사용자에 적용되어 db-writer·monitor도 warehouse/scanner/<자기 이름>/... 에 쓸 수 있다. "
        "2단계 구독자는 토픽의 scanner_id와 메시지의 scanner_id가 같은지 확인한다.",
        "브로커가 비정상 종료되면 마지막 자동 저장(60초) 이후의 브로커 내부 큐를 잃을 수 있다. "
        "스캐너 쪽은 PUBACK 기준이라 영향이 없지만, 구독자가 오래 끊겨 있을 때는 영향이 있으므로 2단계에서 검토한다.",
        "시각(ts)은 파이 시계 기준이다. NTP 동기화 전 스캔은 시각이 틀릴 수 있다.",
    ])


def next_steps(doc):
    doc.add_heading("4. 다음 단계", 1)
    bullets(doc, [
        ("2단계", "db-writer가 영속 세션(QoS 1)으로 scan을 구독해 DB에 저장. id UNIQUE로 중복 제거, "
                  "seq로 누락 확인, 수신 시각 기록, status로 스캐너 상태 관리"),
        ("3단계", "UNO는 네트워크가 없으므로 파이에서 MQTT를 구독해 USB 시리얼로 전달하는 브리지를 검토"),
    ])


def build():
    doc = Document()
    setup_styles(doc)
    title = doc.add_paragraph(style="Title")
    title.add_run("물류창고형 바코드 시스템 설계서")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.add_run(f"전공심화 4학년 2학기 프로젝트 · {DOC_VERSION}").italic = True
    table(doc, ["판", "날짜", "변경 내용"], [
        ["1단계", "2026-10-11", "환경 구성, 바코드 인식 → MQTT 발행 구현·테스트"],
    ], widths=[60, 80, 310])
    overview(doc)
    environment(doc)
    stage1(doc)
    next_steps(doc)
    doc.save(OUT)
    print(f"생성: {OUT}")


if __name__ == "__main__":
    build()
