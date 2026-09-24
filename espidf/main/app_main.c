// SPDX-License-Identifier: GPL-3.0-only
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "sdkconfig.h"
#include "driver/uart.h"
#include "driver/usb_serial_jtag.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "usb/hid_usage_keyboard.h"

#include "kxr60.h"
#include "printer.h"
#include "protocol.h"
#include "usb_keyboard.h"

#define PRINTER_QUEUE_CAPACITY 16384
#define CANDIDATE_CAPACITY 4096
#define UART_RX_CAPACITY 4096
#define UART_TX_CAPACITY 4096

static const char *TAG = "DEVICE_PROTO";
static SemaphoreHandle_t s_tx_mutex;
static SemaphoreHandle_t s_state_mutex;
static uint8_t s_request_id[16];
static uint8_t s_candidate[4096];
static uint32_t s_next_sequence;
static size_t s_candidate_length;
static int64_t s_thinking_started_us;
static unsigned s_thinking_dots;
static bool s_hello_acked;
static enum {
    DEVICE_STATE_EDITING = 0,
    DEVICE_STATE_THINKING,
    DEVICE_STATE_RESPONDING,
    DEVICE_STATE_DRAINING,
    DEVICE_STATE_FAULT,
} s_state = DEVICE_STATE_EDITING;

static void send_frame(uint8_t type, const uint8_t *request_id,
                       const uint8_t *payload, uint16_t length);
static void send_json_state(void);

static esp_err_t emit_keyboard_byte(unsigned char byte)
{
    return printer_enqueue(&byte, 1, 1000);
}

static bool emit_keyboard_event(uint8_t modifier, uint8_t key_code,
                                const uint8_t *output, size_t output_length,
                                bool ctrl_enter)
{
    (void)modifier;
    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (s_state != DEVICE_STATE_EDITING) {
        xSemaphoreGive(s_state_mutex);
        return false;
    }
    if (ctrl_enter) {
        if (!s_candidate_length) {
            xSemaphoreGive(s_state_mutex);
            return false;
        }
        esp_fill_random(s_request_id, sizeof(s_request_id));
        s_state = DEVICE_STATE_THINKING;
        s_thinking_started_us = esp_timer_get_time();
        s_thinking_dots = 0;
        static const uint8_t thinking[] = "thinking";
        if (printer_enqueue(thinking, sizeof(thinking) - 1, 1000) != ESP_OK) {
            s_state = DEVICE_STATE_FAULT;
            xSemaphoreGive(s_state_mutex);
            return false;
        }
        for (size_t offset = 0; offset < s_candidate_length;
             offset += DEVICE_PROTOCOL_MAX_PAYLOAD) {
            const size_t remaining = s_candidate_length - offset;
            const uint16_t chunk = (uint16_t)(remaining > DEVICE_PROTOCOL_MAX_PAYLOAD
                                                   ? DEVICE_PROTOCOL_MAX_PAYLOAD : remaining);
            send_frame(DEVICE_MSG_INPUT_DELTA, s_request_id, s_candidate + offset, chunk);
        }
        const uint16_t submitted_length = (uint16_t)(s_candidate_length <= DEVICE_PROTOCOL_MAX_PAYLOAD
                                                         ? s_candidate_length : 0);
        send_frame(DEVICE_MSG_INPUT_SUBMITTED, s_request_id,
                   submitted_length ? s_candidate : NULL, submitted_length);
        send_json_state();
        xSemaphoreGive(s_state_mutex);
        return false;
    }

    if (key_code == HID_KEY_ENTER) {
        static const uint8_t logical_newline = '\n';
        if (s_candidate_length < sizeof(s_candidate)) {
            s_candidate[s_candidate_length++] = logical_newline;
        }
    } else if (key_code == HID_KEY_DEL || (output_length == 1 && output[0] == '\b')) {
        if (s_candidate_length) --s_candidate_length;
    } else if (output && output_length) {
        if (s_candidate_length + output_length <= sizeof(s_candidate)) {
            memcpy(s_candidate + s_candidate_length, output, output_length);
            s_candidate_length += output_length;
        } else {
            ESP_LOGW(TAG, "local keyboard candidate full");
        }
    }
    xSemaphoreGive(s_state_mutex);
    return true;
}

