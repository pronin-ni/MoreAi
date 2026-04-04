from app.schemas.openai import ChatMessage, MessageContentPart as SchemaMessageContentPart


def extract_last_user_message(messages: list[ChatMessage]) -> str:
    user_messages = [msg for msg in messages if msg.role == "user"]
    
    if not user_messages:
        raise ValueError("No user message found in messages array")
    
    last_user_message = user_messages[-1]
    return extract_text_content(last_user_message.content)


def extract_text_content(content: str | list[SchemaMessageContentPart]) -> str:
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, SchemaMessageContentPart):
                if part.type == "text" and part.text:
                    text_parts.append(part.text)
            elif isinstance(part, dict):
                if part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        text_parts.append(text)
        
        if text_parts:
            return "\n".join(text_parts)
    
    raise ValueError(f"Cannot extract text from content: {type(content)}")
