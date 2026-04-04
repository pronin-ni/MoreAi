# MoreAI Proxy - OpenAI-Compatible API Proxy with Browser Automation

## Overview

Production-oriented backend service that works as an OpenAI-compatible API proxy for multiple browser-based AI chat providers. Uses browser automation (Playwright) to interact with various chat interfaces.

## Supported Providers

| Model | Provider | URL | Description |
|-------|----------|-----|-------------|
| `internal-web-chat` | Qwen | https://chat.qwen.ai/ | Qwen Chat (default) |
| `glm` | GLM | https://chat.z.ai/ | Z.ai GLM Chat |
| `chatgpt` | ChatGPT | https://chatgpt.com/ | OpenAI ChatGPT |
| `yandex` | Alice-Yandex | https://alice.yandex.ru/ | Yandex Alice |
| `kimi` | Kimi | https://www.kimi.com/ | Moonshot Kimi via Google-authenticated browser session |

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   Client    │────▶│  FastAPI     │────▶│  Chat Proxy     │
│ (OpenAI SDK)│     │  Endpoints   │     │  Service        │
└─────────────┘     └──────────────┘     └────────┬────────┘
                                                  │
                    ┌──────────────┐     ┌────────▼────────┐
                    │  /v1/models  │     │  Session Pool  │
                    │  /health     │     │  (Browser Pages)│
                    └──────────────┘     └────────┬────────┘
                                                  │
                                           ┌──────▼──────┐
                                           │ Provider   │
                                           │ Registry   │
                                           └──────┬──────┘
                                                  │
                    ┌──────────────┬──────────────┼──────────────┐
                    │              │              │              │
              ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
              │  Qwen UI  │ │  GLM UI   │ │ChatGPT UI│ │   ...    │
              └───────────┘ └───────────┘ └───────────┘ └───────────┘
```

## Features

- **Multi-Provider Support**: Qwen, GLM, ChatGPT, Yandex, Kimi via unified API
- **OpenAI-Compatible API**: `/v1/chat/completions`, `/v1/models`, `/health`
- **Browser Automation**: Uses Playwright to interact with various chat interfaces
- **Session Pooling**: Concurrent request handling with exclusive browser sessions
- **Reusable Google Auth Bootstrap**: Shared credentials file plus provider-specific storage state for Google-authenticated providers like Kimi
- **Structured Logging**: JSON logs with request-id correlation
- **Error Handling**: Detailed error messages with screenshot/HTML artifacts

## Quick Start

### 1. Install Dependencies

```bash
# Using uv (recommended)
uv sync
playwright install chromium

# Or using pip
pip install -e ".[dev]"
playwright install chromium
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your settings
```

### 3. Run the Service

```bash
# Development
uvicorn app.main:app --reload --port 8000

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### 4. Test

```bash
# Health check
curl http://localhost:8000/health

# List models
curl http://localhost:8000/v1/models

# Chat completion with Qwen (default)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "internal-web-chat",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Chat completion with ChatGPT
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chatgpt",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Chat completion with GLM
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Chat completion with Kimi
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kimi",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Configuration

See `.env.example` for all configuration options.

### Key Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `INTERNAL_CHAT_URL` | Base URL of Qwen Chat | `https://chat.qwen.ai/` |
| `HEADLESS` | Run browser in headless mode | `true` |
| `BROWSER_POOL_SIZE` | Max concurrent browser sessions | `5` |
| `RESPONSE_TIMEOUT_SECONDS` | Max wait for response | `120` |
| `ARTIFACTS_DIR` | Directory for debug artifacts | `./artifacts` |

### Provider-Specific Settings

| Provider | Env Prefix | Key Settings |
|----------|-----------|--------------|
| Qwen | `QWEN_` | `QWEN_URL` |
| GLM | `GLM_` | `GLM_URL` |
| ChatGPT | `CHATGPT_` | `CHATGPT_URL` |
| Yandex | `YANDEX_` | `YANDEX_URL` |
| Kimi | `KIMI_` | `KIMI_URL`, `KIMI_STORAGE_STATE_PATH`, `KIMI_SKIP_AUTH_URL` |

### Shared Google Auth

Google-authenticated providers use a shared credentials file plus provider-specific browser storage state:

| Variable | Description | Default |
|----------|-------------|---------|
| `GOOGLE_AUTH_CREDENTIALS_PATH` | JSON file with shared Google credentials | `./secrets/browser_auth.json` |
| `GOOGLE_AUTH_AUTO_BOOTSTRAP` | Auto-run Google bootstrap when storage state is missing | `true` |
| `GOOGLE_AUTH_TIMEOUT_SECONDS` | Timeout for Google login steps | `180` |
| `KIMI_STORAGE_STATE_PATH` | Storage state file saved after successful Google login for Kimi | `./secrets/kimi.storage_state.json` |

Credentials file format:

```json
{
  "google": {
    "email": "your-google-login@example.com",
    "password": "your-google-password",
    "recovery_email": "optional-recovery@example.com"
  }
}
```

Bootstrap manually when needed:

```bash
python scripts/bootstrap_google_auth.py --model kimi
```

## Routing

The provider is selected based on the `model` field in the request:

```
model="chatgpt"   → ChatGPTProvider → https://chatgpt.com/
model="glm"       → GlmProvider     → https://chat.z.ai/
model="kimi"      → KimiProvider    → https://www.kimi.com/
model="qwen"      → QwenProvider    → https://chat.qwen.ai/
model="internal-web-chat" → QwenProvider → https://chat.qwen.ai/
```

