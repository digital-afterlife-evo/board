"""Compile the production KX-R60 driver against a simulated typewriter.

No board connection and no third-party Python packages required.
"""
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "tests" / "build"

STUB = r'''
#pragma once
#include <stdint.h>
#include <stddef.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_TIMEOUT 0x107
const char *esp_err_to_name(esp_err_t err);
typedef int gpio_num_t;
enum {GPIO_NUM_1=1, GPIO_NUM_2=2, GPIO_NUM_42=42, GPIO_NUM_47=47};
enum {GPIO_MODE_INPUT=1, GPIO_MODE_OUTPUT=2, GPIO_PULLUP_DISABLE=0,
      GPIO_PULLDOWN_DISABLE=0, GPIO_INTR_DISABLE=0};
typedef struct {uint64_t pin_bit_mask; int mode, pull_up_en, pull_down_en, intr_type;} gpio_config_t;
esp_err_t gpio_config(const gpio_config_t *cfg);
esp_err_t gpio_set_level(gpio_num_t pin, uint32_t level);
int gpio_get_level(gpio_num_t pin);
int64_t esp_timer_get_time(void);
void esp_rom_delay_us(uint32_t us);
void vTaskDelay(unsigned ticks);
#define pdMS_TO_TICKS(ms) (ms)
#define CONFIG_KXR60_ACK_TIMEOUT_MS 500
#define CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG 1
typedef struct {size_t rx_buffer_size, tx_buffer_size;} usb_serial_jtag_driver_config_t;
esp_err_t usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *config);
esp_err_t esp_register_shutdown_handler(void (*handler)(void));
enum {ESP_LINE_ENDINGS_LF=0, ESP_LINE_ENDINGS_CRLF=1};
void usb_serial_jtag_vfs_set_rx_line_endings(int mode);
void usb_serial_jtag_vfs_set_tx_line_endings(int mode);
void usb_serial_jtag_vfs_use_driver(void);
void fake_log(const char *tag, const char *format, ...);
#define ESP_LOGI(...) fake_log(__VA_ARGS__)
#define ESP_LOGE(...) fake_log(__VA_ARGS__)
#define ESP_LOGD(...) fake_log(__VA_ARGS__)
'''


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--cc", default=shutil.which("gcc") or "D:/Dev-Cpp/MinGW64/bin/gcc.exe")
    args = parser.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "fake.h").write_text(STUB, encoding="utf-8")
    for header in ["esp_err.h", "driver/gpio.h", "sdkconfig.h", "esp_log.h",
                   "esp_rom_sys.h", "esp_timer.h", "freertos/FreeRTOS.h", "freertos/task.h",
                   "driver/uart.h", "driver/usb_serial_jtag.h", "esp_system.h",
                   "esp_vfs_dev.h", "driver/usb_serial_jtag_vfs.h"]:
        path = BUILD / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "fake.h"\n', encoding="utf-8")
    exe = BUILD / "test_driver.exe"
    subprocess.run([args.cc, "-std=c99", "-Wall", "-Wextra", "-Werror",
                    "-I", str(BUILD), "-I", str(ROOT / "components/kxr60/include"),
                    str(ROOT / "components/kxr60/kxr60.c"), str(ROOT / "tests/test_driver.c"),
                    "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
    console_exe = BUILD / "test_console.exe"
    subprocess.run([args.cc, "-std=c99", "-Wall", "-Wextra", "-Werror",
                    "-Dgetchar=test_getchar", "-I", str(BUILD),
                    "-I", str(ROOT / "components/kxr60/include"),
                    str(ROOT / "main/app_main.c"), str(ROOT / "tests/test_console.c"),
                    "-o", str(console_exe)], check=True)
    payload = (b"status\r\nraw 41\r\nrepeat 41 10\nprint HELLO  WORLD\nraw 00\n"
               b"raw 4\nraw 100\nraw -1\nraw GG\nraw 41junk\n"
               b"print prefix " + "\u4f60\u597d".encode() + b"\n"
               b"print " + b"X" * 300 + b"\n"
               b"print BAD\tTEXT\n"
               b'print "quoted"\nprint AB\bC\nprint discarded\x15raw 42\n'
               b"print " + b"Z" * 249 + b"\n"
               b"demo\nrecover\nhelp\n")
    result = subprocess.run([str(console_exe)], input=payload, capture_output=True,
                            check=True, timeout=10)
    expected = b'\x41' + b'A' * 10 + b'HELLO  WORLD\x00"quoted"AC\x42' + b"Z" * 249 + b"HELLO WORLD\r\n"
    assert b"ACKED_HEX=" + expected.hex().encode() in result.stdout, result.stdout
    assert result.stdout.count(b"Rejected: ASCII only") == 3, result.stdout
    assert b"WRITE_CALLS=18" in result.stdout, result.stdout
    assert b"repeat complete: 10/10 bytes acknowledged" in result.stdout, result.stdout
    print("PASS: console CR/LF/CRLF, repeat, literal spaces/quotes, raw syntax, UTF-8/control/overflow rejection, editing, demo")


if __name__ == "__main__":
    main()
