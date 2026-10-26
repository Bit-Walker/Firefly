idf_component_register(SRCS "agent.c" "runtime.c" "debug_uart.c" "wave.c" "debug_i2c.c" INCLUDE_DIRS "."
  REQUIRES esp_wifi esp_event esp_netif esp_http_client app_update bootloader_support nvs_flash json mbedtls esp_timer lwip esp_driver_uart esp_driver_gpio esp_driver_rmt driver)
if(FLEET_TEST_CRASH_BEFORE_CONFIRM)
  target_compile_definitions(${COMPONENT_LIB} PRIVATE FLEET_TEST_CRASH_BEFORE_CONFIRM=1)
endif()
