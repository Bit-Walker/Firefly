#include <stdio.h>
#include <string.h>
#include "wave.h"
#include "wave_decode.h"
#include "driver/gpio.h"
#include "driver/rmt_rx.h"
#include "driver/rmt_tx.h"
#include "driver/rmt_encoder.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "esp_timer.h"

#define SYMBOLS 256
static SemaphoreHandle_t guard;
static QueueHandle_t events;
static rmt_channel_handle_t channel;
static rmt_encoder_handle_t encoder;
static bool enabled, running, receiving;
static volatile bool accept_rx;
static int gpio, segment_count;
static int64_t started, finished, limit;
static char run_id[25], state[16] = "idle", error[96];
static bool truncated;
static wave_segment_t segments[SYMBOLS * 2];
static rmt_symbol_word_t input[SYMBOLS], output[64];
static rmt_receive_config_t receive_config = {.signal_range_min_ns = 1000, .signal_range_max_ns = 20000000};

static const char *str(cJSON *o, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}

static int number(cJSON *o, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsNumber(v) ? v->valueint : 0;
}

static bool rx_done(rmt_channel_handle_t chan, const rmt_rx_done_event_data_t *event, void *ctx) {
    bool pulse = wave_has_pulse((const uint32_t *)event->received_symbols, event->num_symbols < SYMBOLS ? event->num_symbols : SYMBOLS);
    // A long idle level is not a trigger. Rearm directly in the ISR (supported
    // by IDF) so network-scale waits do not leave task-scheduling-sized gaps.
    if (!pulse && accept_rx) {
        if (rmt_receive(chan, input, sizeof(input), &receive_config) == ESP_OK) return false;
    }
    if (!accept_rx) return false;
    accept_rx = false;
    int count = pulse ? (int)event->num_symbols : -1;
    BaseType_t wake = pdFALSE;
    xQueueSendFromISR(events, &count, &wake);
    return wake == pdTRUE;
}

static bool tx_done(rmt_channel_handle_t chan, const rmt_tx_done_event_data_t *event, void *ctx) {
    int done = -2;
    BaseType_t wake = pdFALSE;
    xQueueSendFromISR(events, &done, &wake);
    return wake == pdTRUE;
}

static void cleanup(void) {
    accept_rx = false;
    if (channel) {
        if (enabled) rmt_disable(channel);
        rmt_del_channel(channel);
        channel = NULL;
    }
    enabled = false;
    if (encoder) { rmt_del_encoder(encoder); encoder = NULL; }
    // Both ends always return to inputs, including the sole output pin.
    if (running) {
        gpio_config_t cfg = {.pin_bit_mask = (1ULL << 6) | (1ULL << 7), .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE, .intr_type = GPIO_INTR_DISABLE};
        gpio_config(&cfg);
    }
    running = false;
}

static void fail(const char *message) {
    snprintf(error, sizeof(error), "%s", message);
    snprintf(state, sizeof(state), "failed");
    finished = esp_timer_get_time();
    cleanup();
}

static void collect(int count) {
    if (count <= 0) { fail("RMT receive failed"); return; }
    truncated = count >= SYMBOLS;
    segment_count = wave_decode((const uint32_t *)input, count < SYMBOLS ? count : SYMBOLS, segments, SYMBOLS * 2);
    finished = esp_timer_get_time();
    snprintf(state, sizeof(state), "captured");
    cleanup();
}

