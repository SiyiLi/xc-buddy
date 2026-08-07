idf_component_register(
    SRCS "ui_status.c" "ui_status_icons.c"
    INCLUDE_DIRS "include" "."
    EMBED_FILES
        "assets/xv_pairing_argb8888.bin"
        "assets/xv_ready_argb8888.bin"
        "assets/xv_listening_argb8888.bin"
        "assets/xv_thinking_argb8888.bin"
        "assets/xv_resting_argb8888.bin"
        "assets/xv_error_argb8888.bin"
    REQUIRES driver esp_driver_ledc esp_lcd esp_timer stick_s3_board lvgl
)
