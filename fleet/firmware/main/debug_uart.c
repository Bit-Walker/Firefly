#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "debug_uart.h"
#include "wave.h"
#include "debug_i2c.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_timer.h"

// This profile is only for the verified, directly wired A/B bench. Never drive
// the waveform, I2C, SPI, boot or reset pins from the UART implementation.
#define RING_SIZE 128
typedef struct { uint32_t seq; int port, size; int64_t us; unsigned char data[128]; } frame_t;
static frame_t ring[RING_SIZE];
static uint32_t seq, acknowledged, dropped, uart_errors;
static SemaphoreHandle_t mutex;
static QueueHandle_t events[2];
static char identity[32], session[25], ended_session[25], last_tx[25], tx_error[96], error[96];
static bool installed[2], is_probe;
static int64_t deadline;

static const char *str(cJSON *obj, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}

static void stop_locked(void) {
    wave_stop();
    debug_i2c_stop();
    if (!session[0] && !installed[0] && !installed[1]) return;
    for (int i = 0; i < 2; i++) {
        if (installed[i]) uart_driver_delete(i + 1);
        installed[i] = false;
        events[i] = NULL;
    }
    // TX4 must stop driving on close, timeout or disconnect. RX5/RX8 remain inputs.
    gpio_config_t pins = {.pin_bit_mask = (1ULL << 4) | (1ULL << 5) | (1ULL << 8), .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE, .intr_type = GPIO_INTR_DISABLE};
    gpio_config(&pins);
    session[0] = 0;
    deadline = 0;
}