static bool is_ascii_text(const uint8_t *data, size_t length)
{
    for (size_t i = 0; i < length; ++i) {
        const uint8_t c = data[i];
        if ((c < 0x20 || c > 0x7e) && c != '\r' && c != '\n' && c != '\b') return false;
    }
    return true;
}

static const char *state_name(void)
{
    switch (s_state) {
    case DEVICE_STATE_EDITING: return "editing";
    case DEVICE_STATE_THINKING: return "thinking";
    case DEVICE_STATE_RESPONDING: return "responding";
    case DEVICE_STATE_DRAINING: return "draining";
    case DEVICE_STATE_FAULT: return "fault";
    default: return "unknown";
    }
}

static int transport_write(const uint8_t *data, size_t length)
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    return usb_serial_jtag_write_bytes(data, length, pdMS_TO_TICKS(1000));
#elif CONFIG_ESP_CONSOLE_UART_DEFAULT || CONFIG_ESP_CONSOLE_UART_CUSTOM
    return uart_write_bytes(CONFIG_ESP_CONSOLE_UART_NUM, data, length);
#else
    (void)data; (void)length;
    return -1;
#endif
}

static int transport_read(uint8_t *data, size_t length, uint32_t timeout_ms)
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    return usb_serial_jtag_read_bytes(data, length, pdMS_TO_TICKS(timeout_ms));
#elif CONFIG_ESP_CONSOLE_UART_DEFAULT || CONFIG_ESP_CONSOLE_UART_CUSTOM
    return uart_read_bytes(CONFIG_ESP_CONSOLE_UART_NUM, data, length,
                           pdMS_TO_TICKS(timeout_ms));
#else
    (void)data; (void)length; (void)timeout_ms;
    return -1;
#endif
}

static esp_err_t transport_init(void)
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    const usb_serial_jtag_driver_config_t config = {
        .rx_buffer_size = UART_RX_CAPACITY,
        .tx_buffer_size = UART_TX_CAPACITY,
    };
    return usb_serial_jtag_driver_install(&config);
#elif CONFIG_ESP_CONSOLE_UART_DEFAULT || CONFIG_ESP_CONSOLE_UART_CUSTOM
    const uart_config_t config = {
        .baud_rate = CONFIG_ESP_CONSOLE_UART_BAUDRATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t err = uart_param_config(CONFIG_ESP_CONSOLE_UART_NUM, &config);
    if (err != ESP_OK) return err;
#if CONFIG_ESP_CONSOLE_UART_CUSTOM
    err = uart_set_pin(CONFIG_ESP_CONSOLE_UART_NUM, CONFIG_ESP_CONSOLE_UART_TX_GPIO,
                       CONFIG_ESP_CONSOLE_UART_RX_GPIO, UART_PIN_NO_CHANGE,
                       UART_PIN_NO_CHANGE);
    if (err != ESP_OK) return err;
#endif
    err = uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, UART_RX_CAPACITY,
                              UART_TX_CAPACITY, 0, NULL, 0);
    if (err == ESP_OK) uart_flush_input(CONFIG_ESP_CONSOLE_UART_NUM);
    return err;
#else
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

static void send_frame(uint8_t type, const uint8_t *request_id,
                       const uint8_t *payload, uint16_t length)
{
    uint8_t encoded[DEVICE_PROTOCOL_MAX_ENCODED];
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    const uint32_t sequence = s_next_sequence++;
    const size_t encoded_length = device_frame_encode(type, 0, request_id,
                                                       sequence, payload, length,
                                                       encoded, sizeof(encoded));
    if (!encoded_length) {
        xSemaphoreGive(s_tx_mutex);
        return;
    }
    (void)transport_write(encoded, encoded_length);
    xSemaphoreGive(s_tx_mutex);
}

