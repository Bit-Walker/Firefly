#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <sys/time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "esp_system.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "cJSON.h"
#include "mbedtls/sha256.h"
#include "esp_crt_bundle.h"
#include "esp_sntp.h"
#include "debug_uart.h"
#include "runtime.h"

static const char *TAG = "fleet";
static char server_url[192], bearer[96], device_id[32], boot_id[24];
static char wifi_ssid[33], wifi_pass[65], root_ca[4096];
static EventGroupHandle_t network_events;
static nvs_handle_t storage;
static bool pending_verification, pending_ota;
static volatile uint32_t reboot_at_ms;
static int64_t boot_started;

// 所有设备共用固件；身份、Wi-Fi、服务器地址和证书在首次安装时单独配置到 NVS。
typedef struct { char id[25], state[16], detail[512]; int progress; } report_t;
static report_t report;
static portMUX_TYPE report_lock = portMUX_INITIALIZER_UNLOCKED;
typedef struct { uint32_t seq; char line[256]; } log_line_t;
static log_line_t log_ring[64];
static uint32_t log_seq;
static portMUX_TYPE log_lock = portMUX_INITIALIZER_UNLOCKED;
static vprintf_like_t original_printf = vprintf;

// 用户业务接入时，可覆盖此函数，加入传感器、外设或业务任务的启动自检。
__attribute__((weak)) bool fleet_application_self_test(void) {
    return esp_get_free_heap_size() > 30000;
}

static int capture_log(const char *format, va_list args) {
    va_list copy;
    va_copy(copy, args);
    char line[256];
    vsnprintf(line, sizeof(line), format, copy);
    va_end(copy);
    portENTER_CRITICAL(&log_lock);
    uint32_t seq = ++log_seq;
    log_ring[seq % 64].seq = seq;
    memcpy(log_ring[seq % 64].line, line, sizeof(line));
    portEXIT_CRITICAL(&log_lock);
    return original_printf(format, args);
}

static void set_report(const char *id, const char *state, int progress, const char *detail) {
    portENTER_CRITICAL(&report_lock);
    snprintf(report.id, sizeof(report.id), "%s", id);
    snprintf(report.state, sizeof(report.state), "%s", state);
    snprintf(report.detail, sizeof(report.detail), "%s", detail);
    report.progress = progress;
    portEXIT_CRITICAL(&report_lock);
}

static report_t get_report(void) {
    report_t copy;
    portENTER_CRITICAL(&report_lock);
    copy = report;
    portEXIT_CRITICAL(&report_lock);
    return copy;
}

static void save_pending(const char *id, const char *kind, const char *version) {
    ESP_ERROR_CHECK(nvs_set_str(storage, "job", id));
    ESP_ERROR_CHECK(nvs_set_str(storage, "kind", kind));
    ESP_ERROR_CHECK(nvs_set_str(storage, "target", version));
    ESP_ERROR_CHECK(nvs_commit(storage));
}

static void clear_pending(void) {
    nvs_erase_key(storage, "job");
    nvs_erase_key(storage, "kind");
    nvs_erase_key(storage, "target");
    nvs_commit(storage);
}

static bool read_string(const char *key, char *out, size_t capacity) {
    return nvs_get_str(storage, key, out, &capacity) == ESP_OK;
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) esp_wifi_connect();
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(network_events, BIT0);
        esp_wifi_connect();
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        ESP_LOGI(TAG, "Connected, IP=" IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(network_events, BIT0);
    }
}

static esp_http_client_handle_t new_client(const char *path) {
    char url[384];
    if (snprintf(url, sizeof(url), "%s%s", server_url, path) >= sizeof(url)) return NULL;
    esp_http_client_config_t cfg = {
        .url = url, .cert_pem = strcmp(root_ca, "SYSTEM") ? root_ca : NULL, .timeout_ms = 10000,
        .crt_bundle_attach = !strcmp(root_ca, "SYSTEM") ? esp_crt_bundle_attach : NULL,
        .buffer_size = 4096, .buffer_size_tx = 1024,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client) {
        esp_http_client_set_header(client, "Authorization", bearer);
        esp_http_client_set_header(client, "Connection", "close");
    }
    return client;
}