static void worker(void *unused) {
    while (true) {
        xSemaphoreTake(guard, portMAX_DELAY);
        if (running) {
            int count;
            if (xQueueReceive(events, &count, 0)) {
                if (receiving) collect(count);
                else if (count == -2 && rmt_tx_wait_all_done(channel, 20) == ESP_OK) {
                    snprintf(state, sizeof(state), "sent");
                    finished = esp_timer_get_time();
                    cleanup();
                } else fail("Pulse completion failed");
            } else if (esp_timer_get_time() >= limit) fail(receiving ? "No pulse trigger before timeout" : "Pulse output timeout");
        }
        xSemaphoreGive(guard);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

esp_err_t wave_init(void) {
    guard = xSemaphoreCreateMutex();
    events = xQueueCreate(2, sizeof(int));
    if (!guard || !events) return ESP_ERR_NO_MEM;
    return xTaskCreate(worker, "wave", 4096, NULL, 4, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void wave_stop(void) {
    if (!guard) return;
    xSemaphoreTake(guard, portMAX_DELAY);
    if (running) {
        snprintf(state, sizeof(state), "cancelled");
        finished = esp_timer_get_time();
        cleanup();
    }
    xSemaphoreGive(guard);
}

void wave_snapshot(cJSON *snapshot) {
    if (!guard) return;
    xSemaphoreTake(guard, portMAX_DELAY);
    cJSON *w = cJSON_AddObjectToObject(snapshot, "wave");
    cJSON_AddStringToObject(w, "id", run_id);
    cJSON_AddStringToObject(w, "state", state);
    cJSON_AddStringToObject(w, "error", error);
    cJSON_AddNumberToObject(w, "gpio", gpio);
    cJSON_AddNumberToObject(w, "started_us", started);
    cJSON_AddNumberToObject(w, "finished_us", finished);
    cJSON_AddBoolToObject(w, "truncated", truncated);
    cJSON *items = cJSON_AddArrayToObject(w, "segments");
    for (int i = 0; i < segment_count; i++) {
        cJSON *item = cJSON_CreateObject();
        cJSON_AddNumberToObject(item, "level", segments[i].level);
        cJSON_AddNumberToObject(item, "us", segments[i].us);
        cJSON_AddItemToArray(items, item);
    }
    xSemaphoreGive(guard);
}

void wave_control(cJSON *command, bool probe) {
    if (!guard) return;
    xSemaphoreTake(guard, portMAX_DELAY);
    if (!cJSON_IsObject(command)) {
        if (running) {
            snprintf(state, sizeof(state), "cancelled");
            finished = esp_timer_get_time();
            cleanup();
        }
        xSemaphoreGive(guard);
        return;
    }
    const char *id = str(command, "id"), *kind = str(command, "kind");
    if (strlen(id) != 24 || !strcmp(id, run_id)) { xSemaphoreGive(guard); return; }
    cleanup();
    xQueueReset(events);
    snprintf(run_id, sizeof(run_id), "%s", id);
    error[0] = 0;
    segment_count = 0;
    truncated = false;
    finished = 0;
    started = esp_timer_get_time();
    receiving = !strcmp(kind, "capture");
    gpio = receiving ? (probe ? 6 : 7) : (probe ? 7 : 6);
    running = true;
    limit = started + (receiving ? 20000000 : 1500000);
    esp_err_t result;
    if (receiving) {
        rmt_rx_channel_config_t cfg = {.gpio_num = gpio, .clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 1000000, .mem_block_symbols = 64};
        result = rmt_new_rx_channel(&cfg, &channel);
        if (result == ESP_OK) result = gpio_pullup_dis(gpio);
        if (result == ESP_OK) result = gpio_pulldown_en(gpio);
        rmt_rx_event_callbacks_t callbacks = {.on_recv_done = rx_done};
        if (result == ESP_OK) result = rmt_rx_register_event_callbacks(channel, &callbacks, NULL);
        if (result == ESP_OK) { result = rmt_enable(channel); enabled = result == ESP_OK; }
        if (result == ESP_OK) { accept_rx = true; result = rmt_receive(channel, input, sizeof(input), &receive_config); }
        if (result == ESP_OK) snprintf(state, sizeof(state), "armed");
    } else if (!strcmp(kind, "pulse")) {
        int hi = number(command, "high_us"), lo = number(command, "low_us"), count = number(command, "count");
        if (hi < 50 || hi > 10000 || lo < 50 || lo > 10000 || count < 1 || count > 64 || (hi + lo) * count > 1000000) {
            fail("Pulse limits: 50..10000us, 1..64 pulses, <=1 second");
            xSemaphoreGive(guard); return;
        }
        for (int i = 0; i < count; i++) output[i] = (rmt_symbol_word_t){.level0 = 1, .duration0 = hi, .level1 = 0, .duration1 = lo};
        gpio_set_level(gpio, 0);
        rmt_tx_channel_config_t cfg = {.gpio_num = gpio, .clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 1000000, .mem_block_symbols = 64, .trans_queue_depth = 1};
        result = rmt_new_tx_channel(&cfg, &channel);
        rmt_copy_encoder_config_t enc_cfg = {0};
        if (result == ESP_OK) result = rmt_new_copy_encoder(&enc_cfg, &encoder);
        rmt_tx_event_callbacks_t callbacks = {.on_trans_done = tx_done};
        if (result == ESP_OK) result = rmt_tx_register_event_callbacks(channel, &callbacks, NULL);
        if (result == ESP_OK) { result = rmt_enable(channel); enabled = result == ESP_OK; }
        if (result == ESP_OK) {
            gpio_set_drive_capability(gpio, GPIO_DRIVE_CAP_0);
            rmt_transmit_config_t transmit = {.loop_count = 0, .flags.eot_level = 0};
            result = rmt_transmit(channel, encoder, output, count * sizeof(output[0]), &transmit);
        }
        if (result == ESP_OK) snprintf(state, sizeof(state), "sending");
    } else result = ESP_ERR_INVALID_ARG;
    if (result != ESP_OK) fail(esp_err_to_name(result));
    xSemaphoreGive(guard);
}
