# MoreAI Proxy

OpenAI-compatible FastAPI proxy with two execution transports behind one unified model namespace:

1. `browser/*` for Playwright/browser providers
2. `api/*` for OpenAI-compatible upstream APIs and g4f integrations

## Namespace

Canonical browser models:

- `browser/qwen`
- `browser/glm`
- `browser/chatgpt`
- `browser/yandex`
- `browser/kimi`
- `browser/deepseek`

Backward-compatible aliases still work:

- `qwen -> browser/qwen`
- `internal-web-chat -> browser/qwen`
- `glm -> browser/glm`
- `chatgpt -> browser/chatgpt`
- `yandex -> browser/yandex`
- `kimi -> browser/kimi`
- `deepseek -> browser/deepseek`

Canonical API models use:

- `api/<integration>/<model>`

Examples:

- `api/g4f-groq/llama-3.3-70b`
- `api/g4f-auto/default`
- `api/openrouter/gpt-4o-mini`

## Architecture

The routing stack is split into three layers:

1. `browser_registry`
Browser-only Playwright providers.

2. `api_registry`
OpenAI-compatible and client-based integrations discovered from `g4f.dev/docs/ready_to_use.html`.

3. `unified_registry`
Facade that aggregates browser and API models, resolves aliases, and returns execution strategy.

Core abstractions:

- `IntegrationDefinition`
- `OpenAICompatibleIntegration`
- `ClientBasedIntegration`
- `ProviderRegistry`
- `APIRegistry`
- `UnifiedRegistry`

## Ready-To-Use Integrations Parsed From Source

Source of truth: `https://g4f.dev/docs/ready_to_use.html`

### Base URLs Table

| Integration | Base URL | API key | Notes |
|-------------|----------|---------|-------|
| `g4f-localhost` | `https://localhost:1337/v1` | none required | use it locally |
| `g4f-groq` | `https://g4f.space/api/groq` | none required | Use Groq provider |
| `g4f-ollama` | `https://g4f.space/api/ollama` | none required | Use Ollama provider |
| `g4f-pollinations` | `https://g4f.space/api/pollinations` | none required | Proxy for pollinations.ai |
| `g4f-nvidia` | `https://g4f.space/api/nvidia` | none required | Use Nvidia provider |
| `g4f-gemini` | `https://g4f.space/api/gemini` | none required | Hosted Gemini provider |
| `g4f-hosted` | `https://g4f.space/v1` | required | Hosted instance, many models |

### Also Supported API Routes

- `nvidia-api` → `https://integrate.api.nvidia.com/v1`
- `deepinfra` → `https://api.deepinfra.com/v1`
- `openrouter` → `https://openrouter.ai/api/v1`
- `gemini-openai` → `https://generativelanguage.googleapis.com/v1beta/openai`
- `xai` → `https://api.x.ai/v1`
- `together` → `https://api.together.xyz/v1`
- `openai` → `https://api.openai.com/v1`
- `typegpt` → `https://typegpt.ai/api`
- `grok` → `https://api.grok.com/v1`
- `apiairforce` → `https://api.airforce/v1`
- `g4f-auto` → `https://g4f.space/api/auto`

### Individual Clients

- `g4f-client-pollinations`
- `g4f-client-puter`
- `g4f-client-huggingface`
- `g4f-client-ollama`
- `g4f-client-gemini`
- `g4f-client-openai-chat`
- `g4f-client-perplexity`

Client-based integrations are represented separately from OpenAI-compatible upstreams, even when a client can reuse the same HTTP adapter internally.

## Configuration

Base runtime config lives in `.env`.

Important env variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `INTEGRATIONS_ENABLED` | Global switch for API integrations | `true` |
| `INTEGRATIONS_AUTO_DISCOVER_MODELS` | Probe `/models` at startup | `true` |
| `INTEGRATIONS_DISCOVERY_TIMEOUT_SECONDS` | Discovery timeout | `10` |
| `INTEGRATIONS_RETRY_ATTEMPTS` | Retry count for API requests | `1` |
| `INTEGRATIONS_ALLOW_FALLBACK_MODELS` | Allow fallback model IDs when `/models` fails | `true` |
| `INTEGRATIONS_CONFIG_PATH` | TOML overrides for per-integration config | `./config/integrations.toml` |
| `INTEGRATIONS_RATE_LIMIT_COOLDOWN_SECONDS` | Cooldown after upstream `429` | `60` |
| `G4F_API_KEY` | Shared token for all `g4f-*` integrations | unset |

