// SPDX-License-Identifier: GPL-3.0-only
#include "usb_keyboard.h"

#include <stddef.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "usb/hid_host.h"
#include "usb/hid_usage_keyboard.h"
#include "usb/usb_host.h"

#include "keyboard_map.h"

static const char *TAG = "USB_KBD";
static QueueHandle_t s_event_queue;
static QueueHandle_t s_key_queue;
static usb_keyboard_emit_fn_t s_emit;
static usb_keyboard_event_fn_t s_event;
static bool s_caps_lock;

typedef struct {
    hid_host_device_handle_t handle;
    hid_host_driver_event_t event;
} hid_event_t;

typedef struct {
    uint8_t modifier;
    uint8_t key_code;
    uint8_t output[2];
    uint8_t output_length;
    bool ctrl_enter;
} keyboard_action_t;

static void keyboard_action_task(void *arg)
{
    (void)arg;
    keyboard_action_t action;
    while (xQueueReceive(s_key_queue, &action, portMAX_DELAY) == pdTRUE) {
        bool allow_output = true;
        if (s_event) {
            allow_output = s_event(action.modifier, action.key_code,
                                   action.output, action.output_length,
                                   action.ctrl_enter);
        }
        if (!allow_output) continue;
        for (size_t i = 0; i < action.output_length; ++i) {
            esp_err_t err = s_emit(action.output[i]);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "key 0x%02x -> 0x%02x failed: %s",
                         action.key_code, action.output[i], esp_err_to_name(err));
                break;
            }
        }
    }
}

static void update_keyboard_leds(hid_host_device_handle_t device)
{
    uint8_t report = s_caps_lock ? (1u << 1) : 0;
    esp_err_t err = hid_class_request_set_report(device, HID_REPORT_TYPE_OUTPUT,
                                                 0, &report, sizeof(report));
    if (err != ESP_OK) {
        // A few keyboards expose a one-byte report with ID 1 instead of the
        // boot-protocol ID 0. Try that form before reporting a real failure.
        err = hid_class_request_set_report(device, HID_REPORT_TYPE_OUTPUT,
                                           1, &report, sizeof(report));
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "keyboard LED report failed: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "keyboard LEDs: caps_lock=%s", s_caps_lock ? "on" : "off");
    }
}

static void report_key(hid_host_device_handle_t device, uint8_t modifier, uint8_t key_code)
{
    if (key_code == HID_KEY_ENTER &&
        (modifier & (HID_LEFT_CONTROL | HID_RIGHT_CONTROL)) != 0) {
        const keyboard_action_t action = {
            .modifier = modifier,
            .key_code = key_code,
            .output_length = 0,
            .ctrl_enter = true,
        };
        if (xQueueSend(s_key_queue, &action, 0) != pdTRUE) {
            ESP_LOGW(TAG, "keyboard action queue full");
        }
        return;
    }

    uint8_t output[2];
    size_t output_length = 0;
    uint8_t byte;
    if (keyboard_map_key(modifier, key_code, &s_caps_lock, &byte)) {
        // KX-R530 behaves as LF = advance paper and CR = return carriage.
        // Send them in that order so Enter advances exactly one line.
        if (key_code == HID_KEY_ENTER) {
            output[0] = '\n';
            output[1] = '\r';
            output_length = 2;
        } else {
            output[0] = byte;
            output_length = 1;
        }
        keyboard_action_t action = {
            .modifier = modifier,
            .key_code = key_code,
            .output_length = (uint8_t)output_length,
            .ctrl_enter = false,
        };
        memcpy(action.output, output, output_length);
        if (xQueueSend(s_key_queue, &action, 0) != pdTRUE) {
            ESP_LOGW(TAG, "keyboard action queue full");
        }
    } else if (key_code == HID_KEY_CAPS_LOCK) {
        update_keyboard_leds(device);
    } else if (key_code != HID_KEY_CAPS_LOCK) {
        ESP_LOGI(TAG, "unmapped HID key 0x%02x", key_code);
    }
}

static bool key_found(const uint8_t *keys, uint8_t key, size_t length)
{
    for (size_t i = 0; i < length; ++i) {
        if (keys[i] == key) return true;
    }
    return false;
}

static void keyboard_report_callback(hid_host_device_handle_t device,
                                     const uint8_t *data, size_t length)
{
    if (length < sizeof(hid_keyboard_input_report_boot_t)) return;

    const hid_keyboard_input_report_boot_t *report =
        (const hid_keyboard_input_report_boot_t *)data;
    static uint8_t previous[HID_KEYBOARD_KEY_MAX];

    for (size_t i = 0; i < HID_KEYBOARD_KEY_MAX; ++i) {
        if (previous[i] > HID_KEY_ERROR_UNDEFINED &&
            !key_found(report->key, previous[i], HID_KEYBOARD_KEY_MAX)) {
            ESP_LOGD(TAG, "released HID key 0x%02x", previous[i]);
        }
        if (report->key[i] > HID_KEY_ERROR_UNDEFINED &&
            !key_found(previous, report->key[i], HID_KEYBOARD_KEY_MAX)) {
            report_key(device, report->modifier.val, report->key[i]);
        }
    }
    memcpy(previous, report->key, HID_KEYBOARD_KEY_MAX);
}

