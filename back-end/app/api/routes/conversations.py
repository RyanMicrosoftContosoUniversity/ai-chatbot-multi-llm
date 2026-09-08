from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.deps import AuthenticatedUser, Chat, history_store
from app.domain.schemas import (
    ConversationChatRequest,
    CreateConversation,
    Feedback,
    RenameConversation,
)

router = APIRouter(prefix="/conversations")


@router.post("", status_code=201)
async def new_conversation(body: CreateConversation, request: Request, user: AuthenticatedUser):
    return await history_store(request).create_conversation(user.oid, body.title)


@router.get("")
async def conversations(
    request: Request,
    user: AuthenticatedUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    continuationToken: Annotated[str | None, Query(max_length=8192)] = None,
):
    return await history_store(request).list_conversations(user.oid, limit, continuationToken)


@router.patch("/{conversation_id}")
async def rename(
    conversation_id: UUID,
    body: RenameConversation,
    request: Request,
    user: AuthenticatedUser,
):
    return await history_store(request).rename_conversation(
        user.oid, str(conversation_id), body.title
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete(conversation_id: UUID, request: Request, user: AuthenticatedUser):
    await history_store(request).delete_conversation(user.oid, str(conversation_id))
    return Response(status_code=204)


@router.get("/{conversation_id}/messages")
async def messages(
    conversation_id: UUID,
    request: Request,
    user: AuthenticatedUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    continuationToken: Annotated[str | None, Query(max_length=8192)] = None,
):
    return await history_store(request).list_messages(
        user.oid, str(conversation_id), limit, continuationToken
    )


@router.post("/{conversation_id}/messages")
async def conversation_chat(
    conversation_id: UUID,
    body: ConversationChatRequest,
    request: Request,
    user: AuthenticatedUser,
    service: Chat,
):
    return await service.start_chat(body, request, user, str(conversation_id))


@router.put("/{conversation_id}/messages/{message_id}/feedback", status_code=204)
async def feedback(
    conversation_id: UUID,
    message_id: str,
    body: Feedback,
    request: Request,
    user: AuthenticatedUser,
):
    await history_store(request).feedback(user.oid, str(conversation_id), message_id, body.rating)
    return Response(status_code=204)