static void send_json_state(void)
{
    printer_status_t printer;
    printer_get_status(&printer);
    char json[DEVICE_PROTOCOL_MAX_PAYLOAD];
    const int length = snprintf(json, sizeof(json),
                                "{\"state\":\"%s\",\"candidate_length\":%u,"
                                "\"queued_bytes\":%u,\"capacity\":%u,"
                                "\"printed_bytes\":%" PRIu32 ",\"faulted\":%s}",
                                state_name(), (unsigned)s_candidate_length,
                                (unsigned)printer.queued_bytes, (unsigned)printer.capacity,
                                printer.bytes_printed, printer.faulted ? "true" : "false");
    if (length > 0 && length < (int)sizeof(json)) {
        send_frame(DEVICE_MSG_STATE, NULL, (const uint8_t *)json, (uint16_t)length);
    }
}

static void send_error(const char *code, const uint8_t *request_id)
{
    char json[160];
    const int length = snprintf(json, sizeof(json), "{\"error\":\"%s\"}", code);
    if (length > 0 && length < (int)sizeof(json)) {
        send_frame(DEVICE_MSG_ERROR, request_id, (const uint8_t *)json, (uint16_t)length);
    }
}

static void send_credit(void)
{
    printer_status_t printer;
    printer_get_status(&printer);
    char json[128];
    const int length = snprintf(json, sizeof(json),
                                "{\"credit\":%u,\"queued\":%u}",
                                (unsigned)(printer.capacity - printer.queued_bytes),
                                (unsigned)printer.queued_bytes);
    if (length > 0 && length < (int)sizeof(json)) {
        send_frame(DEVICE_MSG_PRINT_CREDIT, s_request_id,
                   (const uint8_t *)json, (uint16_t)length);
    }
}

static void send_hello(void)
{
    static const char hello[] =
        "{\"device\":\"kxr530-esp32s3\",\"protocol\":1,"
        "\"charset\":\"ascii\",\"printer_capacity\":16384}";
    send_frame(DEVICE_MSG_HELLO, NULL, (const uint8_t *)hello, sizeof(hello) - 1);
}

static esp_err_t enqueue_text(const uint8_t *data, size_t length)
{
    if (!is_ascii_text(data, length)) return ESP_ERR_INVALID_ARG;
    return printer_enqueue(data, length, 1000);
}

static void accept_input_delta(const device_frame_t *frame)
{
    if (s_state != DEVICE_STATE_EDITING) {
        send_error("busy", frame->request_id);
        return;
    }
    if (!is_ascii_text(frame->payload, frame->payload_length) ||
        s_candidate_length + frame->payload_length > CANDIDATE_CAPACITY) {
        send_error("input_invalid_or_full", frame->request_id);
        return;
    }
    if (enqueue_text(frame->payload, frame->payload_length) != ESP_OK) {
        send_error("printer_queue_full", frame->request_id);
        return;
    }
    if (s_candidate_length + frame->payload_length > sizeof(s_candidate)) {
        send_error("input_invalid_or_full", frame->request_id);
        return;
    }
    memcpy(s_candidate + s_candidate_length, frame->payload, frame->payload_length);
    s_candidate_length += frame->payload_length;
    send_frame(DEVICE_MSG_INPUT_DELTA, NULL, frame->payload, frame->payload_length);
    send_json_state();
}

static void accept_submit(const device_frame_t *frame)
{
    if (s_state != DEVICE_STATE_EDITING || !s_candidate_length) {
        send_error(s_state == DEVICE_STATE_EDITING ? "empty_input" : "busy",
                   frame->request_id);
        return;
    }
    memcpy(s_request_id, frame->request_id, sizeof(s_request_id));
    s_state = DEVICE_STATE_THINKING;
    s_thinking_started_us = esp_timer_get_time();
    s_thinking_dots = 0;
    static const uint8_t thinking[] = "thinking";
    if (enqueue_text(thinking, sizeof(thinking) - 1) != ESP_OK) {
        s_state = DEVICE_STATE_FAULT;
        send_error("printer_queue_full", s_request_id);
        return;
    }
    send_frame(DEVICE_MSG_INPUT_SUBMITTED, s_request_id, NULL, 0);
    send_json_state();
}

