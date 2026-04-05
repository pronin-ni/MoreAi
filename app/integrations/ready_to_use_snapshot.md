# Documentation: API endpoints and usage

### Overview

- This collection exposes multiple base URLs (providers) for chat-style completions. Each entry in the table lists a base URL (with /models removed) and whether an API key is required.
- Base URL extraction: remove /models from the URL in your table to get the API base_url you should use in requests.

### Base URLs Table

| Base URLs                                                                       | API key       | Notes                                                                                         |
| ------------------------------------------------------------------------------- | ------------- | --------------------------------------------------------------------------------------------- |
| [https://localhost:1337/v1](https://localhost:1337/v1/models)                   | none required | use it locally                                                                                |
| [https://g4f.space/api/groq](https://g4f.space/api/groq/models)                 | none required | Use Groq provider                                                                             |
| [https://g4f.space/api/ollama](https://g4f.space/api/ollama/models)             | none required | Use Ollama provider                                                                           |
| [https://g4f.space/api/pollinations](https://g4f.space/api/pollinations/models) | none required | Proxy for pollinations.ai                                                                     |
| [https://g4f.space/api/nvidia](https://g4f.space/api/nvidia/models)             | none required | Use Nvidia provider                                                                           |
| [https://g4f.space/api/gemini](https://g4f.space/api/gemini/models)             | none required | Hosted Gemini provider                                                                        |
| [https://g4f.space/v1](https://g4f.space/v1/models)                             | required      | Hosted instance, many models, get key from [g4f.dev/api_key](https://g4f.dev/api_key.html) |

### Also Supported API Routes:

- **Nvidia**: <https://integrate.api.nvidia.com/v1>
- **DeepInfra**: <https://api.deepinfra.com/v1>
- **OpenRouter**: <https://openrouter.ai/api/v1>
- **Google Gemini**: <https://generativelanguage.googleapis.com/v1beta/openai>
- **xAI**: <https://api.x.ai/v1>
- **Together**: <https://api.together.xyz/v1>
- **OpenAI**: <https://api.openai.com/v1>
- **TypeGPT**: <https://typegpt.ai/api>
- **Grok**: <https://api.grok.com/v1>
- **ApiAirforce**: <https://api.airforce/v1>
- **Auto Provider & Model Selection**: <https://g4f.space/api/auto>

### Individual clients available for:

See the full [Providers Documentation](https://g4f.dev/docs/providers/) for detailed usage guides.

- [Pollinations AI](https://g4f.dev/docs/providers/pollinations.html)
- [Puter AI](https://g4f.dev/docs/providers/puter.html)
- [HuggingFace](https://g4f.dev/docs/providers/huggingface.html)
- [Ollama](https://g4f.dev/docs/providers/ollama.html)
- [Gemini](https://g4f.dev/docs/providers/gemini.html)
- [OpenAI Chat](https://g4f.dev/docs/providers/openai.html)
- [Perplexity](https://g4f.dev/docs/providers/perplexity.html)
