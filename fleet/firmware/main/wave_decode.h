#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct { int level, us; } wave_segment_t;

// RMT symbols: duration0[14:0], level0[15], duration1[30:16], level1[31].
// A zero duration terminates the entire stream; the other half may be stale.
static inline bool wave_has_pulse(const uint32_t *words, size_t count) {
    for (size_t i = 0; i < count; i++) {
        for (int half = 0; half < 2; half++) {
            uint32_t sample = words[i] >> (16 * half);
            unsigned us = sample & 0x7fff;
            if (!us) return false;
            if ((sample & 0x8000) && us >= 5 && us < 20000) return true;
        }
    }
    return false;
}

static inline size_t wave_decode(const uint32_t *words, size_t count, wave_segment_t *out, size_t capacity) {
    size_t used = 0;
    bool triggered = false;
    for (size_t i = 0; i < count; i++) {
        for (int half = 0; half < 2; half++) {
            uint32_t sample = words[i] >> (16 * half);
            int us = sample & 0x7fff, level = (sample >> 15) & 1;
            if (!us || used == capacity) return used;
            if (!triggered && !level) continue;
            triggered = true;
            out[used++] = (wave_segment_t){level, us};
        }
    }
    return used;
}