typedef struct { char id[25], firmware_id[25], sha256[65], version[32]; int size; } ota_args_t;

static void ota_task(void *argument) {
    ota_args_t job = *(ota_args_t *)argument;
    free(argument);
    char path[160], error[128] = "OTA connection failed";
    snprintf(path, sizeof(path), "/api/device/%s/firmware/%s", device_id, job.firmware_id);
    esp_http_client_handle_t client = new_client(path);
    esp_ota_handle_t handle = 0;
    bool handle_open = false;
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (!client || !target || job.size <= 0 || job.size > target->size) goto failed;
    if (esp_http_client_open(client, 0) != ESP_OK) goto failed;
    int64_t length = esp_http_client_fetch_headers(client);
    if (esp_http_client_get_status_code(client) != 200 || length != job.size) {
        snprintf(error, sizeof(error), "Firmware response status/length mismatch"); goto failed;
    }
    esp_err_t result = esp_ota_begin(target, job.size, &handle);
    if (result != ESP_OK) { snprintf(error, sizeof(error), "ota_begin: %s", esp_err_to_name(result)); goto failed; }
    handle_open = true;
    mbedtls_sha256_starts(&sha, 0);
    char *buffer = malloc(4096);
    if (!buffer) { snprintf(error, sizeof(error), "Not enough memory"); goto failed; }
    int total = 0;
    while (total < job.size) {
        int count = esp_http_client_read(client, buffer, job.size - total > 4096 ? 4096 : job.size - total);
        if (count <= 0) { snprintf(error, sizeof(error), "Firmware download interrupted"); free(buffer); goto failed; }
        mbedtls_sha256_update(&sha, (unsigned char *)buffer, count);
        result = esp_ota_write(handle, buffer, count);
        if (result != ESP_OK) { snprintf(error, sizeof(error), "ota_write: %s", esp_err_to_name(result)); free(buffer); goto failed; }
        total += count;
        set_report(job.id, "running", total * 100 / job.size, "Downloading and writing inactive partition");
    }
    free(buffer);
    unsigned char bytes[32];
    char hex[65];
    mbedtls_sha256_finish(&sha, bytes);
    for (int i = 0; i < 32; ++i) snprintf(hex + 2 * i, 3, "%02x", bytes[i]);
    if (strcmp(hex, job.sha256)) { snprintf(error, sizeof(error), "SHA-256 mismatch"); goto failed; }
    result = esp_ota_end(handle);
    handle_open = false;
    if (result != ESP_OK) { snprintf(error, sizeof(error), "Image validation: %s", esp_err_to_name(result)); goto failed; }
    esp_app_desc_t descriptor;
    if (esp_ota_get_partition_description(target, &descriptor) != ESP_OK || strcmp(descriptor.project_name, "fleet_agent") || strcmp(descriptor.version, job.version)) {
        snprintf(error, sizeof(error), "Project/version mismatch"); goto failed;
    }
    save_pending(job.id, "ota", job.version);
    result = esp_ota_set_boot_partition(target);
    if (result != ESP_OK) { clear_pending(); snprintf(error, sizeof(error), "Set boot partition: %s", esp_err_to_name(result)); goto failed; }
    esp_http_client_cleanup(client);
    mbedtls_sha256_free(&sha);
    ESP_LOGI(TAG, "OTA verified: %s -> %s; rebooting", job.version, target->label);
    set_report(job.id, "rebooting", 100, "SHA-256 and image verified; waiting for new boot");
    reboot_at_ms = (uint32_t)(esp_timer_get_time() / 1000) + 5000;
    vTaskDelete(NULL);
    return;
failed:
    if (handle_open) esp_ota_abort(handle);
    if (client) esp_http_client_cleanup(client);
    mbedtls_sha256_free(&sha);
    ESP_LOGE(TAG, "%s", error);
    set_report(job.id, "failed", 0, error);
    vTaskDelete(NULL);
}