static esp_err_t install_port(int port, int tx, int rx) {
    gpio_pullup_en(rx);
    uart_config_t cfg = {.baud_rate = 115200, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE, .stop_bits = UART_STOP_BITS_1, .flow_ctrl = UART_HW_FLOWCTRL_DISABLE, .source_clk = UART_SCLK_DEFAULT};
    esp_err_t result = uart_param_config(port, &cfg);
    if (result != ESP_OK) return result;
    result = uart_driver_install(port, 4096, 0, 10, &events[port - 1], 0);
    if (result != ESP_OK) return result;
    installed[port - 1] = true;
    result = uart_set_pin(port, tx, rx, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (result == ESP_OK) gpio_pullup_en(rx);
    return result;
}

static void reader(void *unused) {
    while (true) {
        xSemaphoreTake(mutex, portMAX_DELAY);
        debug_i2c_tick();
        if (session[0] && esp_timer_get_time() >= deadline) {
            snprintf(ended_session, sizeof(ended_session), "%s", session);
            snprintf(error, sizeof(error), "Lease expired; start a new session");
            stop_locked();
        }
        for (int i = 0; i < 2; i++) {
            if (!installed[i]) continue;
            uart_event_t event;
            while (xQueueReceive(events[i], &event, 0)) {
                if (event.type == UART_FIFO_OVF || event.type == UART_BUFFER_FULL || event.type == UART_PARITY_ERR || event.type == UART_FRAME_ERR) uart_errors++;
            }
            unsigned char bytes[128];
            // Bound work to avoid starving management and watchdog tasks.
            for (int n = 0; n < 8; n++) {
                int length = uart_read_bytes(i + 1, bytes, sizeof(bytes), 0);
                if (length <= 0) break;
                uint32_t next = ++seq;
                frame_t *f = &ring[next % RING_SIZE];
                if (f->seq > acknowledged) dropped++;
                *f = (frame_t){.seq = next, .port = i + 1, .size = length, .us = esp_timer_get_time()};
                memcpy(f->data, bytes, length);
            }
        }
        xSemaphoreGive(mutex);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

esp_err_t debug_uart_init(const char *device_id) {
    snprintf(identity, sizeof(identity), "%s", device_id);
    // All inter-board signal pins 4..14 are high impedance before any Wi-Fi or
    // management activity, including on OTA reboot. Leave UART0/USB untouched.
    gpio_config_t pins = {.pin_bit_mask = ((1ULL << 15) - 1) & ~((1ULL << 4) - 1), .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE, .intr_type = GPIO_INTR_DISABLE};
    esp_err_t result = gpio_config(&pins);
    if (result != ESP_OK) return result;
    result = wave_init();
    if (result != ESP_OK) return result;
    mutex = xSemaphoreCreateMutex();
    if (!mutex) return ESP_ERR_NO_MEM;
    return xTaskCreate(reader, "debug_uart", 4096, NULL, 3, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void debug_uart_snapshot(cJSON *body) {
    if (!mutex) return;
    xSemaphoreTake(mutex, portMAX_DELAY);
    cJSON *s = cJSON_AddObjectToObject(body, "debug");
    wave_snapshot(s);
    debug_i2c_snapshot(s);
    cJSON_AddStringToObject(s, "session", session[0] ? session : ended_session);
    cJSON_AddStringToObject(s, "state", session[0] ? "active" : ended_session[0] ? "expired" : "idle");
    cJSON_AddStringToObject(s, "error", error);
    cJSON_AddStringToObject(s, "tx_id", last_tx);
    cJSON_AddStringToObject(s, "tx_error", tx_error);
    cJSON_AddNumberToObject(s, "dropped", dropped);
    cJSON_AddNumberToObject(s, "uart_errors", uart_errors);
    cJSON *rx = cJSON_AddArrayToObject(s, "rx");
    uint32_t first = acknowledged + 1;
    if (seq >= RING_SIZE && first <= seq - RING_SIZE) first = seq - RING_SIZE + 1;
    for (uint32_t n = first; n <= seq && n < first + 16; n++) {
        frame_t *f = &ring[n % RING_SIZE];
        if (f->seq != n) continue;
        char hex[257];
        for (int i = 0; i < f->size; i++) snprintf(hex + 2*i, 3, "%02x", f->data[i]);
        cJSON *item = cJSON_CreateObject();
        cJSON_AddNumberToObject(item, "seq", f->seq);
        cJSON_AddNumberToObject(item, "port", f->port);
        cJSON_AddNumberToObject(item, "us", f->us);
        cJSON_AddStringToObject(item, "data", hex);
        cJSON_AddItemToArray(rx, item);
    }
    xSemaphoreGive(mutex);
}

static int nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

void debug_uart_control(cJSON *control) {
    if (!mutex) return;
    xSemaphoreTake(mutex, portMAX_DELAY);
    if (!cJSON_IsObject(control)) {
        stop_locked();
        ended_session[0] = error[0] = 0;
        xSemaphoreGive(mutex);
        return;
    }
    const char *sid = str(control, "session"), *role = str(control, "role");
    cJSON *lease_value = cJSON_GetObjectItemCaseSensitive(control, "lease_ms");
    int lease = cJSON_IsNumber(lease_value) ? lease_value->valueint : 0;
    bool allowed = (!strcmp(identity, "s3-fc012cd58460") && !strcmp(role, "target")) || (!strcmp(identity, "s3-fc012cca2cf8") && !strcmp(role, "probe"));
    if (!cJSON_IsObject(control) || strlen(sid) != 24 || !allowed || lease <= 0 || lease > 10000) {
        stop_locked();
        xSemaphoreGive(mutex);
        return;
    }
    // Once the local lease expires, even the same delayed server response must
    // not reopen this session or replay a previously transmitted command.
    if (!strcmp(ended_session, sid)) {
        xSemaphoreGive(mutex);
        return;
    }
    if (strcmp(session, sid)) {
        stop_locked();
        memset(ring, 0, sizeof(ring));
        seq = acknowledged = dropped = uart_errors = 0;
        last_tx[0] = tx_error[0] = error[0] = 0;
        ended_session[0] = 0;
        is_probe = !strcmp(role, "probe");
        esp_err_t result = install_port(1, 4, 5);
        if (result == ESP_OK && is_probe) result = install_port(2, UART_PIN_NO_CHANGE, 8);
        if (result != ESP_OK) {
            snprintf(error, sizeof(error), "UART init: %s", esp_err_to_name(result));
            stop_locked();
            snprintf(ended_session, sizeof(ended_session), "%s", sid);
            xSemaphoreGive(mutex);
            return;
        }
        gpio_set_drive_capability(4, GPIO_DRIVE_CAP_0);
        snprintf(session, sizeof(session), "%s", sid);
    }
    deadline = esp_timer_get_time() + (int64_t)lease * 1000;
    cJSON *ack = cJSON_GetObjectItemCaseSensitive(control, "rx_ack");
    if (cJSON_IsNumber(ack) && ack->valuedouble >= acknowledged && ack->valuedouble <= seq) acknowledged = ack->valuedouble;
    cJSON *tx = cJSON_GetObjectItemCaseSensitive(control, "tx");
    const char *tid = str(tx, "id"), *hex = str(tx, "data");
    if (strlen(tid) == 24 && strcmp(last_tx, tid)) {
        // Record BEFORE writing: lost network acknowledgments cannot replay a
        // physical transmission. Server also binds commands to this boot/session.
        snprintf(last_tx, sizeof(last_tx), "%s", tid);
        tx_error[0] = 0;
        size_t length = strlen(hex);
        unsigned char bytes[256];
        bool valid = length > 0 && length <= 512 && !(length % 2);
        for (size_t i = 0; valid && i < length / 2; i++) {
            int hi = nibble(hex[i*2]), lo = nibble(hex[i*2+1]);
            if (hi < 0 || lo < 0) valid = false;
            else bytes[i] = (hi << 4) | lo;
        }
        if (!valid) snprintf(tx_error, sizeof(tx_error), "Invalid data");
        else if (uart_write_bytes(1, bytes, length/2) != length/2 || uart_wait_tx_done(1, pdMS_TO_TICKS(100)) != ESP_OK) snprintf(tx_error, sizeof(tx_error), "UART transmit incomplete; do not retry automatically");
    }
    wave_control(cJSON_GetObjectItemCaseSensitive(control, "wave"), is_probe);
    debug_i2c_control(cJSON_GetObjectItemCaseSensitive(control, "i2c"), is_probe);
    xSemaphoreGive(mutex);
}
