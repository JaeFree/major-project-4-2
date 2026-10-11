// 물류창고 바코드 시스템 - 3단계 UNO 표시 장치
//
// 파이의 uno-bridge가 USB 시리얼로 보내는 DB 저장 결과·스캐너 상태를 LCD, LED, 부저로 알린다.
//   S 저장됨   : LCD에 종류·스캐너 / 바코드 값, 초록 깜빡임 + 짧은 삐
//   X 거부됨   : LCD에 REJECTED / 사유, 빨강 깜빡임 + 긴 삐
//   T 상태     : lost인 스캐너가 하나라도 있으면 빨강 계속 켜짐, 상태 변화는 3초간 LCD에 표시
//   P 연결 확인: 응답만
//   브리지에서 LINK_TIMEOUT_MS 동안 아무것도 오지 않으면 NO LINK + 빨강 느린 깜빡임
// 수신한 프레임마다 A|n 으로 확인 응답, 부팅하면 R|uno-display|1 을 보낸다.
// 프로토콜: protocol.h / 배선·부품: pins.h
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

#include "pins.h"
#include "protocol.h"

static const char FW_NAME[] = "uno-display";
static const char FW_VERSION[] = "1";

static proto::LineReader reader;
static LiquidCrystal_I2C* lcd = nullptr;   // 부팅 때 주소를 찾아 만든다 (없으면 nullptr)

// 마지막 스캔 화면 (상태 메시지를 잠깐 보여 준 뒤 되돌아온다)
static char scanLine1[LCD_COLS + 1] = "Barcode Display";
static char scanLine2[LCD_COLS + 1] = "waiting...";

struct ScannerState {
    char id[13];
    bool lost;
};
static ScannerState scanners[MAX_SCANNERS];

static unsigned long lastRxAt = 0;
static bool linkOk = false;
static unsigned long greenOffAt = 0, redBlinkOffAt = 0, buzzerOffAt = 0, restoreScreenAt = 0;

// ------------------------------------------------------------ 출력 도우미
static bool timeReached(unsigned long now, unsigned long at) {
    return at != 0 && (long)(now - at) >= 0;   // millis() 넘김(약 49일)에도 안전한 비교
}

static void copyFit(char* dst, const char* src, uint8_t width) {
    // LCD 한 줄에 맞춰 복사. 넘치면 마지막 칸을 '~'로 표시
    size_t n = strlen(src);
    if (n <= width) {
        strcpy(dst, src);
    } else {
        memcpy(dst, src, width - 1);
        dst[width - 1] = '~';
        dst[width] = '\0';
    }
}

static void show(const char* line1, const char* line2) {
    if (!lcd) return;
    char buf[LCD_COLS + 1];
    lcd->clear();
    copyFit(buf, line1, LCD_COLS);
    lcd->setCursor(0, 0);
    lcd->print(buf);
    copyFit(buf, line2, LCD_COLS);
    lcd->setCursor(0, 1);
    lcd->print(buf);
}

static void beep(unsigned int ms) {
#if USE_BUZZER
#if BUZZER_PASSIVE
    tone(PIN_BUZZER, BEEP_HZ, ms);
#else
    digitalWrite(PIN_BUZZER, HIGH);
    buzzerOffAt = millis() + ms;
#endif
#endif
}

static bool anyLost() {
    for (uint8_t i = 0; i < MAX_SCANNERS; i++)
        if (scanners[i].id[0] && scanners[i].lost) return true;
    return false;
}

static void sendFrame(const char* const* fields, uint8_t n) {
    char out[proto::LINE_BUF];
    if (proto::build(out, sizeof(out), fields, n)) Serial.print(out);
}

static void sendError(const char* code) {
    const char* f[] = {"E", code};
    sendFrame(f, 2);
}

// ------------------------------------------------------------ 프레임 처리
static void onSaved(const proto::Frame& f) {          // S|n|type|scanner|data
    if (f.count < 5) return;
    char l1[LCD_COLS * 2 + 2];
    snprintf(l1, sizeof(l1), "%s %s", f.fields[2], f.fields[3]);
    copyFit(scanLine1, l1, LCD_COLS);
    copyFit(scanLine2, f.fields[4], LCD_COLS);
    show(scanLine1, scanLine2);
    restoreScreenAt = 0;
    digitalWrite(PIN_LED_GREEN, HIGH);
    greenOffAt = millis() + 300;
    beep(80);
}

static void onRejected(const proto::Frame& f) {       // X|n|scanner|reason
    if (f.count < 4) return;
    char l1[LCD_COLS * 2 + 2];
    snprintf(l1, sizeof(l1), "REJECTED %s", f.fields[2]);
    show(l1, f.fields[3]);
    restoreScreenAt = millis() + 5000;
    redBlinkOffAt = millis() + 600;
    beep(400);
}