static const char *json_string(cJSON *object, const char *key) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    return cJSON_IsString(item) ? item->valuestring : "";
}

static void handle_command(cJSON *command) {
    const char *id = json_string(command, "id");
    const char *kind = json_string(command, "kind");
    if (strlen(id) != 24 || get_report().id[0]) return;
    ESP_LOGI(TAG, "Command %s: %s", id, kind);
    if (!strcmp(kind, "diagnostics")) {
        char detail[512];
        wifi_ap_record_t ap = {0};
        esp_wifi_sta_get_ap_info(&ap);
        snprintf(detail, sizeof(detail), "sdk=%s heap=%lu min_heap=%lu RSSI=%d reset_reason=%d uptime=%llds partition=%s version=%s",
            esp_get_idf_version(), (unsigned long)esp_get_free_heap_size(), (unsigned long)esp_get_minimum_free_heap_size(),
            ap.rssi, esp_reset_reason(), esp_timer_get_time() / 1000000,
            esp_ota_get_running_partition()->label, esp_app_get_description()->version);
        ESP_LOGI(TAG, "Diagnostics: %s", detail);
        set_report(id, "succeeded", 100, detail);
    } else if (!strcmp(kind, "restart")) {
        save_pending(id, "restart", esp_app_get_description()->version);
        set_report(id, "rebooting", 100, "Remote restart requested");
        reboot_at_ms = (uint32_t)(esp_timer_get_time() / 1000) + 4000;
    } else if (!strcmp(kind, "ota")) {
        ota_args_t *args = calloc(1, sizeof(*args));
        if (!args) { set_report(id, "failed", 0, "Not enough memory"); return; }
        snprintf(args->id, sizeof(args->id), "%s", id);
        snprintf(args->firmware_id, sizeof(args->firmware_id), "%s", json_string(command, "firmware_id"));
        snprintf(args->sha256, sizeof(args->sha256), "%s", json_string(command, "sha256"));
        snprintf(args->version, sizeof(args->version), "%s", json_string(command, "target_version"));
        args->size = cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(command, "size"));
        if (strlen(args->sha256) != 64 || strlen(args->firmware_id) != 24) { free(args); set_report(id, "failed", 0, "Invalid OTA manifest"); return; }
        set_report(id, "running", 0, "Starting OTA");
        if (xTaskCreate(ota_task, "fleet_ota", 10240, args, 4, NULL) != pdPASS) {
            free(args); set_report(id, "failed", 0, "Cannot start OTA task");
        }
    } else set_report(id, "failed", 0, "Unsupported command");
}

