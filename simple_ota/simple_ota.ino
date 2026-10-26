#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include "config.h"
#if __has_include("demo_version.h")
#include "demo_version.h"
#endif

#ifndef DEMO_VERSION
#define DEMO_VERSION 1
#endif

// Build the same program twice; only the displayed version changes.
const char *VERSION = DEMO_VERSION == 2 ? "2.0.0" : "1.0.0";
WebServer server(80);
String uploadToken;
bool uploadStarted = false;
bool uploadEnded = false;
size_t expectedBytes = 0;
size_t receivedBytes = 0;
String uploadError;
uint32_t rebootAt = 0;

const char PAGE[] PROGMEM = R"HTML(<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESP32-S3 无线升级</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:#182b40;font:16px/1.7 system-ui,sans-serif}
main{max-width:660px;margin:7vh auto;padding:28px}section{background:white;border:1px solid #dce4ed;border-radius:20px;padding:28px;margin:20px 0}
h1{font-size:30px;margin:0}h2{font-size:20px;margin:0 0 16px}p{margin:10px 0;color:#52647a}
.badge{display:inline-block;border-radius:30px;padding:6px 16px;background:%COLOR%;color:white;font-weight:700;margin:16px 0}
dl{display:grid;grid-template-columns:100px 1fr;margin:0;gap:6px}dt{color:#64748b}dd{margin:0;overflow-wrap:anywhere}
input{width:100%;margin:12px 0}button{background:#1466d9;color:white;border:0;border-radius:9px;padding:12px 20px;font-size:16px;cursor:pointer}button:disabled{opacity:.55}
progress{width:100%;height:18px;margin-top:18px}small{color:#64748b}#result{white-space:pre-wrap}
</style></head><body><main>
<small>ESP32-S3 / OTA DEMO</small><h1>通过 Wi-Fi 更新程序</h1>
<section><span class="badge">当前版本 v%VERSION%</span>
<dl><dt>程序分区</dt><dd>%PARTITION%</dd><dt>设备地址</dt><dd>%IP%</dd><dt>编译时间</dt><dd>%BUILD%</dd></dl>
<p>%MESSAGE%</p></section>
<section><h2>上传新的固件</h2><p>选择本工程生成的 <b>ota-v1.bin</b> 或 <b>ota-v2.bin</b>。升级成功后设备会自动重启。</p>
<form id="form"><input id="file" type="file" accept=".bin" required>
<button id="send">开始无线升级</button></form>
<progress id="progress" value="0" max="100" hidden></progress><p id="result" role="status" aria-live="polite">等待选择固件。</p>
<small>上传的是应用程序 .bin；请勿上传 merged.bin、bootloader 或 partitions 文件。新程序也必须保留 OTA 功能。</small>
</section></main><script>
const token='%TOKEN%';
const form=document.querySelector('#form'),button=document.querySelector('#send'),result=document.querySelector('#result'),progress=document.querySelector('#progress');
form.addEventListener('submit',event=>{
  event.preventDefault(); const file=document.querySelector('#file').files[0];
  if(!file || !file.name.toLowerCase().endsWith('.bin') || file.size===0){result.textContent='请选择非空的 .bin 固件。';return;}
  if(file.size>%MAX_SIZE%){result.textContent='固件超出 OTA 分区容量。';return;}
  const data=new FormData();data.append('firmware',file);
  const xhr=new XMLHttpRequest();xhr.open('POST','/update?token='+token+'&size='+file.size);
  button.disabled=true;progress.hidden=false;progress.value=0;result.textContent='正在通过 Wi-Fi 上传…';
  xhr.upload.onprogress=e=>{if(e.lengthComputable){progress.value=Math.round(e.loaded/e.total*100);result.textContent='上传进度 '+progress.value+'%（上传后还需校验）';}};
  xhr.timeout=180000;
  xhr.onerror=xhr.ontimeout=()=>{button.disabled=false;result.textContent='连接中断或超时。请重新打开网页检查当前版本。';};
  xhr.onload=()=>{
    result.textContent=xhr.responseText;
    if(xhr.status!==200){button.disabled=false;return;}
    result.textContent+='\n正在等待设备重启…';
    let attempts=0;
    const waitForBoot=async()=>{
      try{const response=await fetch('/api/status',{cache:'no-store'});if(response.ok){const status=await response.json();if(status.boot_token!==token){location.reload();return;}}}catch(error){}
      if(++attempts<45){setTimeout(waitForBoot,2000);}else{result.textContent+='\n请稍后手动刷新网页。';button.disabled=false;}
    };
    setTimeout(waitForBoot,3000);
  };
  xhr.send(data);
});
</script></body></html>)HTML";

bool authenticated() {
  if (server.authenticate(OTA_USERNAME, OTA_PASSWORD)) return true;
  server.requestAuthentication(BASIC_AUTH, "ESP32-S3 OTA");
  return false;
}

bool validUploadRequest() {
  // A per-boot token and Origin check protect the authenticated upload endpoint.
  return server.arg("token") == uploadToken &&
         server.header("Origin") == String("http://") + server.header("Host");
}

void failUpload(const String &message) {
  uploadError = message;
  if (Update.isRunning()) Update.abort();
  Serial.printf("[OTA] rejected: %s\n", message.c_str());
}

void statusPage() {
  if (!authenticated()) return;
  const esp_partition_t *running = esp_ota_get_running_partition();
  const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
  String json = "{\"version\":\"" + String(VERSION) + "\",\"partition\":\"" + running->label;
  json += "\",\"ip\":\"" + WiFi.localIP().toString();
  json += "\",\"boot_token\":\"" + uploadToken + "\",\"uptime_ms\":" + String(millis());
  json += ",\"ota_capacity\":" + String(next ? next->size : 0) + "}";
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", json);
}

void homePage() {
  if (!authenticated()) return;
  const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
  String page = FPSTR(PAGE);
  page.replace("%VERSION%", VERSION);
  page.replace("%PARTITION%", esp_ota_get_running_partition()->label);
  page.replace("%IP%", WiFi.localIP().toString());
  page.replace("%BUILD%", String(__DATE__) + " " + __TIME__);
  page.replace("%COLOR%", DEMO_VERSION == 2 ? "#15803d" : "#1466d9");
  page.replace("%MESSAGE%", DEMO_VERSION == 2 ? "v2 已运行：你已成功通过 Wi-Fi 更新程序。" : "这是通过 USB 首次安装的 v1。上传 v2 固件来体验 OTA。");
  page.replace("%TOKEN%", uploadToken);
  page.replace("%MAX_SIZE%", String(next ? next->size : 0));
  server.sendHeader("Cache-Control", "no-store");
  server.sendHeader("X-Frame-Options", "DENY");
  server.send(200, "text/html; charset=utf-8", page);
}

// The upload callback streams data into the inactive app partition.
// It does NOT change the next boot partition until the entire POST is checked.
void receiveUpload() {
  HTTPUpload &upload = server.upload();
  if (upload.status == UPLOAD_FILE_START) {
    if (uploadStarted) { failUpload("Only one firmware file is allowed."); return; }
    uploadStarted = true;
    uploadEnded = false;
    receivedBytes = 0;
    uploadError = "";
    if (!server.authenticate(OTA_USERNAME, OTA_PASSWORD) || !validUploadRequest()) {
      failUpload("Authentication or upload token rejected."); return;
    }
    if (rebootAt) { failUpload("Device is restarting."); return; }
    if (upload.name != "firmware" || !upload.filename.endsWith(".bin")) {
      failUpload("Choose an application .bin file."); return;
    }
    String sizeArgument = server.arg("size");
    if (sizeArgument.isEmpty() || sizeArgument.length() > 8) {
      failUpload("Missing or invalid firmware size."); return;
    }
    for (size_t i = 0; i < sizeArgument.length(); ++i) {
      if (sizeArgument[i] < '0' || sizeArgument[i] > '9') {
        failUpload("Invalid firmware size."); return;
      }
    }
    expectedBytes = sizeArgument.toInt();
    const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
    if (!next || !expectedBytes || expectedBytes > next->size) {
      failUpload("Firmware does not fit the OTA partition."); return;
    }
    Serial.printf("[OTA] begin: %u bytes -> %s\n", unsigned(expectedBytes), next->label);
    // 1. Prepare the inactive partition; no modification of the running app.
    if (!Update.begin(expectedBytes, U_FLASH)) failUpload(Update.errorString());
  } else if (upload.status == UPLOAD_FILE_WRITE && uploadError.isEmpty() && uploadStarted) {
    if (receivedBytes + upload.currentSize > expectedBytes) {
      failUpload("Received more data than declared."); return;
    }
    // 2. Write each incoming Wi-Fi chunk to Flash.
    if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
      failUpload(Update.errorString()); return;
    }
    receivedBytes += upload.currentSize;
  } else if (upload.status == UPLOAD_FILE_END) {
    uploadEnded = true;
    if (uploadError.isEmpty() && receivedBytes != expectedBytes) failUpload("Incomplete firmware.");
  } else if (upload.status == UPLOAD_FILE_ABORTED) {
    failUpload("Upload connection aborted.");
    uploadStarted = false;
  }
}

void finishUpload() {
  if (!authenticated()) {
    if (Update.isRunning()) Update.abort();
    uploadStarted = false;
    return;
  }
  if (!validUploadRequest()) {
    if (Update.isRunning()) Update.abort();
    uploadStarted = false;
    server.send(403, "text/plain", "Invalid upload token or Origin.");
    return;
  }
  if (!uploadStarted || !uploadEnded || !uploadError.isEmpty()) {
    if (Update.isRunning()) Update.abort();
    server.send(400, "text/plain", uploadError.isEmpty() ? "No complete firmware received." : uploadError);
    uploadStarted = false;
    return;
  }
  // 3. Verify the complete image and select it as the next boot application.
  const bool success = Update.end();
  uploadStarted = false;
  if (!success) {
    server.send(400, "text/plain", String("Firmware validation failed: ") + Update.errorString());
    return;
  }
  Serial.printf("[OTA] complete: %u bytes; rebooting\n", unsigned(receivedBytes));
  server.sendHeader("Connection", "close");
  server.send(200, "text/plain; charset=utf-8", "固件校验成功，设备即将重启。");
  // Return the HTTP response before restarting.
  rebootAt = millis() + 1500;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("\n[BOOT] ESP32-S3 OTA demo v%s, running=%s\n", VERSION, esp_ota_get_running_partition()->label);
  uploadToken = String(uint32_t(esp_random()), HEX) + String(uint32_t(esp_random()), HEX);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[WIFI] connecting to %s", WIFI_SSID);
  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    delay(500); Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WIFI] IP=%s\n", WiFi.localIP().toString().c_str());
    if (MDNS.begin("esp32s3-ota")) MDNS.addService("http", "tcp", 80);
  } else {
    Serial.println("[WIFI] not connected; retrying in background (2.4 GHz required)");
  }
  const char *headers[] = {"Origin", "Host"};
  server.collectHeaders(headers, 2);
  server.on("/", HTTP_GET, homePage);
  server.on("/api/status", HTTP_GET, statusPage);
  server.on("/update", HTTP_POST, finishUpload, receiveUpload);
  server.onNotFound([]() { server.send(404, "text/plain", "Not found"); });
  server.begin();
  Serial.println("[HTTP] ready; http://esp32s3-ota.local/ (login required)");
}

void loop() {
  server.handleClient();
  if (rebootAt && int32_t(millis() - rebootAt) >= 0) ESP.restart();
  static uint32_t lastHeartbeat = 0;
  if (millis() - lastHeartbeat >= 10000) {
    lastHeartbeat = millis();
    Serial.printf("[ALIVE] version=%s partition=%s wifi=%d ip=%s\n", VERSION,
                  esp_ota_get_running_partition()->label, int(WiFi.status()),
                  WiFi.localIP().toString().c_str());
  }
  delay(2);
}
