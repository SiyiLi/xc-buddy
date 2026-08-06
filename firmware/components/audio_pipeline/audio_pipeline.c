#include "audio_pipeline.h"

#include <inttypes.h>
#include <stdatomic.h>
#include <string.h>

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hal/i2s_types.h"
#include "opus.h"

#include "stick_s3_board.h"
#include "voice_ble.h"

static const char *TAG = "audio_pipeline";

#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_CHANNELS 1
#define AUDIO_FRAME_MS 60
#define AUDIO_FRAME_SAMPLES ((AUDIO_SAMPLE_RATE * AUDIO_FRAME_MS) / 1000)
#define OPUS_BITRATE 20000
#define OPUS_MAX_PACKET_SIZE 220
#define OPUS_COMPLEXITY 1

#define TX_QUEUE_DEPTH 50
#define TX_RETRY_DELAY_MS 30
#define TX_MAX_RETRIES 50
#define TX_DRAIN_TIMEOUT_MS 500
#define TASK_EXIT_WAIT_MS 800

#define CHIME_TASK_STACK 4096
#define CHIME_TASK_PRIORITY 5
#define CHIME_GATE_WAIT_MS 1000
#define CHIME_BUFFER_SAMPLES 160
#define CHIME_FADE_SAMPLES 80
#define CHIME_VOLUME 60
#define CHIME_PEAK_AMPLITUDE 10000

typedef struct {
    uint32_t session_id;
    uint32_t seq;
    uint8_t  flags;
    uint16_t len;
    uint8_t  data[OPUS_MAX_PACKET_SIZE];
} audio_packet_t;

static atomic_bool s_running;
static atomic_bool s_recording_requested;
static atomic_bool s_chime_running;
static atomic_bool s_chime_cancel;
static bool s_initialized;
static uint32_t s_session_id;
static uint32_t s_seq;
static TaskHandle_t s_audio_task;
static TaskHandle_t s_tx_task;
static QueueHandle_t s_tx_queue;
static SemaphoreHandle_t s_audio_resource_gate;

/* Per-session resources: created on start, destroyed on stop */
static i2s_chan_handle_t s_rx_handle;
static i2s_chan_handle_t s_tx_handle;
static esp_codec_dev_handle_t s_codec;
static const audio_codec_ctrl_if_t *s_ctrl_if;
static const audio_codec_data_if_t *s_data_if;
static const audio_codec_gpio_if_t *s_gpio_if;
static const audio_codec_if_t *s_codec_if;
static OpusEncoder *s_opus_encoder;

static bool tasks_exited(void)
{
    return s_audio_task == NULL && s_tx_task == NULL;
}

static esp_err_t wait_for_tasks_to_exit(TickType_t timeout_ticks)
{
    TickType_t deadline = xTaskGetTickCount() + timeout_ticks;
    while (!tasks_exited()) {
        if (xTaskGetTickCount() >= deadline) {
            ESP_LOGW(TAG, "tasks still exiting: audio=%p tx=%p",
                     s_audio_task, s_tx_task);
            return ESP_ERR_TIMEOUT;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return ESP_OK;
}

static esp_err_t init_i2s(bool playback)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg,
                                        playback ? &s_tx_handle : NULL,
                                        playback ? NULL : &s_rx_handle),
                        TAG, "create i2s channel");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = STICK_S3_PIN_ES8311_MCLK,
            .bclk = STICK_S3_PIN_ES8311_BCLK,
            .ws = STICK_S3_PIN_ES8311_LRCK,
            .dout = STICK_S3_PIN_ES8311_DIN,
            .din = STICK_S3_PIN_ES8311_DOUT,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    std_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    i2s_chan_handle_t handle = playback ? s_tx_handle : s_rx_handle;
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(handle, &std_cfg),
                        TAG, "init i2s channel");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(handle), TAG, "enable i2s channel");
    return ESP_OK;
}