Per-integration overrides live in TOML.

Example file: `config/integrations.example.toml`

Example sections:

```toml
[integrations.g4f_groq]
enabled = true

[integrations.g4f_hosted]
enabled = false
api_key = ""

[integrations.openrouter]
enabled = false
api_key = ""

[integrations.g4f_auto]
enabled = true
fallback_models = ["default"]
```

Behavior rules:

1. Base-table integrations marked `none required` auto-enable by default.
2. `g4f-hosted` is disabled by default until configured.
3. `G4F_API_KEY` is applied automatically to every `g4f-*` integration unless a more specific `INTEGRATION_<ID>_API_KEY` env override exists.
4. Supported API routes use explicit TOML or per-integration env config because the source page does not state their auth requirements.
5. Client-based integrations are registered separately and can be enabled independently.

Portainer-friendly shared g4f token:

```env
G4F_API_KEY=your_shared_g4f_token
```

Optional per-integration override example:

```env
INTEGRATION_G4F_HOSTED_API_KEY=your_override_token
```

## Startup Discovery

At app startup:

1. Browser pool initializes.
2. `unified_registry.initialize()` loads API definitions.
3. Enabled API integrations try to probe `/models`.
4. If probing fails, fallback models are used when configured.
5. If an upstream returns `429`, that integration enters a temporary cooldown and requests can fall back to another integration exposing the same upstream model name.
6. One failed integration does not stop the service.

## Routing

`/v1/chat/completions` uses `unified_registry.resolve_model(model)`.

Routing behavior:

1. Browser aliases resolve to canonical `browser/*` IDs.
2. `transport=browser` goes through Playwright providers.
3. `transport=api` goes through OpenAI-compatible or client-based API adapters.

## `/v1/models`

`/v1/models` returns canonical models only.

Browser aliases are not listed as primary entries, but they still resolve in routing.

Example response shape:

```json
{
  "object": "list",
  "data": [
    {
      "id": "browser/qwen",
      "object": "model",
      "created": 1775380000,
      "owned_by": "qwen",
      "provider_id": "qwen",
      "transport": "browser",
      "source_type": "browser",
      "enabled": true,
      "available": true
    },
    {
      "id": "api/g4f-auto/default",
      "object": "model",
      "created": 1775380000,
      "owned_by": "g4f-auto",
      "provider_id": "g4f-auto",
      "transport": "api",
      "source_type": "g4f_openai",
      "enabled": true,
      "available": true
    }
  ]
}
```

## Diagnostics

Available endpoints:

- `GET /health`
- `GET /v1/models`
- `GET /diagnostics/integrations`
- `GET /diagnostics/models`

Diagnostics include:

- enabled integrations
- available integrations
- API key requirements
- disabled reasons
- discovered models

## Browser Providers

Browser providers remain supported and now use canonical names:

- `browser/qwen`
- `browser/glm`
- `browser/chatgpt`
- `browser/yandex`
- `browser/kimi`
- `browser/deepseek`

DeepSeek-specific notes:

1. The app stores DeepSeek Playwright auth state automatically at `./secrets/deepseek.storage_state.json`.
2. `DEEPSEEK_LOGIN` and `DEEPSEEK_PASSWORD` are only needed to refresh an expired session.
3. `Глубокое мышление` and `Умный поиск` are disabled before every send.

## Syncing The Source Page

To refresh the checked-in `ready_to_use` snapshot when upstream docs change:

```bash
uv run python scripts/sync_g4f_ready_to_use.py
```

This updates `app/integrations/ready_to_use_snapshot.md`, which is then parsed into integration definitions.

## Tests

```bash
uv run pytest -v
```

Covered areas:

- ready-to-use parser
- browser canonical model aliases
- API adapter discovery and fallback
- unified `/v1/models`
- diagnostics endpoint
- disabled integrations without API key

## Development Notes

If you want to add a new integration when `ready_to_use.html` changes:

1. Sync the snapshot.
2. Update `app/integrations/definitions.py` mapping if a new source entry needs a new provider ID.
3. Add TOML config overrides if the new integration needs auth or custom fallback models.
4. Add tests for parsing and discovery behavior.
