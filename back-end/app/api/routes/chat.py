from fastapi import APIRouter, Request

from app.api.deps import AuthenticatedUser, Chat
from app.domain.schemas import ChatRequest

router = APIRouter()


@router.post("/chat")
async def chat(body: ChatRequest, request: Request, user: AuthenticatedUser, service: Chat):
    return await service.start_chat(body, request, user)
