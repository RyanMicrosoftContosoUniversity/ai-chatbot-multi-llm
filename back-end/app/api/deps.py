from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from app.core.auth import TokenValidator, User
from app.core.errors import ApiError
from app.services.apim_client import GatewayClient
from app.services.chat_service import ChatService

if TYPE_CHECKING:
    from app.repositories.chat_repository import HistoryStore


@dataclass
class Services:
    validator: TokenValidator
    gateway: GatewayClient
    history: "HistoryStore | None" = None


async def current_user(request: Request) -> User:
    return await request.app.state.services.validator.validate(request.headers.get("authorization"))


AuthenticatedUser = Annotated[User, Depends(current_user)]


def history_store(request: Request) -> "HistoryStore":
    history = request.app.state.services.history
    if history is None:
        raise ApiError(503, "history_disabled", "Conversation history is not configured.")
    return history


def chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


Chat = Annotated[ChatService, Depends(chat_service)]
