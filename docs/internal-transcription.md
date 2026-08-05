# NVIDIA Inference audio transcription

XC Buddy uses the existing NVIDIA Inference API through its OpenAI-compatible chat-completions endpoint. Audio is embedded as base64 `input_audio`; this is **not** the `/audio/transcriptions` multipart contract.

## Verified default

```toml
asr_provider = "openai_compatible"
openai_base_url = "https://inference-api.nvidia.com/v1"
openai_api_key = "<NVIDIA inference API key>"
openai_model = "gcp/google/gemini-3.6-flash"
openai_language = ""
openai_prompt = "Transcribe this audio accurately and verbatim. The speaker may mix Mandarin Chinese and English technical terms in the same sentence. Preserve each language, technical names, punctuation, and capitalization. Output transcript only."
```

The model is selected from the live `inference-nvidia` inventory. XC Buddy uses Gemini 3.6 Flash as a current, audio-capable multimodal model that supports the existing chat-completions `input_audio` contract. The model remains configurable so mixed English/Chinese recordings can be re-benchmarked as the inventory changes.

## Request contract

```http
POST https://inference-api.nvidia.com/v1/chat/completions
Authorization: Bearer <openai_api_key>
Content-Type: application/json
```

The request uses this shape:

```json
{
  "model": "gcp/google/gemini-3.6-flash",
  "messages": [{
    "role": "user",
    "content": [
      {
        "type": "input_audio",
        "input_audio": {
          "data": "<base64 Ogg/Opus>",
          "format": "ogg"
        }
      },
      {
        "type": "text",
        "text": "<transcription instruction plus hotwords>"
      }
    ]
  }],
  "temperature": 0,
  "max_tokens": 2048
}
```

XC Buddy reads the transcript from `choices[0].message.content`. Non-2xx responses, timeouts, missing content, and malformed JSON enter the existing error/recovery flow.

Leave `openai_language` blank for mixed Mandarin/English speech. Add project-specific names to `asr_hotwords`; XC Buddy appends them to the transcription instruction.

The API key is currently stored in the local XC Buddy `config.toml` for this personal v0.1 build. A Keychain migration can be added later.
