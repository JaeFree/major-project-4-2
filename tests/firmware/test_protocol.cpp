// arduino/uno_display/protocol.h 단위 테스트 (PC용 g++로 빌드, tests/test_firmware.py가 실행)
#include <cstdio>
#include <cstring>
#include <string>

#include "../../arduino/uno_display/protocol.h"

static int failures = 0;
#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);   \
            failures++;                                                   \
        }                                                                 \
    } while (0)

// 문자열을 LineReader에 넣고 완성된 줄 목록을 '\n'으로 이어 돌려준다
static std::string feedAll(proto::LineReader& r, const std::string& bytes, int* overflows = nullptr) {
    std::string out;
    for (char c : bytes) {
        if (r.feed(c)) out += std::string(r.line()) + "\n";
        if (r.overflowed() && overflows) (*overflows)++;
    }
    return out;
}

static void test_checksum_and_build() {
    const char* f[] = {"P", "7"};
    char out[proto::LINE_BUF];
    size_t n = proto::build(out, sizeof(out), f, 2);
    CHECK(n == std::strlen(out));
    // 'P'^'|'^'7' = 0x50^0x7C^0x37 = 0x1B
    CHECK(std::strcmp(out, "P|7*1B\n") == 0);
}

static void test_build_overflow_returns_zero() {
    std::string big(70, 'x');
    const char* f[] = {"S", big.c_str()};
    char out[proto::LINE_BUF];
    CHECK(proto::build(out, sizeof(out), f, 2) == 0);
}

static void test_parse_ok() {
    char line[] = "S|12|EAN13|dock-01|8801234567893*00";
    // 올바른 체크섬을 계산해 넣는다
    uint8_t c = proto::checksum(line, std::strchr(line, '*') - line);
    std::snprintf(std::strchr(line, '*'), 4, "*%02X", c);
    proto::Frame f;
    CHECK(proto::parse(line, f) == proto::OK);
    CHECK(f.count == 5 && f.type() == 'S');
    CHECK(std::strcmp(f.fields[1], "12") == 0);
    CHECK(std::strcmp(f.fields[4], "8801234567893") == 0);
}

static void test_parse_errors() {
    proto::Frame f;
    char a[] = "P|1*00";            CHECK(proto::parse(a, f) == proto::ERR_CHECKSUM);
    char b[] = "P|1";               CHECK(proto::parse(b, f) == proto::ERR_FORMAT);
    char c[] = "P|1*G1";            CHECK(proto::parse(c, f) == proto::ERR_FORMAT);
    char d[] = "*00";               CHECK(proto::parse(d, f) == proto::ERR_FORMAT);
    char e[] = "P|1*1";             CHECK(proto::parse(e, f) == proto::ERR_FORMAT);
    // 필드 7개 (최대 6)
    char g[] = "S|1|2|3|4|5|6*00";
    uint8_t ck = proto::checksum(g, std::strchr(g, '*') - g);
    std::snprintf(std::strchr(g, '*'), 4, "*%02X", ck);
    CHECK(proto::parse(g, f) == proto::ERR_FIELDS);
    // 종류가 두 글자
    char h[] = "SS|1*00";
    ck = proto::checksum(h, std::strchr(h, '*') - h);
    std::snprintf(std::strchr(h, '*'), 4, "*%02X", ck);
    CHECK(proto::parse(h, f) == proto::ERR_FORMAT);
}

static void test_reader_lines_crlf_and_empty() {
    proto::LineReader r;
    CHECK(feedAll(r, "a\r\n\n\nbc\n") == "a\nbc\n");   // CR 무시, 빈 줄 건너뜀
    CHECK(feedAll(r, "par") == "");                       // 아직 줄바꿈 없음
    CHECK(feedAll(r, "tial\n") == "partial\n");           // 나뉘어 들어와도 이어 붙임
}

static void test_reader_overflow_drops_whole_line() {
    proto::LineReader r;
    int overflows = 0;
    std::string ok63(63, 'a'), long64(64, 'b');
    CHECK(feedAll(r, ok63 + "\n", &overflows) == ok63 + "\n");    // 63바이트는 통과
    CHECK(overflows == 0);
    CHECK(feedAll(r, long64 + "\nnext\n", &overflows) == "next\n");  // 64바이트 줄은 통째로 버림
    CHECK(overflows == 1);
    std::string huge(500, 'c');
    CHECK(feedAll(r, huge + "\nZ\n", &overflows) == "Z\n");
    CHECK(overflows == 2);
}

int main() {
    test_checksum_and_build();
    test_build_overflow_returns_zero();
    test_parse_ok();
    test_parse_errors();
    test_reader_lines_crlf_and_empty();
    test_reader_overflow_drops_whole_line();
    if (failures) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("all protocol tests passed\n");
    return 0;
}
