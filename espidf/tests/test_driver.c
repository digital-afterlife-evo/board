// SPDX-License-Identifier: GPL-3.0-only
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "fake.h"
#include "hw_config.h"
#include "kxr60.h"

static int levels[48], ack, pending_ack, bit_count, bytes_started;
static int fail_bit, fail_byte, fail_phase, config_failure, write_failure;
static int active, rising_acks, falling_acks, yields, config_calls;
static unsigned received;
static int64_t now, ack_due, txd_set_at;
static unsigned ack_delay;

const char *esp_err_to_name(esp_err_t err) { (void)err; return "mock"; }
void fake_log(const char *tag, const char *format, ...) { (void)tag; (void)format; }
int64_t esp_timer_get_time(void) { return now; }
void esp_rom_delay_us(uint32_t us) { now += us; }
void vTaskDelay(unsigned ticks) { now += ticks * 1000; ++yields; }

static void released(void)
{
    assert(levels[KXR_GPIO_ONLINE] == 1);
    assert(levels[KXR_GPIO_TXD] == 1);
    assert(levels[KXR_GPIO_STB] == 1);
}

esp_err_t gpio_config(const gpio_config_t *cfg)
{
    ++config_calls;
    // Preload HIGH before switching GPIOs to outputs, with no internal pulls.
    released();
    assert(!cfg->pull_up_en && !cfg->pull_down_en && !cfg->intr_type);
    if (cfg->mode == GPIO_MODE_OUTPUT) {
        assert(cfg->pin_bit_mask == ((1ULL<<47) | (1ULL<<2) | (1ULL<<42)));
    } else {
        assert(cfg->mode == GPIO_MODE_INPUT && cfg->pin_bit_mask == (1ULL<<1));
    }
    return config_calls == config_failure ? ESP_FAIL : ESP_OK;
}

int gpio_get_level(gpio_num_t pin)
{
    assert(pin == KXR_GPIO_ACK);
    if (now >= ack_due) ack = pending_ack;
    return ack;
}

esp_err_t gpio_set_level(gpio_num_t pin, uint32_t level)
{
    assert(pin == KXR_GPIO_ONLINE || pin == KXR_GPIO_TXD || pin == KXR_GPIO_STB);
    if (write_failure && pin == KXR_GPIO_TXD && active) {
        write_failure = 0;
        return ESP_FAIL;
    }
    if (pin == KXR_GPIO_ONLINE) {
        if (!level) {
            assert(!active && !gpio_get_level(KXR_GPIO_ACK));
            active = 1;
            bit_count = 0;
            received = 0;
            ++bytes_started;
        } else {
            active = 0;
        }
    }
    if (pin == KXR_GPIO_TXD) {
        if (active) assert(levels[KXR_GPIO_STB] == 1);
        txd_set_at = now;
    }
    if (pin == KXR_GPIO_STB && active && levels[pin] != (int)level) {
        if (!level) {
            assert(bit_count < 8);
            assert(gpio_get_level(KXR_GPIO_ACK) == 0);
            assert(now - txd_set_at >= 50);
            received |= (unsigned)levels[KXR_GPIO_TXD] << bit_count;
            pending_ack = !(bytes_started == fail_byte && bit_count == fail_bit && fail_phase == 1);
            ++rising_acks;
        } else {
            assert(gpio_get_level(KXR_GPIO_ACK) == 1);
            pending_ack = (bytes_started == fail_byte && bit_count == fail_bit && fail_phase == 0);
            ++bit_count;
            ++falling_acks;
        }
        ack_due = now + ack_delay;
    }
    levels[pin] = level;
    return ESP_OK;
}

static void reset_mock(void)
{
    kxr60_deinit();
    memset(levels, 0, sizeof(levels));
    ack = pending_ack = bit_count = bytes_started = 0;
    active = rising_acks = falling_acks = yields = config_calls = 0;
    config_failure = write_failure = 0;
    fail_byte = 1;
    fail_bit = fail_phase = -1;
    now = ack_due = txd_set_at = 0;
    ack_delay = 5;
}

static kxr60_status_t get_status(void)
{
    kxr60_status_t s;
    kxr60_get_status(&s);
    return s;
}

