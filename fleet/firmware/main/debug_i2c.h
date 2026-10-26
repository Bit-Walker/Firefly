#pragma once
#include <stdbool.h>
#include "cJSON.h"
void debug_i2c_control(cJSON *control, bool is_probe);
void debug_i2c_snapshot(cJSON *debug);
void debug_i2c_stop(void);
void debug_i2c_tick(void);
