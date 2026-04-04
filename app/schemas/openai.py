from pydantic import BaseModel, Field
from typing import Optional, Literal


class MessageContentPart(BaseModel):
    type: Literal["text", "image_url", "input_audio"] = "text"
    text: Optional[str] = None
    image_url: Optional[dict[str, str]] = None
    input_audio: Optional[dict[str, str]] = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[MessageContentPart]
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = "internal-web-chat"
    messages: list[ChatMessage]
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=1.0, ge=0.0, le=1.0)
    n: Optional[int] = Field(default=1, ge=1)
    stream: bool = False
    stop: Optional[str | list[str]] = None
    max_tokens: Optional[int] = Field(default=2048, ge=1)
    presence_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)
    user: Optional[str] = None


class Message(BaseModel):
    role: Literal["assistant", "system", "user"]
    content: str


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: Optional[Literal["stop", "length", "content_filter", "tool_calls", "function_call"]] = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]


class ErrorResponse(BaseModel):
    message: str
    type: str
    param: Optional[str] = None
    code: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
