// SPDX-License-Identifier: GPL-3.0-only
// KX-R60 handshake adapted from Matthew Nielsen (xunker):
// https://github.com/xunker/panasonic_typewriter_interface
// Commit f0dacdc7e1627dce5a4d6cc75f59393ccea50063, sendByte().
#include "kxr60.h"
#include "hw_config.h"
#include "sdkconfig.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <inttypes.h>

static const char *TAG = "KXR60";
static kxr60_status_t state = {.last_error = ESP_ERR_INVALID_STATE,
                             .failed_bit = -1, .expected_ack = -1};

void kxr60_release_lines(void)
{
    // End the byte before changing TXD. These fixed GPIOs are valid outputs.
    gpio_set_level(KXR_GPIO_ONLINE, 1);
    gpio_set_level(KXR_GPIO_STB, 1);
    gpio_set_level(KXR_GPIO_TXD, 1);
}

static esp_err_t fail(esp_err_t err)
{
    kxr60_release_lines();
    state.last_error = err;
    state.faulted = true;
    ESP_LOGE(TAG, "%s; outputs released, recovery required", esp_err_to_name(err));
    return err;
}

esp_err_t kxr60_init(void)
{
    state.initialized = false;
    // Preload output latches before gpio_config enables the output drivers.
    kxr60_release_lines();
    const gpio_config_t out = {
        .pin_bit_mask = (1ULL << KXR_GPIO_ONLINE) | (1ULL << KXR_GPIO_TXD) |
                        (1ULL << KXR_GPIO_STB),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&out);
    if (err != ESP_OK) return fail(err);
    const gpio_config_t in = {
        .pin_bit_mask = 1ULL << KXR_GPIO_ACK,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    err = gpio_config(&in);
    if (err != ESP_OK) return fail(err);
    kxr60_release_lines();
    state.initialized = true;
    state.faulted = false;
    state.last_error = ESP_OK;
    state.failed_bit = -1;
    state.expected_ack = -1;
    ESP_LOGI(TAG, "driver initialized; ACK=%d, timeout=%d ms",
             kxr60_get_ack_level(), CONFIG_KXR60_ACK_TIMEOUT_MS);
    return ESP_OK;
}

static esp_err_t wait_ack(int expected, int bit)
{
    const int64_t start = esp_timer_get_time();
    int64_t next_yield = start + 1000;
    const int64_t timeout = (int64_t)CONFIG_KXR60_ACK_TIMEOUT_MS * 1000;
    for (;;) {
        const int level = gpio_get_level(KXR_GPIO_ACK);
        const int64_t now = esp_timer_get_time();
        if (now - start >= timeout) {
            ++state.ack_timeouts;
            state.failed_bit = bit;
            state.expected_ack = expected;
            // Release before any potentially blocking log output.
            kxr60_release_lines();
            ESP_LOGE(TAG, "ACK timeout: bit=%d expected=%d actual=%d elapsed=%" PRId64 " us",
                     bit, expected, level, now - start);
            return ESP_ERR_TIMEOUT;
        }
        if (level == expected) return ESP_OK;
        // ACK is held until STB changes. Sleep only during a long ACK wait,
        // never during the 50 us TXD setup. Let the idle task/watchdog run.
        if (now >= next_yield) {
            vTaskDelay(1);
            next_yield = esp_timer_get_time() + 1000;
        } else {
            esp_rom_delay_us(1);
        }
    }
}

esp_err_t kxr60_send_byte(uint8_t byte)
{
    if (!state.initialized || state.faulted) {
        kxr60_release_lines();
        return ESP_ERR_INVALID_STATE; // Keep the original fault in status.
    }
    state.failed_bit = -1;
    state.expected_ack = -1;
    ESP_LOGI(TAG, "starting byte 0x%02X", byte);
    esp_err_t err = wait_ack(0, -1);
    if (err != ESP_OK) return fail(err);
    err = gpio_set_level(KXR_GPIO_ONLINE, 0);
    if (err != ESP_OK) return fail(err);
    for (int bit = 0; bit < 8; ++bit) {
        const int txd = (byte >> bit) & 1;
        const int64_t start = esp_timer_get_time();
        err = gpio_set_level(KXR_GPIO_TXD, txd);
        if (err != ESP_OK) return fail(err);
        esp_rom_delay_us(KXR_TXD_SETUP_US);
        err = gpio_set_level(KXR_GPIO_STB, 0);
        if (err != ESP_OK) return fail(err);
        err = wait_ack(1, bit);
        if (err != ESP_OK) return fail(err);
        err = gpio_set_level(KXR_GPIO_STB, 1);
        if (err != ESP_OK) return fail(err);
        err = wait_ack(0, bit);
        if (err != ESP_OK) return fail(err);
        ESP_LOGD(TAG, "byte=0x%02X bit=%d TXD=%d ACK=1->0 elapsed=%" PRId64 " us",
                 byte, bit, txd, esp_timer_get_time() - start);
    }
    // Preserve upstream end-of-byte ONLINE transition, then restore TXD idle.
    kxr60_release_lines();
    ++state.bytes_transmitted;
    state.last_error = ESP_OK;
    ESP_LOGI(TAG, "byte completed 0x%02X", byte);
    // Yield with all lines released even when every ACK was immediate.
    vTaskDelay(1);
    return ESP_OK;
}

esp_err_t kxr60_write(const uint8_t *data, size_t len)
{
    if (!state.initialized || state.faulted) {
        kxr60_release_lines();
        return ESP_ERR_INVALID_STATE;
    }
    if (!data && len) return fail(ESP_ERR_INVALID_ARG);
    for (size_t i = 0; i < len; ++i) {
        esp_err_t err = kxr60_send_byte(data[i]);
        if (err != ESP_OK) return err; // No automatic retries or dropped bytes.
    }
    return ESP_OK;
}

void kxr60_deinit(void)
{
    kxr60_release_lines();
    state.initialized = false;
}

int kxr60_get_ack_level(void)
{
    return state.initialized ? gpio_get_level(KXR_GPIO_ACK) : -1;
}

void kxr60_get_status(kxr60_status_t *status)
{
    if (status) *status = state;
}