static void hid_interface_callback(hid_host_device_handle_t device,
                                   hid_host_interface_event_t event, void *arg)
{
    (void)arg;
    hid_host_dev_params_t params;
    ESP_ERROR_CHECK(hid_host_device_get_params(device, &params));

    switch (event) {
    case HID_HOST_INTERFACE_EVENT_INPUT_REPORT: {
        uint8_t data[64];
        size_t length = 0;
        ESP_ERROR_CHECK(hid_host_device_get_raw_input_report_data(device, data,
                                                                  sizeof(data), &length));
        if (params.proto == HID_PROTOCOL_KEYBOARD &&
            params.sub_class == HID_SUBCLASS_BOOT_INTERFACE) {
            keyboard_report_callback(device, data, length);
        }
        break;
    }
    case HID_HOST_INTERFACE_EVENT_DISCONNECTED:
        ESP_LOGI(TAG, "keyboard disconnected");
        ESP_ERROR_CHECK(hid_host_device_close(device));
        break;
    default:
        break;
    }
}

static void hid_device_event(hid_host_device_handle_t device,
                             hid_host_driver_event_t event)
{
    if (event != HID_HOST_DRIVER_EVENT_CONNECTED) return;

    hid_host_dev_params_t params;
    ESP_ERROR_CHECK(hid_host_device_get_params(device, &params));
    if (params.proto != HID_PROTOCOL_KEYBOARD ||
        params.sub_class != HID_SUBCLASS_BOOT_INTERFACE) {
        ESP_LOGI(TAG, "ignoring non-boot HID device");
        return;
    }

    const hid_host_device_config_t config = {
        .callback = hid_interface_callback,
        .callback_arg = NULL,
    };
    ESP_ERROR_CHECK(hid_host_device_open(device, &config));
    ESP_ERROR_CHECK(hid_class_request_set_protocol(device, HID_REPORT_PROTOCOL_BOOT));
    ESP_ERROR_CHECK(hid_class_request_set_idle(device, 0, 0));
    ESP_ERROR_CHECK(hid_host_device_start(device));
    update_keyboard_leds(device);
    ESP_LOGI(TAG, "USB HID keyboard connected");
}

static void hid_event_task(void *arg)
{
    (void)arg;
    hid_event_t event;
    while (xQueueReceive(s_event_queue, &event, portMAX_DELAY) == pdTRUE) {
        hid_device_event(event.handle, event.event);
    }
}

static void hid_device_callback(hid_host_device_handle_t device,
                                hid_host_driver_event_t event, void *arg)
{
    (void)arg;
    const hid_event_t queued = {.handle = device, .event = event};
    if (xQueueSend(s_event_queue, &queued, 0) != pdTRUE) {
        ESP_LOGW(TAG, "HID event queue full");
    }
}

static void usb_host_task(void *arg)
{
    TaskHandle_t waiting_task = (TaskHandle_t)arg;
    const usb_host_config_t config = {
        .skip_phy_setup = false,
        .intr_flags = ESP_INTR_FLAG_LOWMED,
    };
    ESP_ERROR_CHECK(usb_host_install(&config));
    xTaskNotifyGive(waiting_task);
    while (true) {
        uint32_t flags;
        usb_host_lib_handle_events(portMAX_DELAY, &flags);
        if (flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) break;
    }
    vTaskDelete(NULL);
}

esp_err_t usb_keyboard_start(usb_keyboard_emit_fn_t emit, usb_keyboard_event_fn_t event)
{
    if (!emit) return ESP_ERR_INVALID_ARG;
    s_emit = emit;
    s_event = event;
    s_event_queue = xQueueCreate(8, sizeof(hid_event_t));
    s_key_queue = xQueueCreate(1024, sizeof(keyboard_action_t));
    if (!s_event_queue || !s_key_queue) return ESP_ERR_NO_MEM;

    BaseType_t created = xTaskCreate(keyboard_action_task, "kbd_actions", 4096,
                                     NULL, 6, NULL);
    if (created != pdTRUE) return ESP_ERR_NO_MEM;

    created = xTaskCreate(usb_host_task, "usb_host", 4096,
                                     xTaskGetCurrentTaskHandle(), 2, NULL);
    if (created != pdTRUE) return ESP_ERR_NO_MEM;
    if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000)) == 0) return ESP_ERR_TIMEOUT;

    created = xTaskCreate(hid_event_task, "hid_events", 4096, NULL, 5, NULL);
    if (created != pdTRUE) return ESP_ERR_NO_MEM;

    const hid_host_driver_config_t config = {
        .create_background_task = true,
        .task_priority = 5,
        .stack_size = 4096,
        .core_id = 0,
        .callback = hid_device_callback,
        .callback_arg = NULL,
    };
    return hid_host_install(&config);
}
