// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DEVICE_PROTOCOL_VERSION 1
#define DEVICE_PROTOCOL_MAX_PAYLOAD 480
#define DEVICE_PROTOCOL_MAX_DECODED 512
#define DEVICE_PROTOCOL_MAX_ENCODED 560

typedef enum {
    DEVICE_MSG_HELLO = 1,
    DEVICE_MSG_HELLO_ACK = 2,
    DEVICE_MSG_STATE = 3,
    DEVICE_MSG_INPUT_DELTA = 4,
    DEVICE_MSG_INPUT_SUBMITTED = 5,
    DEVICE_MSG_PRINT_DATA = 6,
    DEVICE_MSG_PRINT_END = 7,
    DEVICE_MSG_PRINT_CREDIT = 8,
    DEVICE_MSG_PRINT_PROGRESS = 9,
    DEVICE_MSG_RECOVER = 10,
    DEVICE_MSG_CANCEL = 11,
    DEVICE_MSG_ERROR = 12,
    DEVICE_MSG_PING = 13,
    DEVICE_MSG_PONG = 14,
} device_msg_type_t;

typedef struct {
    uint8_t version;
    uint8_t type;
    uint8_t flags;
    uint8_t request_id[16];
    uint32_t sequence;
    uint16_t payload_length;
    const uint8_t *payload;
} device_frame_t;

typedef struct {
    uint8_t encoded[DEVICE_PROTOCOL_MAX_ENCODED];
    size_t encoded_length;
    uint8_t decoded[DEVICE_PROTOCOL_MAX_DECODED];
} device_decoder_t;

typedef bool (*device_frame_callback_t)(const device_frame_t *frame, void *arg);

void device_decoder_init(device_decoder_t *decoder);
bool device_decoder_feed(device_decoder_t *decoder, uint8_t byte,
                         device_frame_callback_t callback, void *arg);

size_t device_frame_encode(uint8_t type, uint8_t flags, const uint8_t request_id[16],
                           uint32_t sequence, const uint8_t *payload,
                           uint16_t payload_length, uint8_t *out, size_t out_capacity);

uint32_t device_crc32(const uint8_t *data, size_t length);
