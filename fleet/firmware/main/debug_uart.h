#pragma once
#include "cJSON.h"
#include "esp_err.h"
esp_err_t debug_uart_init(const char *device_id);
void debug_uart_snapshot(cJSON *body);
void debug_uart_control(cJSON *control);
