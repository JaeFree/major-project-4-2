// 표준 입력의 바이트를 UNO와 똑같이 LineReader -> parse 로 처리해 결과를 한 줄씩 출력한다.
//   OK<TAB>필드1<TAB>필드2...   /   ERR<TAB>코드   /   OVERFLOW
// tests/test_firmware.py가 Python(uno_protocol.encode)으로 만든 프레임을 넣어 양쪽 구현이 일치하는지 확인한다.
#include <cstdio>

#include "../../arduino/uno_display/protocol.h"

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

int main() {
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);    // 텍스트 모드의 CRLF 변환·Ctrl+Z 종료 방지
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    proto::LineReader reader;
    int ch;
    while ((ch = std::getchar()) != EOF) {
        bool complete = reader.feed((char)ch);
        if (reader.overflowed()) std::printf("OVERFLOW\n");
        if (!complete) continue;
        proto::Frame f;
        proto::Result r = proto::parse(reader.line(), f);
        if (r != proto::OK) {
            std::printf("ERR\t%d\n", (int)r);
            continue;
        }
        std::printf("OK");
        for (uint8_t i = 0; i < f.count; i++) std::printf("\t%s", f.fields[i]);
        std::printf("\n");
    }
    return 0;
}
