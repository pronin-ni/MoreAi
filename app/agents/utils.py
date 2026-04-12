"""
Shared utilities for agent providers.
"""


def extract_response_text(response: dict) -> str:
    """Extract assistant response text from agent server message response.

    Parses the `parts` array from the response, extracting text parts
    and skipping tool/tool-use/tool-result parts.
    """
    parts = response.get("parts", [])
    if not parts:
        # Fallback: check for content in message info
        info = response.get("info", {})
        return info.get("content", "")

    text_parts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if part_type == "text":
            content = part.get("text", "")
            if content:
                text_parts.append(content)
        elif part_type in ("tool", "tool-use", "tool-result"):
            # Skip tool parts
            continue

    return "\n".join(text_parts).strip()
