// SPDX-License-Identifier: GPL-3.0-only
#include "keyboard_map.h"

#include <stddef.h>

#include "usb/hid_usage_keyboard.h"

static bool has_shift(uint8_t modifier)
{
    return (modifier & (HID_LEFT_SHIFT | HID_RIGHT_SHIFT)) != 0;
}

static bool has_ctrl(uint8_t modifier)
{
    return (modifier & (HID_LEFT_CONTROL | HID_RIGHT_CONTROL)) != 0;
}

static bool map_ascii_key(uint8_t key_code, bool shifted, uint8_t *out_byte)
{
    if (key_code >= HID_KEY_A && key_code <= HID_KEY_Z) {
        *out_byte = (uint8_t)((shifted ? 'A' : 'a') + (key_code - HID_KEY_A));
        return true;
    }

    if (key_code >= HID_KEY_1 && key_code <= HID_KEY_0) {
        static const char normal_digits[] = "1234567890";
        static const char shifted_digits[] = "!@#$%^&*()";
        const size_t index = key_code - HID_KEY_1;
        *out_byte = (uint8_t)(shifted ? shifted_digits[index] : normal_digits[index]);
        return true;
    }

    switch (key_code) {
    case HID_KEY_MINUS: *out_byte = shifted ? '_' : '-'; return true;
    case HID_KEY_EQUAL: *out_byte = shifted ? '+' : '='; return true;
    case HID_KEY_OPEN_BRACKET: *out_byte = shifted ? '{' : '['; return true;
    case HID_KEY_CLOSE_BRACKET: *out_byte = shifted ? '}' : ']'; return true;
    case HID_KEY_BACK_SLASH:
    case HID_KEY_SHARP: *out_byte = shifted ? '|' : '\\'; return true;
    case HID_KEY_COLON: *out_byte = shifted ? ':' : ';'; return true;
    case HID_KEY_QUOTE: *out_byte = shifted ? '"' : '\''; return true;
    case HID_KEY_TILDE: *out_byte = shifted ? '~' : '`'; return true;
    case HID_KEY_LESS: *out_byte = shifted ? '<' : ','; return true;
    case HID_KEY_GREATER: *out_byte = shifted ? '>' : '.'; return true;
    case HID_KEY_SLASH: *out_byte = shifted ? '?' : '/'; return true;
    case HID_KEY_SPACE:
        *out_byte = ' ';
        return true;
    case HID_KEY_ENTER:
        *out_byte = '\r';
        return true;
    case HID_KEY_DEL:
        *out_byte = '\b';
        return true;
    case HID_KEY_TAB:
        *out_byte = '\t';
        return true;
    case HID_KEY_KEYPAD_0:
    case HID_KEY_KEYPAD_1:
    case HID_KEY_KEYPAD_2:
    case HID_KEY_KEYPAD_3:
    case HID_KEY_KEYPAD_4:
    case HID_KEY_KEYPAD_5:
    case HID_KEY_KEYPAD_6:
    case HID_KEY_KEYPAD_7:
    case HID_KEY_KEYPAD_8:
    case HID_KEY_KEYPAD_9:
        *out_byte = (uint8_t)('0' + (key_code == HID_KEY_KEYPAD_0 ? 0 : key_code - HID_KEY_KEYPAD_1 + 1));
        return true;
    case HID_KEY_KEYPAD_DECIMAL:
        *out_byte = '.';
        return true;
    default:
        return false;
    }
}

bool keyboard_map_key(uint8_t modifier, uint8_t key_code, bool *caps_lock,
                      uint8_t *out_byte)
{
    if (!caps_lock || !out_byte) return false;

    if (key_code == HID_KEY_CAPS_LOCK) {
        *caps_lock = !*caps_lock;
        return false;
    }

    const bool shifted = has_shift(modifier);
    if (has_ctrl(modifier)) {
        switch (key_code) {
        case HID_KEY_H: *out_byte = '\b'; return true;
        case HID_KEY_I: *out_byte = '\t'; return true;
        case HID_KEY_J: *out_byte = '\n'; return true;
        case HID_KEY_M: *out_byte = '\r'; return true;
        case HID_KEY_OPEN_BRACKET: *out_byte = 0x1B; return true;
        default: return false;
        }
    }

    if (key_code >= HID_KEY_A && key_code <= HID_KEY_Z) {
        return map_ascii_key(key_code, shifted != *caps_lock, out_byte);
    }
    return map_ascii_key(key_code, shifted, out_byte);
}
