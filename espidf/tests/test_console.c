// SPDX-License-Identifier: GPL-3.0-only
// Exercise production app_main using stdin, with a recording driver boundary.
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
#include "fake.h"
#include "kxr60.h"

static uint8_t bytes[2048];
static size_t length, writes;
static kxr60_status_t state;
void app_main(void);
const char *esp_err_to_name(esp_err_t err) { (void)err; return "ESP_OK"; }
void fake_log(const char *tag, const char *format, ...) { (void)tag; (void)format; }
void vTaskDelay(unsigned ticks) { (void)ticks; }
esp_err_t usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *config)
{ assert(config->rx_buffer_size >= 256); return ESP_OK; }
esp_err_t esp_register_shutdown_handler(void (*handler)(void))
{ assert(handler == kxr60_release_lines); return ESP_OK; }
void usb_serial_jtag_vfs_set_rx_line_endings(int mode) { (void)mode; }
void usb_serial_jtag_vfs_set_tx_line_endings(int mode) { (void)mode; }
void usb_serial_jtag_vfs_use_driver(void) {}
esp_err_t kxr60_init(void) { state.initialized = true; return ESP_OK; }
void kxr60_release_lines(void) {}
void kxr60_deinit(void) { state.initialized = false; }
int kxr60_get_ack_level(void) { return 0; }
void kxr60_get_status(kxr60_status_t *out) { *out = state; }
esp_err_t kxr60_write(const uint8_t *data, size_t len)
{
    assert(length + len <= sizeof(bytes));
    memcpy(bytes + length, data, len);
    length += len;
    state.bytes_transmitted += len;
    ++writes;
    return ESP_OK;
}
esp_err_t kxr60_send_byte(uint8_t byte)
{
    return kxr60_write(&byte, 1);
}
int test_getchar(void)
{
    int c = fgetc(stdin);
    if (c == EOF) {
        printf("\nWRITE_CALLS=%u\nACKED_HEX=", (unsigned)writes);
        for (size_t i = 0; i < length; ++i) printf("%02x", bytes[i]);
        puts("");
        exit(0);
    }
    return c;
}
int main(void)
{
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
#endif
    app_main();
    return 1;
}