static void accept_print_data(const device_frame_t *frame)
{
    if (!is_ascii_text(frame->payload, frame->payload_length)) {
        send_error("request_or_charset_invalid", frame->request_id);
        return;
    }
    if (s_state == DEVICE_STATE_EDITING) {
        memcpy(s_request_id, frame->request_id, sizeof(s_request_id));
        s_state = DEVICE_STATE_RESPONDING;
    } else if (s_state != DEVICE_STATE_THINKING && s_state != DEVICE_STATE_RESPONDING) {
        send_error("no_active_request", frame->request_id);
        return;
    } else if (memcmp(frame->request_id, s_request_id, sizeof(s_request_id)) != 0) {
        send_error("request_or_charset_invalid", frame->request_id);
        return;
    }
    if (s_state == DEVICE_STATE_THINKING) {
        const unsigned seconds = (unsigned)((esp_timer_get_time() - s_thinking_started_us) / 1000000);
        char prefix[48];
        const int prefix_length = snprintf(prefix, sizeof(prefix), " %us\r\n", seconds);
        if (prefix_length <= 0 || enqueue_text((const uint8_t *)prefix, (size_t)prefix_length) != ESP_OK) {
            s_state = DEVICE_STATE_FAULT;
            send_error("printer_queue_full", s_request_id);
            return;
        }
        s_state = DEVICE_STATE_RESPONDING;
    }
    if (enqueue_text(frame->payload, frame->payload_length) != ESP_OK) {
        send_error("printer_queue_full", s_request_id);
        return;
    }
    send_frame(DEVICE_MSG_PRINT_PROGRESS, s_request_id, NULL, 0);
    send_credit();
}

static void accept_print_end(const device_frame_t *frame)
{
    if ((s_state != DEVICE_STATE_THINKING && s_state != DEVICE_STATE_RESPONDING) ||
        memcmp(frame->request_id, s_request_id, sizeof(s_request_id)) != 0) {
        send_error("no_active_request", frame->request_id);
        return;
    }
    if (s_state == DEVICE_STATE_THINKING) {
        const unsigned seconds = (unsigned)((esp_timer_get_time() - s_thinking_started_us) / 1000000);
        char prefix[48];
        const int prefix_length = snprintf(prefix, sizeof(prefix), " %us\r\n", seconds);
        if (prefix_length <= 0 || enqueue_text((const uint8_t *)prefix, (size_t)prefix_length) != ESP_OK) {
            s_state = DEVICE_STATE_FAULT;
            send_error("printer_queue_full", s_request_id);
            return;
        }
        s_state = DEVICE_STATE_RESPONDING;
    }
    static const uint8_t ending[] = "\r\n";
    if (enqueue_text(ending, sizeof(ending) - 1) != ESP_OK) {
        s_state = DEVICE_STATE_FAULT;
        send_error("printer_queue_full", s_request_id);
        return;
    }
    s_state = DEVICE_STATE_DRAINING;
    send_json_state();
}

