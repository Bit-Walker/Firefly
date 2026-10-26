#include <stdio.h>
#include "runtime.h"
#include "esp_app_desc.h"
#include "esp_image_format.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"

static char image_sha256[65];
static uint32_t image_size;
static esp_err_t image_status = ESP_ERR_INVALID_STATE;

esp_err_t runtime_init(void) {
    const esp_partition_t *partition = esp_ota_get_running_partition();
    if (!partition) return image_status = ESP_ERR_NOT_FOUND;
    esp_partition_pos_t position = {.offset = partition->address, .size = partition->size};
    esp_image_metadata_t metadata = {0};
    image_status = esp_image_verify(ESP_IMAGE_VERIFY_SILENT, &position, &metadata);
    if (image_status != ESP_OK) return image_status;
    // Hash verified running bytes INCLUDING the appended hash: the uploaded
    // file digest, rather than an ELF hash or a saved OTA manifest value.
    if (!metadata.image.hash_appended || !metadata.image_len || metadata.image_len > partition->size)
        return image_status = ESP_ERR_INVALID_SIZE;
    mbedtls_sha256_context hash;
    mbedtls_sha256_init(&hash);
    unsigned char buffer[1024], digest[32];
    image_status = mbedtls_sha256_starts(&hash, 0);
    for (uint32_t offset = 0; image_status == ESP_OK && offset < metadata.image_len;) {
        size_t count = metadata.image_len - offset;
        if (count > sizeof(buffer)) count = sizeof(buffer);
        image_status = esp_partition_read(partition, offset, buffer, count);
        if (image_status == ESP_OK) image_status = mbedtls_sha256_update(&hash, buffer, count);
        offset += count;
    }
    if (image_status == ESP_OK) image_status = mbedtls_sha256_finish(&hash, digest);
    mbedtls_sha256_free(&hash);
    if (image_status != ESP_OK) return image_status;
    for (int i = 0; i < 32; ++i) snprintf(image_sha256 + i * 2, 3, "%02x", digest[i]);
    image_size = metadata.image_len;
    return ESP_OK;
}

void runtime_snapshot(cJSON *body, bool pending_verification) {
    cJSON *runtime = cJSON_AddObjectToObject(body, "runtime");
    cJSON_AddStringToObject(runtime, "protocol", "edbg.agent/v1");
    cJSON_AddStringToObject(runtime, "family", "esp32s3");
    cJSON_AddStringToObject(runtime, "build", esp_app_get_description()->version);
    cJSON_AddNumberToObject(runtime, "uptime_ms", esp_timer_get_time() / 1000);
    cJSON_AddStringToObject(runtime, "health", image_status != ESP_OK ? "error" : pending_verification ? "pending" : "valid");
    if (image_status == ESP_OK) {
        cJSON *image = cJSON_AddObjectToObject(runtime, "image");
        cJSON_AddStringToObject(image, "algorithm", "sha256-file-v1");
        cJSON_AddStringToObject(image, "sha256", image_sha256);
        cJSON_AddNumberToObject(image, "size", image_size);
        cJSON_AddStringToObject(image, "project", esp_app_get_description()->project_name);
    } else cJSON_AddNullToObject(runtime, "image");
    cJSON *features = cJSON_AddArrayToObject(runtime, "features");
    const char *supported[] = {"runtime.image/1", "node.update/1", "uart.fixed/1", "i2c.fixed/1", "wave.fixed/1"};
    for (size_t i = 0; i < sizeof(supported) / sizeof(supported[0]); ++i)
        cJSON_AddItemToArray(features, cJSON_CreateString(supported[i]));
}
