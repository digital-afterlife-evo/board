// SPDX-License-Identifier: GPL-3.0-only
#include "protocol.h"

#include <string.h>

static void put_u16(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
}

static void put_u32(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
    p[2] = (uint8_t)(value >> 16);
    p[3] = (uint8_t)(value >> 24);
}

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

uint32_t device_crc32(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)-(int)(crc & 1u));
        }
    }
    return ~crc;
}

static size_t cobs_encode(const uint8_t *input, size_t length,
                          uint8_t *output, size_t capacity)
{
    if (!capacity) return 0;
    size_t read = 0;
    size_t write = 1;
    size_t code = 1;
    size_t code_index = 0;
    while (read < length) {
        if (input[read] == 0) {
            if (code_index >= capacity) return 0;
            output[code_index] = (uint8_t)code;
            code_index = write++;
            code = 1;
            ++read;
        } else {
            if (write >= capacity) return 0;
            output[write++] = input[read++];
            ++code;
            if (code == 0xFF) {
                output[code_index] = 0xFF;
                code_index = write++;
                code = 1;
            }
        }
    }
    if (code_index >= capacity) return 0;
    output[code_index] = (uint8_t)code;
    return write;
}

static size_t cobs_decode(const uint8_t *input, size_t length,
                          uint8_t *output, size_t capacity)
{
    size_t read = 0;
    size_t write = 0;
    while (read < length) {
        uint8_t code = input[read++];
        if (code == 0 || read + code - 1 > length) return 0;
        for (uint8_t i = 1; i < code; ++i) {
            if (write >= capacity) return 0;
            output[write++] = input[read++];
        }
        if (code != 0xFF && read < length) {
            if (write >= capacity) return 0;
            output[write++] = 0;
        }
    }
    return write;
}

size_t device_frame_encode(uint8_t type, uint8_t flags, const uint8_t request_id[16],
                           uint32_t sequence, const uint8_t *payload,
                           uint16_t payload_length, uint8_t *out, size_t out_capacity)
{
    if (!out || payload_length > DEVICE_PROTOCOL_MAX_PAYLOAD ||
        (payload_length && !payload)) return 0;
    const size_t raw_length = 1 + 1 + 1 + 16 + 4 + 2 + payload_length + 4;
    if (raw_length > DEVICE_PROTOCOL_MAX_DECODED || out_capacity < 2) return 0;

    uint8_t raw[DEVICE_PROTOCOL_MAX_DECODED];
    size_t pos = 0;
    raw[pos++] = DEVICE_PROTOCOL_VERSION;
    raw[pos++] = type;
    raw[pos++] = flags;
    if (request_id) memcpy(raw + pos, request_id, 16);
    else memset(raw + pos, 0, 16);
    pos += 16;
    put_u32(raw + pos, sequence); pos += 4;
    put_u16(raw + pos, payload_length); pos += 2;
    if (payload_length) memcpy(raw + pos, payload, payload_length);
    pos += payload_length;
    put_u32(raw + pos, device_crc32(raw, pos)); pos += 4;

    size_t encoded = cobs_encode(raw, pos, out, out_capacity - 1);
    if (!encoded || encoded + 1 > out_capacity) return 0;
    out[encoded] = 0;
    return encoded + 1;
}

void device_decoder_init(device_decoder_t *decoder)
{
    if (decoder) memset(decoder, 0, sizeof(*decoder));
}

bool device_decoder_feed(device_decoder_t *decoder, uint8_t byte,
                         device_frame_callback_t callback, void *arg)
{
    if (!decoder || !callback) return false;
    if (byte != 0) {
        if (decoder->encoded_length >= sizeof(decoder->encoded)) {
            decoder->encoded_length = 0;
            return false;
        }
        decoder->encoded[decoder->encoded_length++] = byte;
        return false;
    }
    if (!decoder->encoded_length) return false;
    const size_t decoded_length = cobs_decode(decoder->encoded, decoder->encoded_length,
                                               decoder->decoded, sizeof(decoder->decoded));
    decoder->encoded_length = 0;
    if (decoded_length < 1 + 1 + 1 + 16 + 4 + 2 + 4) return false;
    const uint16_t payload_length = get_u16(decoder->decoded + 23);
    const size_t expected = 1 + 1 + 1 + 16 + 4 + 2 + payload_length + 4;
    if (decoder->decoded[0] != DEVICE_PROTOCOL_VERSION || expected != decoded_length ||
        payload_length > DEVICE_PROTOCOL_MAX_PAYLOAD) return false;
    const uint32_t expected_crc = get_u32(decoder->decoded + decoded_length - 4);
    if (device_crc32(decoder->decoded, decoded_length - 4) != expected_crc) return false;

    device_frame_t frame = {
        .version = decoder->decoded[0],
        .type = decoder->decoded[1],
        .flags = decoder->decoded[2],
        .sequence = get_u32(decoder->decoded + 19),
        .payload_length = payload_length,
        .payload = decoder->decoded + 25,
    };
    memcpy(frame.request_id, decoder->decoded + 3, 16);
    return callback(&frame, arg);
}
