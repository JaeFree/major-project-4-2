# UNO 표시 장치 (3단계)

파이의 `uno-bridge`가 USB 시리얼로 보내는 **DB 저장 결과**와 **스캐너 상태**를 LCD·LED·부저로 알린다.

| 이벤트 | LCD | LED / 부저 |
|---|---|---|
| 저장됨 (S) | 1줄: 종류 스캐너 / 2줄: 바코드 값 (16자 초과 시 `~`) | 초록 0.3초 + 짧은 삐 |
| 거부됨 (X) | `REJECTED <스캐너>` / 사유 (5초 뒤 마지막 스캔 화면) | 빨강 빠른 깜빡임 + 긴 삐 |
| 스캐너 lost (T) | 스캐너 / `lost` (3초) | 빨강 계속 켜짐 (online이 되면 꺼짐) + 삐 |
| 브리지 끊김 (12초 동안 수신 없음) | `NO LINK` / `check bridge` | 빨강 1초 주기 깜빡임 |

## 배선 (핀은 `pins.h`에서 변경)

| 부품 | UNO 핀 | 비고 |
|---|---|---|
| I2C LCD 16x2 SDA | A4 | VCC → 5V, GND → GND. 주소 0x27/0x3F 자동 검색 |
| I2C LCD 16x2 SCL | A5 | |
| 초록 LED (+) | D5 | 220~330Ω 저항 직렬, (−) → GND |
| 빨강 LED (+) | D6 | 220~330Ω 저항 직렬, (−) → GND |
| 부저 (+) | D8 | (−) → GND. 능동 부저면 `pins.h`의 `BUZZER_PASSIVE 0` |

LCD 글자가 안 보이면 LCD 뒷면 I2C 모듈의 가변저항(명암)을 돌려 본다.

## 컴파일·업로드 (arduino-cli, 버전은 `sketch.yaml`에 고정)

```
arduino-cli compile --profile uno arduino/uno_display
arduino-cli upload  --profile uno -p COM5 arduino/uno_display          # Windows
arduino-cli upload  --profile uno -p /dev/ttyUSB0 arduino/uno_display  # 파이 (CH340/CH341)
```

- CH340/CH341 호환보드: Windows는 드라이버가 자동 설치되지 않으면 WCH의 CH341SER 드라이버 필요. 파이(리눅스)는 커널에 내장(`ch341`), 포트는 `/dev/ttyUSB0`.
- 정품 UNO는 `/dev/ttyACM0`.
- 업로드할 때는 `uno-bridge`를 멈춘다 (같은 포트를 동시에 쓸 수 없음): `sudo systemctl stop barcode-uno-bridge`

## 단독 시험 (브리지 없이, 시리얼 모니터)

115200bps, 줄 끝 `LF`로 아래를 보내면 동작을 확인할 수 있다 (`*` 뒤는 체크섬).

```
P|1*1D              → A|1*.. 응답
S|2|EAN13|dock-01|8801234567893*34
```

체크섬은 `python -c "from barcode_system import uno_protocol as u; print(u.saved(2,'EAN13','dock-01','8801234567893'))"` 로 만들 수 있다.
