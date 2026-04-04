import pytest
from app.schemas.openai import ChatMessage
from app.utils.message_parser import extract_last_user_message, extract_text_content


class TestExtractLastUserMessage:
    def test_simple_string_content(self):
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello, how are you?"),
        ]
        
        result = extract_last_user_message(messages)
        
        assert result == "Hello, how are you?"

    def test_multiple_user_messages(self):
        messages = [
            ChatMessage(role="user", content="First message"),
            ChatMessage(role="user", content="Second message"),
            ChatMessage(role="user", content="Third message"),
        ]
        
        result = extract_last_user_message(messages)
        
        assert result == "Third message"

    def test_last_user_message_with_intermediate_assistant(self):
        messages = [
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hello! How can I help?"),
            ChatMessage(role="user", content="Tell me about Python"),
        ]
        
        result = extract_last_user_message(messages)
        
        assert result == "Tell me about Python"

    def test_no_user_message_raises_error(self):
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="assistant", content="Hello!"),
        ]
        
        with pytest.raises(ValueError, match="No user message found"):
            extract_last_user_message(messages)


class TestExtractTextContent:
    def test_string_content(self):
        content = "Simple text message"
        
        result = extract_text_content(content)
        
        assert result == "Simple text message"

    def test_list_of_text_parts(self):
        content = [
            {"type": "text", "text": "First part"},
            {"type": "text", "text": "Second part"},
        ]
        
        result = extract_text_content(content)
        
        assert result == "First part\nSecond part"

    def test_list_with_mixed_types(self):
        content = [
            {"type": "text", "text": "Text content"},
            {"type": "image_url", "image_url": {"url": "data:image/png..."}},
        ]
        
        result = extract_text_content(content)
        
        assert result == "Text content"

    def test_empty_list_raises_error(self):
        content = []
        
        with pytest.raises(ValueError, match="Cannot extract text"):
            extract_text_content(content)


class TestMessageParserEdgeCases:
    def test_empty_string_content(self):
        messages = [
            ChatMessage(role="user", content=""),
        ]
        
        result = extract_last_user_message(messages)
        
        assert result == ""

    def test_whitespace_content(self):
        messages = [
            ChatMessage(role="user", content="   \n  \n  "),
        ]
        
        result = extract_last_user_message(messages)
        
        assert result == "   \n  \n  "
