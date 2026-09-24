// SPDX-License-Identifier: GPL-3.0-only
#include "printer.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "kxr60.h"

static StreamBufferHandle_t s_stream;
static SemaphoreHandle_t s_status_mutex;
static SemaphoreHandle_t s_kxr_mutex;
static printer_status_t s_status;
static TaskHandle_t s_task;

static void set_fault(esp_err_t err)
{
    xSemaphoreTake(s_status_mutex, portMAX_DELAY);
    s_status.faulted = true;
    s_status.last_error = err;
    xSemaphoreGive(s_status_mutex);
}

static void printer_task(void *arg)
{
    (void)arg;
    xSemaphoreTake(s_kxr_mutex, portMAX_DELAY);
    esp_err_t err = kxr60_init();
    xSemaphoreGive(s_kxr_mutex);
    xSemaphoreTake(s_status_mutex, portMAX_DELAY);
    s_status.initialized = err == ESP_OK;
    s_status.faulted = err != ESP_OK;
    s_status.last_error = err;
    xSemaphoreGive(s_status_mutex);

    uint8_t byte;
    for (;;) {
        if (xStreamBufferReceive(s_stream, &byte, 1, portMAX_DELAY) != 1) continue;
        xSemaphoreTake(s_kxr_mutex, portMAX_DELAY);
        err = kxr60_send_byte(byte);
        xSemaphoreGive(s_kxr_mutex);
        xSemaphoreTake(s_status_mutex, portMAX_DELAY);
        if (err == ESP_OK) {
            ++s_status.bytes_printed;
            s_status.last_error = ESP_OK;
        } else {
            s_status.faulted = true;
            s_status.last_error = err;
        }
        xSemaphoreGive(s_status_mutex);
        if (err != ESP_OK) {
            set_fault(err);
            xStreamBufferReset(s_stream);
        }
    }
}

esp_err_t printer_start(size_t capacity)
{
    if (capacity < 128 || s_task) return ESP_ERR_INVALID_STATE;
    s_stream = xStreamBufferCreate(capacity, 1);
    s_status_mutex = xSemaphoreCreateMutex();
    s_kxr_mutex = xSemaphoreCreateMutex();
    if (!s_stream || !s_status_mutex || !s_kxr_mutex) return ESP_ERR_NO_MEM;
    s_status = (printer_status_t){.capacity = capacity, .last_error = ESP_ERR_INVALID_STATE};
    if (xTaskCreate(printer_task, "printer", 4096, NULL, 8, &s_task) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(2000);
    for (;;) {
        printer_status_t status;
        printer_get_status(&status);
        if (status.initialized || status.faulted || xTaskGetTickCount() >= deadline) {
            return status.initialized ? ESP_OK : status.last_error;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

esp_err_t printer_enqueue(const uint8_t *data, size_t length, uint32_t timeout_ms)
{
    if (!data && length) return ESP_ERR_INVALID_ARG;
    printer_status_t status;
    printer_get_status(&status);
    if (!status.initialized || status.faulted) return ESP_ERR_INVALID_STATE;
    const size_t sent = xStreamBufferSend(s_stream, data, length, pdMS_TO_TICKS(timeout_ms));
    return sent == length ? ESP_OK : ESP_ERR_TIMEOUT;
}

void printer_get_status(printer_status_t *status)
{
    if (!status) return;
    if (!s_status_mutex) {
        *status = (printer_status_t){0};
        return;
    }
    xSemaphoreTake(s_status_mutex, portMAX_DELAY);
    *status = s_status;
    status->queued_bytes = xStreamBufferBytesAvailable(s_stream);
    xSemaphoreGive(s_status_mutex);
}

esp_err_t printer_recover(void)
{
    if (!s_status_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_status_mutex, portMAX_DELAY);
    xSemaphoreTake(s_kxr_mutex, portMAX_DELAY);
    esp_err_t err = kxr60_init();
    xSemaphoreGive(s_kxr_mutex);
    s_status.initialized = err == ESP_OK;
    s_status.faulted = err != ESP_OK;
    s_status.last_error = err;
    xSemaphoreGive(s_status_mutex);
    return err;
}

void printer_flush(void)
{
    if (s_stream) xStreamBufferReset(s_stream);
}

void printer_stop(void)
{
    if (s_stream) xStreamBufferReset(s_stream);
    kxr60_release_lines();
    xSemaphoreTake(s_status_mutex, portMAX_DELAY);
    s_status.initialized = false;
    xSemaphoreGive(s_status_mutex);
    if (s_task) vTaskDelete(s_task);
    s_task = NULL;
}
