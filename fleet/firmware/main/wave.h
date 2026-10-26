#pragma once
#include <stdbool.h>
#include "cJSON.h"
#include "esp_err.h"
esp_err_t wave_init(void);
void wave_snapshot(cJSON *snapshot);
void wave_control(cJSON *command, bool probe);
void wave_stop(void);
