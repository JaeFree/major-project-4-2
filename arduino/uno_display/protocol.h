// 파이(브리지) <-> UNO 시리얼 프로토콜. 파이 쪽 구현: barcode_system/uno_protocol.py
//
//   한 줄 = 프레임 1개:  <종류>|<필드>|...*<체크섬 16진 2자리>\n   (체크섬 = '*' 앞 바이트 XOR)
//   줄 길이(줄바꿈 제외) 최대 63바이트.
//
// Arduino 기능을 쓰지 않으므로 PC용 g++로도 그대로 빌드해 단위 테스트한다 (tests/firmware/).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace proto {

const size_t LINE_BUF = 64;     // 수신 버퍼 (끝의 NUL 포함) -> 줄 최대 63바이트
const uint8_t MAX_FIELDS = 6;

enum Result { OK = 0, ERR_FORMAT, ERR_CHECKSUM, ERR_FIELDS };

inline uint8_t checksum(const char* s, size_t n) {
    uint8_t c = 0;
    for (size_t i = 0; i < n; i++) c ^= (uint8_t)s[i];
    return c;
}

inline int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

// 시리얼 바이트를 한 줄씩 모은다. 버퍼보다 긴 줄은 통째로 버리고 overflowed()로 알린다.
class LineReader {
  public:
    // 바이트 1개를 넣는다. 빈 줄이 아닌 한 줄이 완성되면 true -> line()으로 꺼낸다.
    // (다음 feed() 호출 전에 line()을 다 써야 한다)
    bool feed(char ch) {
        if (ch == '\r') return false;
        if (ch == '\n') {
            bool skip = dropping_ || len_ == 0;
            if (dropping_) overflowed_ = true;
            dropping_ = false;
            buf_[len_] = '\0';
            len_ = 0;
            return !skip;
        }
        if (dropping_) return false;
        if (len_ >= LINE_BUF - 1) {   // 줄이 너무 길다 -> 줄바꿈까지 버림
            dropping_ = true;
            len_ = 0;
            return false;
        }
        buf_[len_++] = ch;
        return false;
    }
    char* line() { return buf_; }
    // 직전에 너무 긴 줄을 버렸으면 true (한 번만)
    bool overflowed() {
        bool o = overflowed_;
        overflowed_ = false;
        return o;
    }

  private:
    char buf_[LINE_BUF] = {0};
    size_t len_ = 0;
    bool dropping_ = false;
    bool overflowed_ = false;
};

struct Frame {
    uint8_t count = 0;
    char* fields[MAX_FIELDS] = {nullptr};
    char type() const { return count ? fields[0][0] : '\0'; }
};

// line을 제자리에서 나눠 frame에 담는다 ('|'와 '*' 자리에 NUL을 쓴다).
inline Result parse(char* line, Frame& f) {
    f.count = 0;
    char* star = strrchr(line, '*');
    if (!star || star == line || strlen(star) != 3) return ERR_FORMAT;
    int hi = hexval(star[1]), lo = hexval(star[2]);
    if (hi < 0 || lo < 0) return ERR_FORMAT;
    if (checksum(line, (size_t)(star - line)) != (uint8_t)(hi << 4 | lo)) return ERR_CHECKSUM;
    *star = '\0';
    char* p = line;
    for (;;) {
        if (f.count >= MAX_FIELDS) return ERR_FIELDS;
        f.fields[f.count++] = p;
        char* bar = strchr(p, '|');
        if (!bar) break;
        *bar = '\0';
        p = bar + 1;
    }
    if (strlen(f.fields[0]) != 1) return ERR_FORMAT;
    return OK;
}

// fields를 '|'로 이어 "...*HH\n" 한 줄을 out에 만든다. 길이(NUL 제외)를 반환, 넘치면 0.
inline size_t build(char* out, size_t cap, const char* const* fields, uint8_t n) {
    size_t len = 0;
    for (uint8_t i = 0; i < n; i++) {
        size_t fl = strlen(fields[i]);
        if (len + fl + (i ? 1 : 0) + 4 >= cap) return 0;   // '*HH\n' + NUL 자리 확보
        if (i) out[len++] = '|';
        memcpy(out + len, fields[i], fl);
        len += fl;
    }
    // ('HEX'라는 이름은 Arduino.h의 매크로와 충돌하므로 쓰지 않는다)
    static const char HEX_DIGITS[] = "0123456789ABCDEF";
    uint8_t c = checksum(out, len);
    out[len++] = '*';
    out[len++] = HEX_DIGITS[c >> 4];
    out[len++] = HEX_DIGITS[c & 0x0F];
    out[len++] = '\n';
    out[len] = '\0';
    return len;
}

}  // namespace proto
