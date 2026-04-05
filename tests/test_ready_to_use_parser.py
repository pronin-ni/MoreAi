from app.integrations.definitions import READY_TO_USE_DEFINITIONS, load_ready_to_use_markdown
from app.integrations.parser_ready_to_use import parse_ready_to_use_markdown


class TestReadyToUseParser:
    def test_extracts_all_base_urls(self):
        parsed = parse_ready_to_use_markdown(load_ready_to_use_markdown())

        assert len(parsed.base_urls) == 7
        assert {item["base_url"] for item in parsed.base_urls} == {
            "https://localhost:1337/v1",
            "https://g4f.space/api/groq",
            "https://g4f.space/api/ollama",
            "https://g4f.space/api/pollinations",
            "https://g4f.space/api/nvidia",
            "https://g4f.space/api/gemini",
            "https://g4f.space/v1",
        }

    def test_extracts_all_supported_routes(self):
        parsed = parse_ready_to_use_markdown(load_ready_to_use_markdown())

        assert len(parsed.supported_api_routes) == 11
        assert {item["display_name"] for item in parsed.supported_api_routes} == {
            "Nvidia",
            "DeepInfra",
            "OpenRouter",
            "Google Gemini",
            "xAI",
            "Together",
            "OpenAI",
            "TypeGPT",
            "Grok",
            "ApiAirforce",
            "Auto Provider & Model Selection",
        }

    def test_extracts_all_individual_clients(self):
        parsed = parse_ready_to_use_markdown(load_ready_to_use_markdown())

        assert len(parsed.individual_clients) == 7
        assert {item["display_name"] for item in parsed.individual_clients} == {
            "Pollinations AI",
            "Puter AI",
            "HuggingFace",
            "Ollama",
            "Gemini",
            "OpenAI Chat",
            "Perplexity",
        }

    def test_builds_expected_definition_count(self):
        assert len(READY_TO_USE_DEFINITIONS) == 25