static void onStatus(const proto::Frame& f) {         // T|n|scanner|state
    if (f.count < 4) return;
    const char* id = f.fields[2];
    bool lost = strcmp(f.fields[3], "lost") == 0;
    int8_t slot = -1, empty = -1;
    for (uint8_t i = 0; i < MAX_SCANNERS; i++) {
        if (strncmp(scanners[i].id, id, sizeof(scanners[i].id) - 1) == 0) slot = i;
        else if (!scanners[i].id[0] && empty < 0) empty = i;
    }
    if (slot < 0) {
        if (!lost) return;                // 모르는 스캐너의 정상 상태는 기억할 필요 없음
        slot = empty >= 0 ? empty : 0;    // 꽉 차면 첫 칸을 덮어씀
        strncpy(scanners[slot].id, id, sizeof(scanners[slot].id) - 1);
        scanners[slot].id[sizeof(scanners[slot].id) - 1] = '\0';
    }
    bool changed = scanners[slot].lost != lost;
    scanners[slot].lost = lost;
    if (!lost) scanners[slot].id[0] = '\0';   // 정상이면 칸 비움
    if (changed || lost) {
        show(id, f.fields[3]);
        restoreScreenAt = millis() + 3000;
        if (lost) beep(200);
    }
}

static void handleLine(char* line) {
    proto::Frame f;
    proto::Result r = proto::parse(line, f);
    if (r == proto::ERR_CHECKSUM) { sendError("CK"); return; }
    if (r != proto::OK || f.count < 2) { sendError("FMT"); return; }

    const char* ack[] = {"A", f.fields[1]};     // 확인 응답은 처리 전에 (브리지 재전송 방지)
    sendFrame(ack, 2);
    lastRxAt = millis();
    if (!linkOk) {
        linkOk = true;
        show(scanLine1, scanLine2);
    }
    switch (f.type()) {
        case 'S': onSaved(f); break;
        case 'X': onRejected(f); break;
        case 'T': onStatus(f); break;
        case 'P': break;
        default: break;
    }
}

// ------------------------------------------------------------ setup / loop
static void setupLcd() {
    Wire.begin();
    const uint8_t addrs[] = {LCD_ADDR_1, LCD_ADDR_2};
    for (uint8_t i = 0; i < sizeof(addrs); i++) {
        Wire.beginTransmission(addrs[i]);
        if (Wire.endTransmission() == 0) {         // 응답한 주소의 LCD 사용
            lcd = new LiquidCrystal_I2C(addrs[i], LCD_COLS, LCD_ROWS);
            lcd->init();
            lcd->backlight();
            return;
        }
    }
}

void setup() {
    pinMode(PIN_LED_GREEN, OUTPUT);
    pinMode(PIN_LED_RED, OUTPUT);
#if USE_BUZZER
    pinMode(PIN_BUZZER, OUTPUT);
#endif
    Serial.begin(SERIAL_BAUD);
    setupLcd();
    show(scanLine1, scanLine2);
    // 전원 확인용: LED 두 개 잠깐 켜기
    digitalWrite(PIN_LED_GREEN, HIGH);
    digitalWrite(PIN_LED_RED, HIGH);
    delay(200);
    digitalWrite(PIN_LED_GREEN, LOW);
    digitalWrite(PIN_LED_RED, LOW);

    const char* ready[] = {"R", FW_NAME, FW_VERSION};
    sendFrame(ready, 3);
    lastRxAt = millis();
}

void loop() {
    while (Serial.available()) {
        if (reader.feed((char)Serial.read())) handleLine(reader.line());
        if (reader.overflowed()) sendError("LEN");
    }

    unsigned long now = millis();
    if (timeReached(now, greenOffAt)) { digitalWrite(PIN_LED_GREEN, LOW); greenOffAt = 0; }
    if (timeReached(now, buzzerOffAt)) { digitalWrite(PIN_BUZZER, LOW); buzzerOffAt = 0; }
    if (timeReached(now, redBlinkOffAt)) redBlinkOffAt = 0;
    if (timeReached(now, restoreScreenAt)) { restoreScreenAt = 0; if (linkOk) show(scanLine1, scanLine2); }

    if (linkOk && now - lastRxAt > LINK_TIMEOUT_MS) {
        linkOk = false;
        show("NO LINK", "check bridge");
    }

    // 빨강: 거부 깜빡임 중 / lost 스캐너 있음 / 링크 끊김이면 1초 주기 깜빡임
    bool red = redBlinkOffAt != 0 ? ((now / 100) % 2) : anyLost();
    if (!linkOk) red = (now / 1000) % 2;
    digitalWrite(PIN_LED_RED, red ? HIGH : LOW);
}