## Kimi Recon Notes

Recon was run against `https://www.kimi.com/` and existing Playwright artifacts were used to lock the initial DOM flow before coding the provider.

Observed pre-auth flow:

1. Kimi lands on a chat shell immediately.
2. `New Chat` is available as `/?chat_enter_method=new_chat`.
3. The input is a custom editor exposed as an accessibility `textbox` and rendered as `.chat-input-editor`.
4. The send control is `.send-button-container` and becomes disabled when there is no message.
5. Real message send is blocked by a login wall with `Continue with Google`, `Phone number`, and `Verification code`.

Chosen Kimi locators and why:

| Page Signal | Primary Locator | Why |
|-------------|-----------------|-----|
| New chat/reset | `a.new-chat-btn[href="/?chat_enter_method=new_chat"]` | Stable URL-based navigation and visible sidebar action |
| Input | `get_by_role("textbox")` then `.chat-input-editor` | Accessibility first, stable custom editor fallback |
| Send | `.send-button-container:not(.disabled)` | Stable container class, works with custom SVG button |
| Login wall | text `Continue with Google` | Most explicit auth gate signal after attempted send |

Kimi provider behavior:

1. Open `https://www.kimi.com/`.
2. Detect the custom chat editor and dismiss obvious promotional overlays.
3. Reset context through `/?chat_enter_method=new_chat`.
4. Type into the custom editor via `textbox`/keyboard fallback.
5. Click the custom send container.
6. If Kimi raises the login wall, trigger Google auth bootstrap and save provider-specific storage state.
7. Wait for completion using explicit loading/stop signals first, then text-stability fallback.
8. Extract the response from assistant-like containers first, then from the visible chat container after filtering known chrome text.

## Discovered Selectors (from UI Recon)

After browser auto-discovery, these selectors were found to work with Qwen Chat:

| Element | Selector | Method |
|---------|----------|--------|
| Message Input | `textarea[placeholder*="Чем"]` | Placeholder-based |
| Send Button | `button:has(img[src*="send"])` | Image src pattern |
| Assistant Message | `main p:last-of-type` | DOM structure |
| New Chat | Navigate to `/` | URL-based reset |

### How selectors were discovered

1. **Navigation**: Opened https://chat.qwen.ai/ in Playwright
2. **Input**: Found textbox with placeholder "Чем я могу помочь вам сегодня?"
3. **Send button**: Located as 2nd button after input container
4. **Response**: Found in `<main>` section as `<p>` element
5. **New chat**: Navigate to root URL `/` resets to fresh chat

## Project Structure

```
app/
├── main.py                    # FastAPI app, lifespan, DI
├── api/
│   └── routes_openai.py       # OpenAI-compatible endpoints
├── schemas/
│   └── openai.py              # Pydantic models for OpenAI API
├── services/
│   └── chat_proxy_service.py  # Business logic orchestration
├── browser/
│   ├── internal_chat_client.py # Playwright client (discovered selectors)
│   ├── session_pool.py         # Browser session management
│   └── recon.py                # UI discovery utilities
├── core/
│   ├── config.py               # Pydantic Settings with real selectors
│   ├── logging.py              # Structured logging
│   └── errors.py               # Custom exceptions
└── utils/
    ├── message_parser.py       # Extract last user message
    └── openai_mapper.py        # Response mapping
scripts/
└── recon_chat_ui.py           # Standalone UI discovery script
tests/
├── test_message_parser.py
├── test_openai_mapper.py
├── test_api_routes.py
└── test_config.py
```

## API Endpoints

### GET /health
Returns service health status.

### GET /v1/models
Returns list of available models.

### POST /v1/chat/completions
Send a chat message to Qwen Chat via browser automation.

**Request:**
```json
{
  "model": "internal-web-chat",
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "temperature": 0.7,
  "max_tokens": 2048,
  "stream": false
}
```

**Response:**
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "internal-web-chat",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

## Streaming Support (Future)

Streaming is not supported in v1.0. Architecture is ready for SSE implementation:

1. Response built incrementally in chunks
2. Each chunk sent via `EventSourceResponse`
3. `stream=True` will be handled in `chat_proxy_service.py`

## Development

### Run Tests

```bash
make test
# or
pytest -v
```

### Code Quality

```bash
make lint   # ruff
make typecheck  # mypy
```

### Docker

```bash
docker-compose up --build
```

## UI Recon Script

Run the standalone reconnaissance script to discover selectors:

```bash
python scripts/recon_chat_ui.py --model kimi
```

This will:
1. Open the selected provider in browser
2. Explore provider-specific DOM structure
3. Test various selectors
4. Save artifacts (screenshots, HTML)
5. Output discovered selectors

## Troubleshooting

### Browser fails to start
- Ensure Playwright browsers are installed: `playwright install chromium`
- Check `HEADLESS=false` for debugging

### Selectors not found
- Run `python scripts/recon_chat_ui.py --model kimi` to re-discover Kimi selectors
- Check artifacts directory for screenshots on error

### Kimi login wall
- Put shared Google credentials in `./secrets/browser_auth.json`
- Run `python scripts/bootstrap_google_auth.py --model kimi` if auto-bootstrap is disabled or if you want to pre-seed storage state
- Check `KIMI_STORAGE_STATE_PATH` after bootstrap completes

### Timeout errors
- Increase `RESPONSE_TIMEOUT_SECONDS`
- Check Qwen Chat service availability

## License

MIT