static uint32_t acknowledged_log;
static bool poll_server(void) {
    cJSON *body = cJSON_CreateObject();
    cJSON *capabilities = cJSON_AddArrayToObject(body, "capabilities");
    cJSON_AddItemToArray(capabilities, cJSON_CreateString("debug_uart_v1"));
    cJSON_AddItemToArray(capabilities, cJSON_CreateString("wave_rmt_v1"));
    cJSON_AddItemToArray(capabilities, cJSON_CreateString("i2c_bench_v1"));
    cJSON_AddItemToArray(capabilities, cJSON_CreateString("i2c_master_v2"));
    debug_uart_snapshot(body);
    cJSON_AddStringToObject(body, "boot_id", boot_id);
    cJSON_AddStringToObject(body, "version", esp_app_get_description()->version);
    cJSON_AddStringToObject(body, "partition", esp_ota_get_running_partition()->label);
    runtime_snapshot(body, pending_verification);
    cJSON_AddNumberToObject(body, "uptime", esp_timer_get_time() / 1000000);
    cJSON_AddNumberToObject(body, "heap", esp_get_free_heap_size());
    cJSON_AddNumberToObject(body, "min_heap", esp_get_minimum_free_heap_size());
    cJSON_AddNumberToObject(body, "reset_reason", esp_reset_reason());
    wifi_ap_record_t ap = {0};
    esp_wifi_sta_get_ap_info(&ap);
    cJSON_AddNumberToObject(body, "rssi", ap.rssi);
    report_t current = get_report();
    if (current.id[0] && !pending_verification) {
        cJSON *item = cJSON_AddObjectToObject(body, "report");
        cJSON_AddStringToObject(item, "id", current.id);
        cJSON_AddStringToObject(item, "state", current.state);
        cJSON_AddNumberToObject(item, "progress", current.progress);
        cJSON_AddStringToObject(item, "detail", current.detail);
    }
    cJSON *logs = cJSON_AddArrayToObject(body, "logs");
    uint32_t max_seq;
    portENTER_CRITICAL(&log_lock); max_seq = log_seq; portEXIT_CRITICAL(&log_lock);
    uint32_t start = acknowledged_log + 1;
    if (max_seq >= 64 && start <= max_seq - 64) start = max_seq - 63;
    uint32_t sent_seq = acknowledged_log;
    for (uint32_t seq = start; seq <= max_seq && seq < start + 16; ++seq) {
        log_line_t item;
        portENTER_CRITICAL(&log_lock); item = log_ring[seq % 64]; portEXIT_CRITICAL(&log_lock);
        if (item.seq != seq) continue;
        cJSON *line = cJSON_CreateObject();
        cJSON_AddNumberToObject(line, "seq", seq);
        cJSON_AddStringToObject(line, "line", item.line);
        cJSON_AddItemToArray(logs, line);
        sent_seq = seq;
    }
    char *payload = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    char path[96]; snprintf(path, sizeof(path), "/api/device/%s/poll", device_id);
    esp_http_client_handle_t client = new_client(path);
    bool success = false;
    if (!client || !payload) goto done;
    esp_http_client_set_method(client, HTTP_METHOD_POST);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    size_t size = strlen(payload);
    if (esp_http_client_open(client, size) != ESP_OK) goto done;
    int written = 0;
    while (written < size) {
        int n = esp_http_client_write(client, payload + written, size - written);
        if (n <= 0) goto done;
        written += n;
    }
    int64_t length = esp_http_client_fetch_headers(client);
    if (esp_http_client_get_status_code(client) != 200 || length <= 0 || length > 8192) goto done;
    char *response = calloc(1, length + 1);
    if (!response) goto done;
    int read = 0;
    while (read < length) {
        int n = esp_http_client_read(client, response + read, length - read);
        if (n <= 0) break;
        read += n;
    }
    cJSON *reply = read == length ? cJSON_Parse(response) : NULL;
    free(response);
    if (!reply) goto done;
    success = true;
    acknowledged_log = sent_seq;
    // 只有联网、TLS 验证、服务端通信及基本内存自检通过后才确认新固件。
    if (pending_verification && fleet_application_self_test()) {
        esp_err_t result = esp_ota_mark_app_valid_cancel_rollback();
        if (result == ESP_OK) {
            pending_verification = false;
            ESP_LOGI(TAG, "New firmware passed health check; rollback cancelled");
            if (current.id[0]) set_report(current.id, "succeeded", 100, "New firmware booted and passed online health check");
        }
    }
    const char *ack = json_string(reply, "report_ack");
    if (current.id[0] && !strcmp(ack, current.id)) {
        clear_pending();
        set_report("", "", 0, "");
    }
    cJSON *command = cJSON_GetObjectItemCaseSensitive(reply, "command");
    if (cJSON_IsObject(command)) handle_command(command);
    if (!pending_verification) debug_uart_control(cJSON_GetObjectItemCaseSensitive(reply, "debug_control"));
    cJSON_Delete(reply);
done:
    free(payload);
    if (client) esp_http_client_cleanup(client);
    return success;
}

