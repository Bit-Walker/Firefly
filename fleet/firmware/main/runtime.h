#pragma once
#include <stdbool.h>
#include "cJSON.h"
#include "esp_err.h"

esp_err_t runtime_init(void);
void runtime_snapshot(cJSON *body, bool pending_verification);
