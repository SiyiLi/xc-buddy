#include "ui_status_icons.h"

#include <string.h>

#define XV_ICON_SIZE 112
#define XV_ICON_STRIDE (XV_ICON_SIZE * 4)
#define XV_ICON_DATA_SIZE (XV_ICON_SIZE * XV_ICON_STRIDE)
#define XV_ICON_TOP_Y 42

extern const uint8_t xv_pairing_start[] asm("_binary_xv_pairing_argb8888_bin_start");
extern const uint8_t xv_ready_start[] asm("_binary_xv_ready_argb8888_bin_start");
extern const uint8_t xv_listening_start[] asm("_binary_xv_listening_argb8888_bin_start");
extern const uint8_t xv_thinking_start[] asm("_binary_xv_thinking_argb8888_bin_start");
extern const uint8_t xv_resting_start[] asm("_binary_xv_resting_argb8888_bin_start");
extern const uint8_t xv_error_start[] asm("_binary_xv_error_argb8888_bin_start");

static const lv_image_dsc_t s_xv_pairing = {
    .header.magic = LV_IMAGE_HEADER_MAGIC,
    .header.cf = LV_COLOR_FORMAT_ARGB8888,
    .header.flags = 0,
    .header.w = XV_ICON_SIZE,
    .header.h = XV_ICON_SIZE,
    .header.stride = XV_ICON_STRIDE,
    .data_size = XV_ICON_DATA_SIZE,
    .data = xv_pairing_start,
};

static const lv_image_dsc_t s_xv_ready = {
    .header.magic = LV_IMAGE_HEADER_MAGIC,
    .header.cf = LV_COLOR_FORMAT_ARGB8888,
    .header.flags = 0,
    .header.w = XV_ICON_SIZE,
    .header.h = XV_ICON_SIZE,
    .header.stride = XV_ICON_STRIDE,
    .data_size = XV_ICON_DATA_SIZE,
    .data = xv_ready_start,
};

static const lv_image_dsc_t s_xv_listening = {
    .header.magic = LV_IMAGE_HEADER_MAGIC,
    .header.cf = LV_COLOR_FORMAT_ARGB8888,
    .header.flags = 0,
    .header.w = XV_ICON_SIZE,
    .header.h = XV_ICON_SIZE,
    .header.stride = XV_ICON_STRIDE,
    .data_size = XV_ICON_DATA_SIZE,
    .data = xv_listening_start,
};

static const lv_image_dsc_t s_xv_thinking = {
    .header.magic = LV_IMAGE_HEADER_MAGIC,
    .header.cf = LV_COLOR_FORMAT_ARGB8888,
    .header.flags = 0,
    .header.w = XV_ICON_SIZE,
    .header.h = XV_ICON_SIZE,
    .header.stride = XV_ICON_STRIDE,
    .data_size = XV_ICON_DATA_SIZE,
    .data = xv_thinking_start,
};

static const lv_image_dsc_t s_xv_resting = {
    .header.magic = LV_IMAGE_HEADER_MAGIC,
    .header.cf = LV_COLOR_FORMAT_ARGB8888,
    .header.flags = 0,
    .header.w = XV_ICON_SIZE,
    .header.h = XV_ICON_SIZE,
    .header.stride = XV_ICON_STRIDE,
    .data_size = XV_ICON_DATA_SIZE,
    .data = xv_resting_start,
};

static const lv_image_dsc_t s_xv_error = {
    .header.magic = LV_IMAGE_HEADER_MAGIC,
    .header.cf = LV_COLOR_FORMAT_ARGB8888,
    .header.flags = 0,
    .header.w = XV_ICON_SIZE,
    .header.h = XV_ICON_SIZE,
    .header.stride = XV_ICON_STRIDE,
    .data_size = XV_ICON_DATA_SIZE,
    .data = xv_error_start,
};

static const lv_image_dsc_t *get_scene_image(ui_status_icon_scene_t scene)
{
    switch (scene) {
    case UI_STATUS_ICON_BOOT:
    case UI_STATUS_ICON_PAIRING:
        return &s_xv_pairing;
    case UI_STATUS_ICON_IDLE:
        return &s_xv_ready;
    case UI_STATUS_ICON_RESTING:
        return &s_xv_resting;
    case UI_STATUS_ICON_RECORDING:
        return &s_xv_listening;
    case UI_STATUS_ICON_TRANSCRIBING:
        return &s_xv_thinking;
    case UI_STATUS_ICON_ERROR:
        return &s_xv_error;
    }
    return &s_xv_ready;
}

void ui_status_icons_create(ui_status_icons_t *icons, lv_obj_t *screen)
{
    memset(icons, 0, sizeof(*icons));

    icons->root = lv_image_create(screen);
    lv_obj_remove_style_all(icons->root);
    lv_image_set_src(icons->root, &s_xv_pairing);
    lv_obj_align(icons->root, LV_ALIGN_TOP_MID, 0, XV_ICON_TOP_Y);
}

void ui_status_icons_stop_anim(ui_status_icons_t *icons)
{
    lv_anim_delete(icons->root, NULL);
}

void ui_status_icons_apply(ui_status_icons_t *icons, ui_status_icon_scene_t scene)
{
    ui_status_icons_stop_anim(icons);
    lv_image_set_src(icons->root, get_scene_image(scene));
    lv_obj_set_style_opa(icons->root, LV_OPA_COVER, 0);
    lv_obj_align(icons->root, LV_ALIGN_TOP_MID, 0, XV_ICON_TOP_Y);
}

void ui_status_icons_start_anim(ui_status_icons_t *icons, ui_status_icon_scene_t scene)
{
    (void)icons;
    (void)scene;
}