static esp_err_t init_codec(bool playback)
{
    i2c_master_bus_handle_t i2c_bus = stick_s3_board_i2c_bus();
    ESP_RETURN_ON_FALSE(i2c_bus != NULL, ESP_ERR_INVALID_STATE, TAG, "i2c bus unavailable");

    audio_codec_i2c_cfg_t i2c_cfg = {
        .port = I2C_NUM_1,
        .addr = ES8311_CODEC_DEFAULT_ADDR,
        .bus_handle = i2c_bus,
    };
    s_ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    ESP_RETURN_ON_FALSE(s_ctrl_if != NULL, ESP_ERR_NO_MEM, TAG, "create codec i2c ctrl");

    audio_codec_i2s_cfg_t i2s_cfg = {
        .port = I2S_NUM_1,
        .rx_handle = s_rx_handle,
        .tx_handle = s_tx_handle,
    };
    s_data_if = audio_codec_new_i2s_data(&i2s_cfg);
    ESP_RETURN_ON_FALSE(s_data_if != NULL, ESP_ERR_NO_MEM, TAG, "create codec i2s data");

    s_gpio_if = audio_codec_new_gpio();
    ESP_RETURN_ON_FALSE(s_gpio_if != NULL, ESP_ERR_NO_MEM, TAG, "create codec gpio");

    es8311_codec_cfg_t es8311_cfg = {
        .ctrl_if = s_ctrl_if,
        .gpio_if = s_gpio_if,
        .codec_mode = playback ? ESP_CODEC_DEV_WORK_MODE_DAC
                               : ESP_CODEC_DEV_WORK_MODE_ADC,
        .pa_pin = -1,
        .pa_reverted = false,
        .master_mode = false,
        .use_mclk = true,
        .digital_mic = false,
        .invert_mclk = false,
        .invert_sclk = false,
        .hw_gain = {
            .pa_voltage = 5.0,
            .codec_dac_voltage = 3.3,
        },
    };
    s_codec_if = es8311_codec_new(&es8311_cfg);
    ESP_RETURN_ON_FALSE(s_codec_if != NULL, ESP_ERR_NO_MEM, TAG, "create es8311");

    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = playback ? ESP_CODEC_DEV_TYPE_OUT : ESP_CODEC_DEV_TYPE_IN,
        .codec_if = s_codec_if,
        .data_if = s_data_if,
    };
    s_codec = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(s_codec != NULL, ESP_ERR_NO_MEM, TAG, "create codec dev");

    esp_codec_dev_sample_info_t sample_cfg = {
        .bits_per_sample = I2S_DATA_BIT_WIDTH_16BIT,
        .channel = 1,
        .channel_mask = I2S_STD_SLOT_LEFT,
        .sample_rate = AUDIO_SAMPLE_RATE,
        .mclk_multiple = 0,
    };
    ESP_RETURN_ON_FALSE(esp_codec_dev_open(s_codec, &sample_cfg) == ESP_CODEC_DEV_OK,
                        ESP_FAIL, TAG, "open codec");
    if (playback) {
        ESP_RETURN_ON_FALSE(esp_codec_dev_set_out_vol(s_codec, CHIME_VOLUME) == ESP_CODEC_DEV_OK,
                            ESP_FAIL, TAG, "set speaker volume");
    } else {
        ESP_RETURN_ON_FALSE(esp_codec_dev_set_in_gain(s_codec, 36.0) == ESP_CODEC_DEV_OK,
                            ESP_FAIL, TAG, "set mic gain");
    }
    return ESP_OK;
}

