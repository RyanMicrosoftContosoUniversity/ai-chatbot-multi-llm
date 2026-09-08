from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChatRequest(RequestModel):
    model: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=100000)
    maxTokens: int | None = Field(default=None, ge=1, le=16384)


class ConversationChatRequest(ChatRequest):
    clientMessageId: UUID


class CreateConversation(RequestModel):
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class RenameConversation(RequestModel):
    title: str = Field(min_length=1, max_length=160)


class Feedback(RequestModel):
    rating: Literal["up", "down"] | None