void app_main(void) {
    boot_started = esp_timer_get_time();
    original_printf = esp_log_set_vprintf(capture_log);
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(nvs_open("fleet", NVS_READWRITE, &storage));
    char token[80];
    if (!read_string("ssid", wifi_ssid, sizeof(wifi_ssid)) || !read_string("password", wifi_pass, sizeof(wifi_pass)) ||
        !read_string("server", server_url, sizeof(server_url)) || !read_string("token", token, sizeof(token)) ||
        !read_string("ca", root_ca, sizeof(root_ca)) || strncmp(server_url, "https://", 8)) {
        ESP_LOGE(TAG, "Missing provisioning data; run tools/provision.py before booting");
        return;
    }
    snprintf(bearer, sizeof(bearer), "Bearer %s", token);
    memset(token, 0, sizeof(token));
    uint8_t mac[6]; ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
    snprintf(device_id, sizeof(device_id), "s3-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ESP_ERROR_CHECK(debug_uart_init(device_id));
    esp_err_t runtime_result = runtime_init();
    if (runtime_result != ESP_OK) ESP_LOGE(TAG, "Running image verification failed: %s", esp_err_to_name(runtime_result));
    snprintf(boot_id, sizeof(boot_id), "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());
    esp_ota_img_states_t state;
    pending_verification = esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY;
#ifdef FLEET_TEST_CRASH_BEFORE_CONFIRM
    // 仅供台架回退测试；正常构建不会包含这段逻辑。
    if (pending_verification) { ESP_LOGE(TAG, "ROLLBACK TEST: reset before health confirmation"); esp_restart(); }
#endif
    char job[25], kind[16], target[32];
    if (read_string("job", job, sizeof(job)) && read_string("kind", kind, sizeof(kind)) && read_string("target", target, sizeof(target))) {
        pending_ota = !strcmp(kind, "ota");
        if (pending_ota && strcmp(target, esp_app_get_description()->version)) {
            set_report(job, "failed", 0, "Bootloader rolled back to previous firmware");
        } else set_report(job, "succeeded", 100, pending_ota ? "Waiting for new firmware health check" : "Remote restart completed");
    }
    // 首次配置的时间是 TLS 启动基准；随后由 SNTP 校准，不关闭证书日期验证。
    int64_t epoch = 0;
    if (nvs_get_i64(storage, "epoch", &epoch) == ESP_OK) {
        struct timeval now = {.tv_sec = epoch}; settimeofday(&now, NULL);
    }
    ESP_LOGI(TAG, "Device=%s version=%s partition=%s boot=%s pending=%d", device_id,
        esp_app_get_description()->version, esp_ota_get_running_partition()->label, boot_id, pending_verification);
    network_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t wifi = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wifi));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t config = {0};
    memcpy(config.sta.ssid, wifi_ssid, strlen(wifi_ssid));
    memcpy(config.sta.password, wifi_pass, strlen(wifi_pass));
    config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, "pool.ntp.org");
    esp_sntp_init();
    int failures = 0;
    int64_t last_saved = 0;
    while (true) {
        if ((xEventGroupGetBits(network_events) & BIT0) && poll_server()) {
            if (failures) ESP_LOGI(TAG, "Management service reconnected");
            failures = 0;
            if (esp_timer_get_time() - last_saved > 3600000000LL) {
                struct timeval now; gettimeofday(&now, NULL);
                nvs_set_i64(storage, "epoch", now.tv_sec); nvs_commit(storage);
                last_saved = esp_timer_get_time();
            }
        } else if (++failures % 5 == 1) ESP_LOGW(TAG, "Service unavailable; retrying (attempt %d)", failures);
        if (pending_verification && esp_timer_get_time() - boot_started > 90000000) {
            ESP_LOGE(TAG, "New firmware failed online health check; rolling back");
            esp_ota_mark_app_invalid_rollback_and_reboot();
        }
        if (reboot_at_ms && (int32_t)((uint32_t)(esp_timer_get_time() / 1000) - reboot_at_ms) >= 0) esp_restart();
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
