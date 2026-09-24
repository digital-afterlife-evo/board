// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <stdbool.h>
#include <stdint.h>

// Translate one newly pressed USB HID keyboard key into a KX-R60 byte.
// Returns false for keys with no safe, confirmed byte mapping.
bool keyboard_map_key(uint8_t modifier, uint8_t key_code, bool *caps_lock,
                      uint8_t *out_byte);
