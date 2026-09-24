// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    bool initialized;
    bool faulted;
    esp_err_t last_error;
    uint32_t bytes_printed;
    size_t queued_bytes;
    size_t capacity;
} printer_status_t;

esp_err_t printer_start(size_t capacity);
esp_err_t printer_enqueue(const uint8_t *data, size_t length, uint32_t timeout_ms);
void printer_get_status(printer_status_t *status);
esp_err_t printer_recover(void);
void printer_flush(void);
void printer_stop(void);
