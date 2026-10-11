// 배선·부품 설정. 실제 부품에 맞춰 이 파일만 고친다.
#pragma once

// ---- LED (각각 220~330옴 저항을 직렬로)
#define PIN_LED_GREEN 5        // 저장됨
#define PIN_LED_RED   6        // 거부됨 / 스캐너 lost / 브리지 끊김

// ---- 부저
#define PIN_BUZZER    8
#define USE_BUZZER    1        // 0이면 소리 끔
#define BUZZER_PASSIVE 1       // 1: 수동 부저(tone으로 주파수 출력), 0: 능동 부저(HIGH면 울림)
#define BEEP_HZ       2000

// ---- I2C LCD 16x2 (UNO: SDA=A4, SCL=A5, VCC=5V)
// 주소는 부팅 때 아래 두 개를 차례로 찾아본다. LCD가 없으면 LED·부저만으로 동작한다.
#define LCD_ADDR_1    0x27
#define LCD_ADDR_2    0x3F
#define LCD_COLS      16
#define LCD_ROWS      2

// ---- 통신
#define SERIAL_BAUD   115200
#define LINK_TIMEOUT_MS 12000UL  // 이 시간 동안 브리지에서 아무 프레임도 없으면 NO LINK (브리지는 5초마다 P)
#define MAX_SCANNERS  4          // 상태를 기억할 스캐너 수
