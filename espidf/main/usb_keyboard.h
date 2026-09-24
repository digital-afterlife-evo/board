// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

typedef esp_err_t (*usb_keyboard_emit_fn_t)(unsigned char byte);
typedef bool (*usb_keyboard_event_fn_t)(uint8_t modifier, uint8_t key_code,
                                        const uint8_t *output, size_t output_length,
                                        bool ctrl_enter);

esp_err_t usb_keyboard_start(usb_keyboard_emit_fn_t emit,
                             usb_keyboard_event_fn_t event);