static bool handle_frame(const device_frame_t *frame, void *arg)
{
    (void)arg;
    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    switch (frame->type) {
    case DEVICE_MSG_HELLO_ACK:
        s_hello_acked = true;
        send_json_state();
        send_credit();
        break;
    case DEVICE_MSG_INPUT_DELTA:
        accept_input_delta(frame);
        break;
    case DEVICE_MSG_INPUT_SUBMITTED:
        accept_submit(frame);
        break;
    case DEVICE_MSG_PRINT_DATA:
        accept_print_data(frame);
        break;
    case DEVICE_MSG_PRINT_END:
        accept_print_end(frame);
        break;
    case DEVICE_MSG_RECOVER: {
        const esp_err_t err = printer_recover();
        if (err == ESP_OK) s_state = DEVICE_STATE_EDITING;
        else s_state = DEVICE_STATE_FAULT;
        send_json_state();
        if (err != ESP_OK) send_error("recover_failed", frame->request_id);
        break;
    }
    case DEVICE_MSG_CANCEL:
        if (s_state == DEVICE_STATE_THINKING || s_state == DEVICE_STATE_RESPONDING ||
            s_state == DEVICE_STATE_DRAINING) {
            printer_flush();
            s_state = DEVICE_STATE_EDITING;
            s_candidate_length = 0;
            memset(s_candidate, 0, sizeof(s_candidate));
            memset(s_request_id, 0, sizeof(s_request_id));
            send_json_state();
        } else {
            send_error("no_active_request", frame->request_id);
        }
        break;
    case DEVICE_MSG_PING:
        send_frame(DEVICE_MSG_PONG, frame->request_id, frame->payload, frame->payload_length);
        break;
    default:
        send_error("unknown_message", frame->request_id);
        break;
    }
    xSemaphoreGive(s_state_mutex);
    return true;
}

static void protocol_task(void *arg)
{
    (void)arg;
    device_decoder_t decoder;
    device_decoder_init(&decoder);
    send_hello();
    uint8_t buffer[128];
    int64_t last_status = 0;
    int64_t last_hello = esp_timer_get_time();
    for (;;) {
        const int count = transport_read(buffer, sizeof(buffer), 50);
        for (int i = 0; i < count; ++i) {
            device_decoder_feed(&decoder, buffer[i], handle_frame, NULL);
        }
        const int64_t now = esp_timer_get_time();
        xSemaphoreTake(s_state_mutex, portMAX_DELAY);
        if (now - last_hello >= 2000000) {
            send_hello();
            last_hello = now;
        }
        printer_status_t printer_status;
        printer_get_status(&printer_status);
        if (printer_status.faulted && s_state != DEVICE_STATE_FAULT) {
            s_state = DEVICE_STATE_FAULT;
            send_json_state();
        }
        // Keep the thinking marker quiet after the initial "thinking" text.
        // The host receives state/progress frames and the printer must not
        // generate an endless stream of dots while an Agent is unavailable.
        if (s_state == DEVICE_STATE_DRAINING) {
            if (printer_status.queued_bytes == 0) {
                s_state = DEVICE_STATE_EDITING;
                s_candidate_length = 0;
                memset(s_candidate, 0, sizeof(s_candidate));
                memset(s_request_id, 0, sizeof(s_request_id));
                send_json_state();
            }
        }
        xSemaphoreGive(s_state_mutex);
        if (now - last_status >= 500000) {
            send_credit();
            last_status = now;
        }
    }
}

void app_main(void)
{
    s_tx_mutex = xSemaphoreCreateMutex();
    s_state_mutex = xSemaphoreCreateMutex();
    if (!s_tx_mutex || !s_state_mutex) return;
    if (transport_init() != ESP_OK) {
        ESP_LOGE(TAG, "transport initialization failed");
        return;
    }
    if (printer_start(PRINTER_QUEUE_CAPACITY) != ESP_OK) {
        ESP_LOGE(TAG, "printer initialization failed");
        return;
    }
    const esp_err_t keyboard_err = usb_keyboard_start(emit_keyboard_byte, emit_keyboard_event);
    if (keyboard_err != ESP_OK) {
        ESP_LOGE(TAG, "USB HID keyboard unavailable: %s", esp_err_to_name(keyboard_err));
    } else {
        ESP_LOGI(TAG, "USB HID keyboard bridge ready");
    }
    esp_register_shutdown_handler(kxr60_release_lines);
    if (xTaskCreate(protocol_task, "protocol", 6144, NULL, 7, NULL) != pdPASS) {
        printer_stop();
        ESP_LOGE(TAG, "protocol task creation failed");
    }
}