static esp_err_t init_opus(void)
{
    int error = OPUS_OK;
    s_opus_encoder = opus_encoder_create(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS,
                                         OPUS_APPLICATION_VOIP, &error);
    ESP_RETURN_ON_FALSE(s_opus_encoder != NULL && error == OPUS_OK,
                        ESP_FAIL, TAG, "create opus encoder error=%d", error);
    opus_encoder_ctl(s_opus_encoder, OPUS_SET_VBR(0));
    opus_encoder_ctl(s_opus_encoder, OPUS_SET_BITRATE(OPUS_BITRATE));
    opus_encoder_ctl(s_opus_encoder, OPUS_SET_DTX(0));
    opus_encoder_ctl(s_opus_encoder, OPUS_SET_COMPLEXITY(OPUS_COMPLEXITY));
    opus_encoder_ctl(s_opus_encoder, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    return ESP_OK;
}

static void deinit_opus(void)
{
    if (s_opus_encoder) {
        opus_encoder_destroy(s_opus_encoder);
        s_opus_encoder = NULL;
    }
}

static void deinit_codec(void)
{
    if (s_codec) {
        esp_codec_dev_close(s_codec);
        esp_codec_dev_delete(s_codec);
        s_codec = NULL;
    }
    if (s_codec_if) {
        audio_codec_delete_codec_if(s_codec_if);
        s_codec_if = NULL;
    }
    if (s_data_if) {
        audio_codec_delete_data_if(s_data_if);
        s_data_if = NULL;
    }
    if (s_gpio_if) {
        audio_codec_delete_gpio_if(s_gpio_if);
        s_gpio_if = NULL;
    }
    if (s_ctrl_if) {
        audio_codec_delete_ctrl_if(s_ctrl_if);
        s_ctrl_if = NULL;
    }
}

static void deinit_i2s(void)
{
    if (s_rx_handle) {
        /* Channel is already disabled by codec close; just release it. */
        i2s_del_channel(s_rx_handle);
        s_rx_handle = NULL;
    }
    if (s_tx_handle) {
        /* Channel is already disabled by codec close; just release it. */
        i2s_del_channel(s_tx_handle);
        s_tx_handle = NULL;
    }
}

static void deinit_session_resources(void)
{
    deinit_opus();
    deinit_codec();
    deinit_i2s();
    ESP_LOGI(TAG, "session resources released");
}

static const int16_t s_sine_table[32] = {
    0, 6393, 12539, 18204, 23170, 27245, 30273, 32138,
    32767, 32138, 30273, 27245, 23170, 18204, 12539, 6393,
    0, -6393, -12539, -18204, -23170, -27245, -30273, -32138,
    -32767, -32138, -30273, -27245, -23170, -18204, -12539, -6393,
};

static bool chime_cancelled(void)
{
    return atomic_load(&s_chime_cancel) || atomic_load(&s_recording_requested);
}

static esp_err_t write_chime_tone(uint32_t frequency_hz, uint32_t duration_ms)
{
    int16_t samples[CHIME_BUFFER_SAMPLES];
    const uint32_t total_samples = AUDIO_SAMPLE_RATE * duration_ms / 1000;
    const uint32_t phase_step = (uint32_t)(((uint64_t)frequency_hz << 32) /
                                           AUDIO_SAMPLE_RATE);
    uint32_t phase = 0;

    for (uint32_t offset = 0; offset < total_samples;) {
        if (chime_cancelled()) {
            return ESP_ERR_INVALID_STATE;
        }

        uint32_t count = total_samples - offset;
        if (count > CHIME_BUFFER_SAMPLES) {
            count = CHIME_BUFFER_SAMPLES;
        }
        for (uint32_t i = 0; i < count; ++i) {
            const uint32_t position = offset + i;
            int32_t amplitude = CHIME_PEAK_AMPLITUDE;
            if (position < CHIME_FADE_SAMPLES) {
                amplitude = amplitude * position / CHIME_FADE_SAMPLES;
            }
            const uint32_t remaining = total_samples - position;
            if (remaining < CHIME_FADE_SAMPLES) {
                amplitude = amplitude * remaining / CHIME_FADE_SAMPLES;
            }

            const int32_t wave = s_sine_table[phase >> 27];
            samples[i] = (int16_t)(wave * amplitude / 32767);
            phase += phase_step;
        }

        int err = esp_codec_dev_write(s_codec, samples, count * sizeof(samples[0]));
        if (err != ESP_CODEC_DEV_OK) {
            return ESP_FAIL;
        }
        offset += count;
    }
    return ESP_OK;
}

static esp_err_t write_chime_silence(uint32_t duration_ms)
{
    int16_t silence[CHIME_BUFFER_SAMPLES] = {0};
    uint32_t remaining = AUDIO_SAMPLE_RATE * duration_ms / 1000;
    while (remaining > 0) {
        if (chime_cancelled()) {
            return ESP_ERR_INVALID_STATE;
        }
        uint32_t count = remaining > CHIME_BUFFER_SAMPLES ? CHIME_BUFFER_SAMPLES : remaining;
        int err = esp_codec_dev_write(s_codec, silence, count * sizeof(silence[0]));
        if (err != ESP_CODEC_DEV_OK) {
            return ESP_FAIL;
        }
        remaining -= count;
    }
    return ESP_OK;
}

static void success_chime_task(void *arg)
{
    (void)arg;
    bool gate_acquired = false;
    bool speaker_enabled = false;
    esp_err_t err = ESP_OK;

    if (xSemaphoreTake(s_audio_resource_gate,
                       pdMS_TO_TICKS(CHIME_GATE_WAIT_MS)) != pdTRUE) {
        err = ESP_ERR_TIMEOUT;
        ESP_LOGW(TAG, "success chime timed out waiting for audio resources");
        goto done;
    }
    gate_acquired = true;

    if (chime_cancelled()) {
        goto done;
    }

    err = init_i2s(true);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "success chime i2s init failed: %s", esp_err_to_name(err));
        goto done;
    }
    err = init_codec(true);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "success chime codec init failed: %s", esp_err_to_name(err));
        goto done;
    }

    err = stick_s3_board_set_speaker_amp(true);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "enable speaker amp failed: %s", esp_err_to_name(err));
        goto done;
    }
    speaker_enabled = true;
    vTaskDelay(pdMS_TO_TICKS(5));

    err = write_chime_tone(659, 90);
    if (err == ESP_OK) {
        err = write_chime_silence(20);
    }
    if (err == ESP_OK) {
        err = write_chime_tone(880, 150);
    }
    if (err == ESP_OK) {
        err = write_chime_silence(20);
    }