int main(void)
{
    reset_mock();
    assert(kxr60_send_byte(0x41) == ESP_ERR_INVALID_STATE);
    released();
    assert(kxr60_get_ack_level() == -1);

    // All possible bytes, including raw NUL, bit order, setup and handshake.
    for (unsigned byte = 0; byte < 256; ++byte) {
        reset_mock();
        assert(kxr60_init() == ESP_OK);
        unsigned count = get_status().bytes_transmitted;
        assert(kxr60_send_byte((uint8_t)byte) == ESP_OK);
        assert(received == byte && bytes_started == 1 && bit_count == 8);
        assert(rising_acks == 8 && falling_acks == 8 && ack == 0);
        assert(get_status().bytes_transmitted == count + 1);
        assert(yields > 0);
        released();
    }
    puts("PASS: 256 byte patterns, LSB first, >=50 us setup, 8 ACK cycles, idle HIGH");

    // Both failed ACK phases at every bit position; no retry until recovery.
    for (int phase = 0; phase < 2; ++phase) {
        for (int bit = 0; bit < 8; ++bit) {
            reset_mock();
            assert(kxr60_init() == ESP_OK);
            kxr60_status_t before = get_status();
            fail_phase = phase;
            fail_bit = bit;
            assert(kxr60_send_byte(0xa5) == ESP_ERR_TIMEOUT);
            kxr60_status_t after = get_status();
            assert(after.faulted && after.last_error == ESP_ERR_TIMEOUT);
            assert(after.failed_bit == bit && after.expected_ack == phase);
            assert(after.ack_timeouts == before.ack_timeouts + 1);
            assert(after.bytes_transmitted == before.bytes_transmitted);
            assert(now >= 500000 && now < 510000 && yields > 0);
            released();
            assert(kxr60_send_byte(0x41) == ESP_ERR_INVALID_STATE);
            assert(bytes_started == 1);
            assert(get_status().last_error == ESP_ERR_TIMEOUT);
            // Simulate external resynchronization before explicit recovery.
            ack = pending_ack = 0;
            ack_due = now;
            fail_phase = -1;
            assert(kxr60_init() == ESP_OK);
            assert(kxr60_send_byte(0x41) == ESP_OK);
            released();
        }
    }
    puts("PASS: all 16 ACK failure positions, bounded timeouts, release, fault latch/recovery");

    reset_mock();
    assert(kxr60_init() == ESP_OK);
    ack = pending_ack = 1;
    assert(kxr60_send_byte(0x41) == ESP_ERR_TIMEOUT);
    assert(bytes_started == 0 && get_status().failed_bit == -1);
    released();
    puts("PASS: initially stuck-HIGH ACK never starts a byte");

    reset_mock();
    assert(kxr60_init() == ESP_OK);
    fail_byte = 2;
    fail_bit = 3;
    fail_phase = 1;
    unsigned count = get_status().bytes_transmitted;
    const uint8_t message[] = {0x41, 0x42, 0x43};
    assert(kxr60_write(message, sizeof(message)) == ESP_ERR_TIMEOUT);
    assert(bytes_started == 2 && get_status().bytes_transmitted == count + 1);
    released();
    puts("PASS: partial write stops at fault with accurate success count");

    for (int cfg = 1; cfg <= 2; ++cfg) {
        reset_mock();
        config_failure = cfg;
        assert(kxr60_init() == ESP_FAIL);
        assert(!get_status().initialized && get_status().faulted);
        released();
    }
    reset_mock();
    assert(kxr60_init() == ESP_OK);
    write_failure = 1;
    assert(kxr60_send_byte(0x41) == ESP_FAIL);
    released();
    assert(kxr60_init() == ESP_OK);
    assert(kxr60_write(NULL, 0) == ESP_OK);
    assert(kxr60_write(NULL, 1) == ESP_ERR_INVALID_ARG);
    released();
    kxr60_deinit();
    assert(!get_status().initialized);
    assert(kxr60_write(message, sizeof(message)) == ESP_ERR_INVALID_STATE);
    released();
    puts("PASS: init/GPIO/argument failures and shutdown release outputs");

    reset_mock();
    assert(kxr60_init() == ESP_OK);
    ack_delay = 4000;
    assert(kxr60_send_byte(0x81) == ESP_OK && received == 0x81);
    assert(yields > 16);
    released();
    puts("PASS: slow ACK remains valid while yielding to scheduler");
    return 0;
}
