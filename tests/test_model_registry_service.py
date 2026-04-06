from unittest.mock import patch

from app.services.model_registry_service import service


class TestModelRegistryService:
    @patch(
        "app.services.model_registry_service.unified_registry.list_models",
        return_value=[
            {
                "id": "browser/qwen",
                "provider_id": "qwen",
                "transport": "browser",
                "source_type": "browser",
                "enabled": True,
                "available": True,
            },
            {
                "id": "api/ollamafreeapi/llama3.3:70b",
                "provider_id": "ollamafreeapi",
                "transport": "api",
                "source_type": "client_based",
                "enabled": True,
                "available": True,
            },
        ],
    )
    def test_group_models_includes_ollamafreeapi_in_api_panel(self, _mock_list_models):
        browser_models, api_models, agent_models = service.group_models()

        assert [model.id for model in browser_models] == ["browser/qwen"]
        assert [model.id for model in api_models] == ["api/ollamafreeapi/llama3.3:70b"]
        assert agent_models == []
        assert api_models[0].display_name == "Ollamafreeapi | llama3.3:70b"
