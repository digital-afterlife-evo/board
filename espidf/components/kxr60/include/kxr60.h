// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool initialized;
    bool faulted;
    esp_err_t last_error;
    uint32_t bytes_transmitted;
    uint32_t ack_timeouts;
    int failed_bit;              // -1: initial idle check / not a bit failure
    int expected_ack;            // -1: no ACK failure
} kxr60_status_t;

// Synchronous, single-owner API. Serialize ALL calls in one task.
// init also clears a latched fault; counters survive recovery.
esp_err_t kxr60_init(void);
esp_err_t kxr60_send_byte(uint8_t byte);
esp_err_t kxr60_write(const uint8_t *data, size_t len);
// Call only outside a transfer. Does not clear a latched fault.
void kxr60_release_lines(void);
// Call before terminating the owner task; leaves outputs driven HIGH.
void kxr60_deinit(void);
int kxr60_get_ack_level(void);
void kxr60_get_status(kxr60_status_t *status);

#ifdef __cplusplus
}
#endif