done:
    if (speaker_enabled) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(stick_s3_board_set_speaker_amp(false));
    }
    deinit_codec();
    deinit_i2s();
    if (gate_acquired) {
        xSemaphoreGive(s_audio_resource_gate);
    }
    if (err == ESP_OK && !chime_cancelled()) {
        ESP_LOGI(TAG, "success chime complete");
    } else if (chime_cancelled()) {
        ESP_LOGI(TAG, "success chime cancelled for recording");
    } else {
        ESP_LOGW(TAG, "success chime failed: %s", esp_err_to_name(err));
    }
    atomic_store(&s_chime_running, false);
    vTaskDelete(NULL);
}

static void audio_task(void *arg)
{
    (void)arg;
    int16_t mono[AUDIO_FRAME_SAMPLES];
    uint8_t opus_buf[OPUS_MAX_PACKET_SIZE];
    uint32_t enqueued = 0;
    uint32_t dropped = 0;

    while (atomic_load(&s_running)) {
        esp_err_t err = esp_codec_dev_read(s_codec, mono, sizeof(mono));
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "codec read failed: %s", esp_err_to_name(err));
            continue;
        }

        opus_int32 encoded = opus_encode(s_opus_encoder, mono, AUDIO_FRAME_SAMPLES,
                                         opus_buf, sizeof(opus_buf));
        if (encoded < 0) {
            ESP_LOGE(TAG, "opus encode failed: %d", (int)encoded);
            continue;
        }

        audio_packet_t pkt = {
            .session_id = s_session_id,
            .seq = s_seq,
            .flags = (s_seq == 0) ? VOICE_BLE_FLAG_START : 0x00,
            .len = (uint16_t)encoded,
        };
        memcpy(pkt.data, opus_buf, encoded);

        if (xQueueSend(s_tx_queue, &pkt, 0) == pdTRUE) {
            s_seq++;
            enqueued++;
        } else {
            /* Queue full: drop oldest packet to make room */
            audio_packet_t discard;
            xQueueReceive(s_tx_queue, &discard, 0);
            xQueueSend(s_tx_queue, &pkt, 0);
            s_seq++;
            enqueued++;
            dropped++;
            if (dropped == 1 || (dropped % 20) == 0) {
                ESP_LOGW(TAG, "tx queue overflow, dropped oldest (total=%" PRIu32 ")", dropped);
            }
        }
    }

    ESP_LOGI(TAG, "audio task exit: enqueued=%" PRIu32 " overflow_drops=%" PRIu32,
             enqueued, dropped);
    s_audio_task = NULL;
    vTaskDelete(NULL);
}

static void tx_task(void *arg)
{
    (void)arg;
    audio_packet_t pkt;
    uint32_t sent = 0;
    uint32_t tx_dropped = 0;

    while (true) {
        if (xQueueReceive(s_tx_queue, &pkt, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        /* Sentinel: END flag with no payload signals drain mode */
        if (pkt.flags == VOICE_BLE_FLAG_END && pkt.len == 0) {
            goto drain;
        }

        int retries = 0;
        while (true) {
            esp_err_t err = voice_ble_send_audio(pkt.session_id, pkt.seq,
                                                 pkt.flags, pkt.data, pkt.len);
            if (err == ESP_OK) {
                sent++;
                break;
            }
            retries++;
            if (retries >= TX_MAX_RETRIES) {
                tx_dropped++;
                if (tx_dropped == 1 || (tx_dropped % 20) == 0) {
                    ESP_LOGW(TAG, "tx failed after %d retries seq=%" PRIu32
                             " (total_dropped=%" PRIu32 ")",
                             TX_MAX_RETRIES, pkt.seq, tx_dropped);
                }
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(TX_RETRY_DELAY_MS));
        }
    }

drain:
    {
        TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(TX_DRAIN_TIMEOUT_MS);
        while (xQueueReceive(s_tx_queue, &pkt, 0) == pdTRUE) {
            if (pkt.flags == VOICE_BLE_FLAG_END && pkt.len == 0) {
                break;
            }
            if (xTaskGetTickCount() >= deadline) {
                UBaseType_t remaining = uxQueueMessagesWaiting(s_tx_queue);
                if (remaining > 0) {
                    ESP_LOGW(TAG, "drain timeout, discarding %u packets",
                             (unsigned)remaining);
                }
                xQueueReset(s_tx_queue);
                break;
            }
            int retries = 0;
            while (true) {
                esp_err_t err = voice_ble_send_audio(pkt.session_id, pkt.seq,
                                                     pkt.flags, pkt.data, pkt.len);
                if (err == ESP_OK) {
                    sent++;
                    break;
                }
                retries++;
                if (retries >= TX_MAX_RETRIES || xTaskGetTickCount() >= deadline) {
                    tx_dropped++;
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(TX_RETRY_DELAY_MS));
            }
        }

        /* Send the END marker over BLE */
        voice_ble_send_audio(s_session_id, s_seq, VOICE_BLE_FLAG_END, NULL, 0);

        ESP_LOGI(TAG, "tx task exit: sent=%" PRIu32 " dropped=%" PRIu32, sent, tx_dropped);

        /* Wait for audio_task to finish before destroying shared resources */
        while (s_audio_task != NULL) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        deinit_session_resources();
        s_tx_task = NULL;
        xSemaphoreGive(s_audio_resource_gate);
        vTaskDelete(NULL);
    }
}

esp_err_t audio_pipeline_init(void)
{
    if (s_initialized) {
        return ESP_OK;
    }

    s_tx_queue = xQueueCreate(TX_QUEUE_DEPTH, sizeof(audio_packet_t));
    ESP_RETURN_ON_FALSE(s_tx_queue != NULL, ESP_ERR_NO_MEM, TAG, "create tx queue");

    s_audio_resource_gate = xSemaphoreCreateBinary();
    if (!s_audio_resource_gate) {
        vQueueDelete(s_tx_queue);
        s_tx_queue = NULL;
        return ESP_ERR_NO_MEM;
    }
    xSemaphoreGive(s_audio_resource_gate);

    s_initialized = true;
    ESP_LOGI(TAG, "audio pipeline ready (resources allocated on demand)");
    return ESP_OK;
}

esp_err_t audio_pipeline_start(uint32_t session_id)
{
    ESP_RETURN_ON_FALSE(s_initialized, ESP_ERR_INVALID_STATE, TAG, "not initialized");
    if (atomic_load(&s_running)) {
        return ESP_OK;
    }

    atomic_store(&s_recording_requested, true);
    atomic_store(&s_chime_cancel, true);

    esp_err_t err = wait_for_tasks_to_exit(pdMS_TO_TICKS(TASK_EXIT_WAIT_MS));
    if (err != ESP_OK) {
        atomic_store(&s_recording_requested, false);
        ESP_LOGE(TAG, "wait previous session exit: %s", esp_err_to_name(err));
        return err;
    }
    if (xSemaphoreTake(s_audio_resource_gate,
                       pdMS_TO_TICKS(TASK_EXIT_WAIT_MS)) != pdTRUE) {
        atomic_store(&s_recording_requested, false);
        ESP_LOGE(TAG, "wait for audio resources timed out");
        return ESP_ERR_TIMEOUT;
    }

    err = init_i2s(false);
    if (err != ESP_OK) {
        deinit_i2s();
        xSemaphoreGive(s_audio_resource_gate);
        atomic_store(&s_recording_requested, false);
        ESP_LOGE(TAG, "i2s init: %s", esp_err_to_name(err));
        return err;
    }
    err = init_codec(false);
    if (err != ESP_OK) {
        deinit_codec();
        deinit_i2s();
        xSemaphoreGive(s_audio_resource_gate);
        atomic_store(&s_recording_requested, false);
        ESP_LOGE(TAG, "codec init: %s", esp_err_to_name(err));
        return err;
    }
    err = init_opus();
    if (err != ESP_OK) {
        deinit_codec();
        deinit_i2s();
        xSemaphoreGive(s_audio_resource_gate);
        atomic_store(&s_recording_requested, false);
        ESP_LOGE(TAG, "opus init: %s", esp_err_to_name(err));
        return err;
    }

    voice_ble_request_fast_interval();

    xQueueReset(s_tx_queue);
    s_session_id = session_id;
    s_seq = 0;
    opus_encoder_ctl(s_opus_encoder, OPUS_RESET_STATE);
    atomic_store(&s_running, true);

    BaseType_t ok = xTaskCreatePinnedToCore(tx_task, "audio_tx", 4096,
                                            NULL, 6, &s_tx_task, 0);
    if (ok != pdPASS) {
        atomic_store(&s_running, false);
        atomic_store(&s_recording_requested, false);
        s_tx_task = NULL;
        deinit_session_resources();
        xSemaphoreGive(s_audio_resource_gate);
        return ESP_ERR_NO_MEM;
    }

    ok = xTaskCreatePinnedToCore(audio_task, "audio_pipeline", 32768,
                                 NULL, 5, &s_audio_task, 1);
    if (ok != pdPASS) {
        atomic_store(&s_running, false);
        s_audio_task = NULL;
        audio_packet_t sentinel = {
            .session_id = s_session_id,
            .seq = s_seq,
            .flags = VOICE_BLE_FLAG_END,
            .len = 0,
        };
        xQueueSend(s_tx_queue, &sentinel, portMAX_DELAY);
        atomic_store(&s_recording_requested, false);
        (void)wait_for_tasks_to_exit(pdMS_TO_TICKS(TASK_EXIT_WAIT_MS));
        /* tx_task cleans up session resources on exit */
        return ESP_ERR_NO_MEM;
    }
    atomic_store(&s_recording_requested, false);
    ESP_LOGI(TAG, "start session %" PRIu32, session_id);
    return ESP_OK;
}

esp_err_t audio_pipeline_stop(void)
{
    if (!atomic_load(&s_running)) {
        return ESP_OK;
    }
    atomic_store(&s_running, false);
    ESP_LOGI(TAG, "stop session %" PRIu32, s_session_id);

    /* Send sentinel to trigger tx_task drain and exit */
    audio_packet_t sentinel = {
        .session_id = s_session_id,
        .seq = s_seq,
        .flags = VOICE_BLE_FLAG_END,
        .len = 0,
    };
    xQueueSend(s_tx_queue, &sentinel, portMAX_DELAY);
    return ESP_OK;
}

esp_err_t audio_pipeline_play_success_chime(void)
{
    ESP_RETURN_ON_FALSE(s_initialized, ESP_ERR_INVALID_STATE, TAG, "not initialized");
    if (atomic_load(&s_chime_running)) {
        return ESP_OK;
    }

    atomic_store(&s_chime_cancel, false);
    if (atomic_load(&s_running) || atomic_load(&s_recording_requested)) {
        ESP_LOGD(TAG, "skip success chime while recording is active");
        return ESP_OK;
    }
    if (atomic_exchange(&s_chime_running, true)) {
        return ESP_OK;
    }

    BaseType_t ok = xTaskCreatePinnedToCore(success_chime_task, "success_chime",
                                            CHIME_TASK_STACK, NULL,
                                            CHIME_TASK_PRIORITY, NULL, 1);
    if (ok != pdPASS) {
        atomic_store(&s_chime_running, false);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

uint32_t audio_pipeline_session_id(void)
{
    return s_session_id;
}
