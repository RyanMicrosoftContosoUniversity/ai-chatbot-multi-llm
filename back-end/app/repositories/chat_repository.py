"""Owner-scoped Cosmos history with partition-local fencing, not exactly-once inference."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID, uuid4

from azure.core import MatchConditions
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError

from app.core.config import Settings
from app.core.errors import ApiError

logger = logging.getLogger("multillm_bff.history")
T = TypeVar("T")
Document = dict[str, Any]
Operation = tuple[str, tuple[Any, ...], dict[str, Any]]
CONTROL_ID = "_control"
OWNER_ID = "_owner"
FINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


def _uuid(value: str, *, request: bool = False) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        if request:
            raise ApiError(400, "invalid_request", "clientMessageId must be a UUID.") from exc
        raise _not_found() from exc


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "Conversation or message not found.")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _body(document: Document) -> Document:
    return {key: value for key, value in document.items() if not key.startswith("_")}


def _replace_operation(document: Document, body: Document) -> Operation:
    return ("replace", (document["id"], body), {"if_match_etag": document["_etag"]})


def _public_conversation(document: Document) -> Document:
    return {key: document[key] for key in ("id", "title", "createdAt", "updatedAt")}


def _public_message(document: Document) -> Document:
    fields = (
        "id",
        "conversationId",
        "role",
        "content",
        "status",
        "createdAt",
        "model",
        "usage",
        "feedback",
        "metrics",
        "promptVersion",
    )
    return {key: document[key] for key in fields if key in document}


class HistoryStore:
    def __init__(self, database_client: DatabaseProxy, settings: Settings):
        self.settings = settings
        self.conversations = database_client.get_container_client(
            settings.cosmos_conversations_container
        )
        self.messages = database_client.get_container_client(settings.cosmos_messages_container)
        self._heartbeat_seconds = settings.heartbeat_seconds
        self._lease_seconds = max(30.0, self._heartbeat_seconds * 3)
        self._io_timeout = min(settings.connect_timeout_seconds, self._lease_seconds / 3)
        self._cleanup_seconds = 5.0

    async def _request(self, operation: Awaitable[T]) -> T:
        try:
            async with asyncio.timeout(self._io_timeout):
                return await operation
        except (CosmosHttpResponseError, CosmosBatchOperationError) as exc:
            if exc.status_code == 404:
                raise _not_found() from exc
            if exc.status_code in {409, 412}:
                raise ApiError(
                    409, "history_conflict", "History changed; retry the request."
                ) from exc
            raise ApiError(
                503, "history_unavailable", "History storage is temporarily unavailable.", 3
            ) from exc
        except (TimeoutError, ServiceRequestError, ServiceResponseError) as exc:
            raise ApiError(
                503, "history_unavailable", "History storage is temporarily unavailable.", 3
            ) from exc

    async def _read(self, container: Any, item_id: str, partition: Any) -> Document | None:
        try:
            document = await self._request(
                container.read_item(item=item_id, partition_key=partition)
            )
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        user = partition[0] if isinstance(partition, list) else partition
        if document.get("userId") != user or document.get("id") != item_id:
            raise _not_found()
        if isinstance(partition, list) and document.get("conversationId") != partition[1]:
            raise _not_found()
        return document

    async def _query(
        self, container: Any, query: str, parameters: list[Document], partition: Any, take: int
    ) -> list[Document]:
        async def collect() -> list[Document]:
            return [
                item
                async for item in container.query_items(
                    query=query, parameters=parameters, partition_key=partition, max_item_count=take
                )
            ]

        documents = await self._request(collect())
        user = partition[0] if isinstance(partition, list) else partition
        if any(
            item.get("userId") != user
            or (isinstance(partition, list) and item.get("conversationId") != partition[1])
            for item in documents
        ):
            raise _not_found()
        return documents

    async def _conversation(
        self, user: str, conversation_id: str, *, include_deleted: bool = False
    ) -> Document:
        document = await self._read(self.conversations, conversation_id, user)
        if (
            document is None
            or document.get("docType") != "conversation"
            or (not include_deleted and document.get("state") != "active")
        ):
            raise _not_found()
        return document

    async def create_conversation(self, user_id: str, title: str) -> Document:
        user = _uuid(user_id)
        created = _timestamp()
        conversation_id = str(uuid4())
        document = {
            "id": conversation_id,
            "userId": user,
            "docType": "conversation",
            "state": "active",
            "title": title,
            "createdAt": created,
            "updatedAt": created,
            "sortKey": f"{created}:{conversation_id}",
        }
        await self._request(self.conversations.create_item(body=document))
        return _public_conversation(document)

    async def _cursor_key(self, user: str) -> bytes:
        document = await self._read(self.conversations, OWNER_ID, user)
        if document is None:
            document = {
                "id": OWNER_ID,
                "userId": user,
                "docType": "owner",
                "cursorKey": secrets.token_hex(32),
            }
            try:
                await self._request(self.conversations.create_item(body=document))
            except ApiError as exc:
                if exc.code != "history_conflict":
                    raise
                document = await self._read(self.conversations, OWNER_ID, user)
                if document is None:
                    raise ApiError(
                        503, "history_unavailable", "History cursor is unavailable."
                    ) from exc
        return bytes.fromhex(document["cursorKey"])

    async def _cursor(
        self, user: str, kind: str, conversation_id: str | None, token: str | None
    ) -> tuple[bytes, str | int | None]:
        key = await self._cursor_key(user)
        if token is None:
            return key, None
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError("Invalid cursor length")
            payload, signature = token.split(".")
            expected = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid signature")
            decoded = json.loads(
                base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
            )
            if (
                not isinstance(decoded, dict)
                or set(decoded) != {"v", "user", "kind", "conversation", "after"}
                or decoded["v"] != 1
                or decoded["user"] != user
                or decoded["kind"] != kind
                or decoded["conversation"] != conversation_id
            ):
                raise ValueError("Invalid cursor scope")
            after = decoded["after"]
            if kind == "messages":
                if type(after) is not int or after < 1:
                    raise ValueError("Invalid sequence")
            elif not isinstance(after, str) or not 1 <= len(after) <= 100:
                raise ValueError("Invalid sort key")
            return key, after
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ApiError(400, "invalid_continuation", "Invalid continuation token.") from exc

    @staticmethod
    def _encode_cursor(
        key: bytes, user: str, kind: str, conversation_id: str | None, after: str | int
    ) -> str:
        raw = json.dumps(
            {"v": 1, "user": user, "kind": kind, "conversation": conversation_id, "after": after},
            separators=(",", ":"),
        ).encode()
        payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        return f"{payload}.{hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()}"

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApiError(400, "invalid_request", "limit must be between 1 and 100.")

    async def list_conversations(
        self, user_id: str, limit: int, continuation: str | None
    ) -> Document:
        self._limit(limit)
        user = _uuid(user_id)
        key, after = await self._cursor(user, "conversations", None, continuation)
        query = (
            "SELECT TOP @take * FROM c WHERE c.userId = @user "
            "AND c.docType = 'conversation' AND c.state = 'active'"
        )
        parameters = [{"name": "@user", "value": user}, {"name": "@take", "value": limit + 1}]
        if after is not None:
            query += " AND c.sortKey < @after"
            parameters.append({"name": "@after", "value": after})
        rows = await self._query(
            self.conversations, query + " ORDER BY c.sortKey DESC", parameters, user, limit + 1
        )
        token = None
        if len(rows) > limit:
            token = self._encode_cursor(
                key, user, "conversations", None, rows[limit - 1]["sortKey"]
            )
        return {
            "items": [_public_conversation(row) for row in rows[:limit]],
            "continuationToken": token,
        }

    async def _edit_conversation(
        self, user: str, conversation_id: str, changes: Document, *, deleting: bool = False
    ) -> Document:
        for _ in range(5):
            current = await self._conversation(user, conversation_id, include_deleted=deleting)
            body = _body(current)
            body.update(changes)
            body["updatedAt"] = _timestamp()
            body["sortKey"] = f"{body['updatedAt']}:{conversation_id}"
            try:
                await self._request(
                    self.conversations.replace_item(
                        item=conversation_id,
                        body=body,
                        etag=current["_etag"],
                        match_condition=MatchConditions.IfNotModified,
                    )
                )
                return body
            except ApiError as exc:
                if exc.code != "history_conflict":
                    raise
        raise ApiError(409, "history_conflict", "Conversation changed; retry the request.")

    async def rename_conversation(self, user_id: str, conversation_id: str, title: str) -> Document:
        user, conversation_id = _uuid(user_id), _uuid(conversation_id)
        return _public_conversation(
            await self._edit_conversation(
                user,
                conversation_id,
                {
                    "title": title,
                },
            )
        )

    async def _control(self, user: str, conversation_id: str) -> Document:
        partition = [user, conversation_id]
        current = await self._read(self.messages, CONTROL_ID, partition)
        if current is not None:
            return current
        body = {
            "id": CONTROL_ID,
            "docType": "control",
            "userId": user,
            "conversationId": conversation_id,
            "state": "active",
            "owner": None,
            "expiresAt": 0,
            "sequence": 0,
            "activeTurn": None,
        }
        try:
            return await self._request(self.messages.create_item(body=body))
        except ApiError as exc:
            if exc.code != "history_conflict":
                raise
            current = await self._read(self.messages, CONTROL_ID, partition)
            if current is None:
                raise ApiError(
                    503, "history_unavailable", "Conversation lease is unavailable."
                ) from exc
            return current

    async def _acquire(
        self, user: str, conversation_id: str, *, deleting: bool = False
    ) -> "_Lease":
        for _ in range(5):
            current = await self._control(user, conversation_id)
            if current["state"] == "deleted" or (current["state"] != "active" and not deleting):
                raise _not_found()
            if current.get("owner") and current["expiresAt"] > time.time():
                raise ApiError(409, "conversation_busy", "Conversation has an active operation.", 3)
            body = _body(current)
            body.update(owner=str(uuid4()), expiresAt=time.time() + self._lease_seconds)
            if deleting:
                body["state"] = "deleting"
            try:
                await self._request(
                    self.messages.replace_item(
                        item=CONTROL_ID,
                        body=body,
                        etag=current["_etag"],
                        match_condition=MatchConditions.IfNotModified,
                    )
                )
                return _Lease(self, body)
            except ApiError as exc:
                if exc.code != "history_conflict":
                    raise
        raise ApiError(409, "conversation_busy", "Conversation has an active operation.", 3)

    async def _message_rows(
        self,
        user: str,
        conversation_id: str,
        take: int,
        after: int = 0,
        *,
        completed: bool = False,
    ) -> list[Document]:
        query = (
            "SELECT TOP @take * FROM c WHERE c.userId = @user "
            "AND c.conversationId = @conversation AND c.docType = 'message' "
            "AND c.sequence > @after"
        )
        if completed:
            query += " AND c.status = 'completed'"
        return await self._query(
            self.messages,
            query + " ORDER BY c.sequence ASC",
            [
                {"name": "@user", "value": user},
                {"name": "@conversation", "value": conversation_id},
                {"name": "@take", "value": take},
                {"name": "@after", "value": after},
            ],
            [user, conversation_id],
            take,
        )

    async def _recover(self, lease: "_Lease") -> None:
        async def build(control: Document) -> list[Operation]:
            active = control.get("activeTurn")
            if active is None:
                return []
            operations = []
            for item_id in (active["clientMessageId"], active["messageId"]):
                document = await self._read(self.messages, item_id, lease.partition)
                if document is None or document.get("status") != "pending":
                    raise ApiError(
                        503, "history_inconsistent", "Interrupted history needs recovery."
                    )
                body = _body(document)
                body.update(status="interrupted", finishedAt=_timestamp())
                operations.append(_replace_operation(document, body))
            control["activeTurn"] = None
            return operations

        await lease.mutate(build)

    async def _recover_expired(self, user: str, conversation_id: str) -> None:
        control = await self._read(self.messages, CONTROL_ID, [user, conversation_id])
        if control is None or control.get("activeTurn") is None:
            return
        if control.get("owner") and control["expiresAt"] > time.time():
            return
        try:
            lease = await self._acquire(user, conversation_id)
        except ApiError as exc:
            if exc.code == "conversation_busy":
                return
            raise
        lease.start()
        try:
            await self._recover(lease)
        finally:
            await self._close_bounded(lease, None, "interrupted", cancelled=False)

    async def list_messages(
        self, user_id: str, conversation_id: str, limit: int, continuation: str | None
    ) -> Document:
        self._limit(limit)
        user, conversation_id = _uuid(user_id), _uuid(conversation_id)
        await self._conversation(user, conversation_id)
        key, after = await self._cursor(user, "messages", conversation_id, continuation)
        await self._recover_expired(user, conversation_id)
        rows = await self._message_rows(user, conversation_id, limit + 1, after or 0)
        token = None
        if len(rows) > limit:
            token = self._encode_cursor(
                key, user, "messages", conversation_id, rows[limit - 1]["sequence"]
            )
        return {
            "items": [_public_message(row) for row in rows[:limit]],
            "continuationToken": token,
        }

    async def _context(self, user: str, conversation_id: str, message: str) -> list[dict[str, str]]:
        size = len(message)
        context: list[dict[str, str]] = []
        after = 0
        pending_user = None
        while True:
            if size > self.settings.max_context_chars:
                raise ApiError(
                    400, "context_limit", "Conversation exceeds the context limit; start a new one."
                )
            rows = await self._message_rows(user, conversation_id, 100, after, completed=True)
            for row in rows:
                if row["role"] == "user":
                    pending_user = row
                elif (
                    row["role"] == "assistant"
                    and pending_user is not None
                    and row["turnId"] == pending_user["turnId"]
                    and row["sequence"] == pending_user["sequence"] + 1
                ):
                    size += len(pending_user["content"]) + len(row["content"])
                    if size > self.settings.max_context_chars:
                        raise ApiError(
                            400,
                            "context_limit",
                            "Conversation exceeds the context limit; start a new one.",
                        )
                    context.extend(
                        [
                            {"role": "user", "content": pending_user["content"]},
                            {"role": "assistant", "content": row["content"]},
                        ]
                    )
                    pending_user = None
            if len(rows) < 100:
                return [*context, {"role": "user", "content": message}]
            after = rows[-1]["sequence"]

    @asynccontextmanager
    async def open_turn(
        self,
        user_id: str,
        conversation_id: str,
        client_message_id: str,
        model: str,
        message: str,
        prompt_version: str,
    ) -> AsyncIterator["Turn"]:
        user, conversation_id = _uuid(user_id), _uuid(conversation_id)
        client_message_id = _uuid(client_message_id, request=True)
        await self._conversation(user, conversation_id)
        if await self._read(self.messages, client_message_id, [user, conversation_id]) is not None:
            raise ApiError(409, "duplicate_message", "clientMessageId has already been submitted.")
        lease = await self._acquire(user, conversation_id)
        turn = None
        cancelled = False
        lease.start()
        try:
            await self._recover(lease)
            messages = await self._context(user, conversation_id, message)
            turn = Turn(lease, client_message_id, model, message, prompt_version, messages)
            lease.turn = turn
            await turn._reserve()
            yield turn
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            await self._close_bounded(
                lease, turn, "cancelled" if cancelled else "interrupted", cancelled=cancelled
            )

    async def _close_bounded(
        self, lease: "_Lease", turn: "Turn | None", status: str, *, cancelled: bool
    ) -> None:
        async def close() -> None:
            try:
                async with asyncio.timeout(self._cleanup_seconds):
                    await lease.stop()
                    if turn is not None and turn.reserved and not turn.finalized:
                        await turn.finish(status, turn.content, {}, {})
                    await lease.release()
            except (TimeoutError, ApiError) as exc:
                if cancelled or (turn is not None and turn.finalized):
                    logger.warning(
                        "History cleanup incomplete; lease expiry permits recovery: %s", exc
                    )
                elif isinstance(exc, TimeoutError):
                    raise ApiError(
                        503,
                        "history_unavailable",
                        "History cleanup timed out; recovery is pending.",
                    ) from exc
                else:
                    raise
            finally:
                lease.closed = True
                if lease.task is not None and not lease.task.done():
                    lease.task.cancel()
                    try:
                        await lease.task
                    except asyncio.CancelledError:
                        pass

        cleanup = asyncio.create_task(close(), name="history-cleanup")
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # AsyncExitStack may close in a task that is itself being cancelled.
            # Finish the independently bounded cleanup without orphaning its heartbeat.
            try:
                await asyncio.shield(cleanup)
            finally:
                if not cleanup.done():
                    cleanup.cancel()
                    try:
                        await cleanup
                    except asyncio.CancelledError:
                        pass
            raise

    async def feedback(
        self, user_id: str, conversation_id: str, message_id: str, rating: str | None
    ) -> None:
        if rating not in {"up", "down", None}:
            raise ApiError(400, "invalid_request", "rating must be up, down, or null.")
        user, conversation_id, message_id = (
            _uuid(user_id),
            _uuid(conversation_id),
            _uuid(message_id),
        )
        await self._conversation(user, conversation_id)
        partition = [user, conversation_id]
        for _ in range(5):
            control = await self._control(user, conversation_id)
            if control["state"] != "active":
                raise _not_found()
            document = await self._read(self.messages, message_id, partition)
            if document is None or document.get("role") != "assistant":
                raise _not_found()
            body = _body(document)
            body["feedback"] = rating
            try:
                await self._request(
                    self.messages.execute_item_batch(
                        batch_operations=[
                            _replace_operation(control, _body(control)),
                            _replace_operation(document, body),
                        ],
                        partition_key=partition,
                    )
                )
                return
            except ApiError as exc:
                if exc.code != "history_conflict":
                    raise
        raise ApiError(409, "history_conflict", "Message changed; retry feedback.")

    async def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        user, conversation_id = _uuid(user_id), _uuid(conversation_id)
        conversation = await self._conversation(user, conversation_id, include_deleted=True)
        if conversation["state"] == "deleted":
            return
        control = await self._control(user, conversation_id)
        if control["state"] == "deleted":
            await self._edit_conversation(
                user, conversation_id, {"state": "deleted", "title": ""}, deleting=True
            )
            return
        lease = await self._acquire(user, conversation_id, deleting=True)
        lease.start()
        try:
            await self._edit_conversation(
                user, conversation_id, {"state": "deleting", "title": ""}, deleting=True
            )
            while True:
                count = 0

                async def remove(control: Document) -> list[Operation]:
                    nonlocal count
                    rows = await self._message_rows(user, conversation_id, 99)
                    count = len(rows)
                    return [
                        ("delete", (row["id"],), {"if_match_etag": row["_etag"]}) for row in rows
                    ]

                await lease.mutate(remove)
                if count == 0:
                    break

            async def tombstone(control: Document) -> list[Operation]:
                control.update(
                    state="deleted", owner=None, expiresAt=0, activeTurn=None, sequence=0
                )
                return []

            await lease.mutate(tombstone)
            await self._edit_conversation(
                user, conversation_id, {"state": "deleted", "title": ""}, deleting=True
            )
        finally:
            await self._close_bounded(lease, None, "interrupted", cancelled=False)


class _Lease:
    def __init__(self, store: HistoryStore, control: Document):
        self.store = store
        self.partition = [control["userId"], control["conversationId"]]
        self.owner = control["owner"]
        self.state = control["state"]
        self.expires_at = control["expiresAt"]
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.turn: Turn | None = None
        self.lost: ApiError | None = None
        self.closed = False
        self.stopping = False

    def assert_owned(self) -> None:
        if self.lost is not None:
            raise self.lost
        if self.closed or time.time() >= self.expires_at:
            raise ApiError(409, "lease_lost", "Conversation lease was lost; generation must stop.")
        if self.task is not None and self.task.cancelled() and not self.stopping:
            self.lost = ApiError(409, "lease_lost", "Conversation heartbeat was cancelled.")
            raise self.lost
        if self.task is not None and self.task.done() and not self.task.cancelled():
            error = self.task.exception()
            if error is not None:
                raise ApiError(
                    503, "history_unavailable", "Conversation heartbeat failed."
                ) from error

    def start(self) -> None:
        self.task = asyncio.create_task(self._heartbeat(), name="history-lease-heartbeat")

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.store._heartbeat_seconds)

                async def checkpoint(control: Document) -> list[Operation]:
                    turn = self.turn
                    if turn is None or not turn.reserved or turn.finalized:
                        return []
                    document = await self.store._read(
                        self.store.messages, turn.message_id, self.partition
                    )
                    if document is None or document["status"] != "pending":
                        raise ApiError(409, "lease_lost", "Pending generation is no longer owned.")
                    if document["content"] == turn.content:
                        return []
                    body = _body(document)
                    body["content"] = turn.content
                    return [_replace_operation(document, body)]

                await self.mutate(checkpoint)
        except ApiError as exc:
            self.lost = exc
            logger.warning("Conversation heartbeat stopped: %s", exc.code)

    async def mutate(self, build: Callable[[Document], Awaitable[list[Operation]]]) -> None:
        async with self.lock:
            for _ in range(5):
                self.assert_owned()
                current = await self.store._read(self.store.messages, CONTROL_ID, self.partition)
                if (
                    current is None
                    or current.get("owner") != self.owner
                    or current["state"] != self.state
                    or current["expiresAt"] <= time.time()
                ):
                    raise ApiError(409, "lease_lost", "Conversation lease is no longer owned.")
                control = _body(current)
                control["expiresAt"] = time.time() + self.store._lease_seconds
                operations = await build(control)
                self.assert_owned()
                try:
                    # All inference-related writes include the same-partition control ETag.
                    # A delayed old worker cannot commit after another instance acquires it.
                    await self.store._request(
                        self.store.messages.execute_item_batch(
                            batch_operations=[_replace_operation(current, control), *operations],
                            partition_key=self.partition,
                        )
                    )
                    self.expires_at = control["expiresAt"]
                    return
                except ApiError as exc:
                    if exc.code != "history_conflict":
                        raise
            raise ApiError(
                409, "history_conflict", "History changed repeatedly; retry the request."
            )

    async def stop(self) -> None:
        self.stopping = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def release(self) -> None:
        async with self.lock:
            for _ in range(5):
                current = await self.store._read(self.store.messages, CONTROL_ID, self.partition)
                if current is None or current.get("owner") != self.owner:
                    return
                # Preserve an ambiguous pending turn until expiry, rather than enabling replay.
                if current.get("activeTurn") is not None and current["state"] == "active":
                    return
                body = _body(current)
                body.update(owner=None, expiresAt=0)
                try:
                    await self.store._request(
                        self.store.messages.replace_item(
                            item=CONTROL_ID,
                            body=body,
                            etag=current["_etag"],
                            match_condition=MatchConditions.IfNotModified,
                        )
                    )
                    return
                except ApiError as exc:
                    if exc.code != "history_conflict":
                        raise
            raise ApiError(409, "history_conflict", "Lease release conflicted; it will expire.")


class Turn:
    def __init__(
        self,
        lease: _Lease,
        client_message_id: str,
        model: str,
        message: str,
        prompt_version: str,
        messages: list[dict[str, str]],
    ):
        self._lease = lease
        self.client_message_id = client_message_id
        self.message_id = str(uuid4())
        self.messages = messages
        self.model = model
        self.message = message
        self.prompt_version = prompt_version
        self.content = ""
        self.reserved = False
        self.finalized = False
        self._final_status: str | None = None
        self._metadata_updated = False
        self._finishing = asyncio.Lock()

    def assert_lease(self) -> None:
        self._lease.assert_owned()

    async def _reserve(self) -> None:
        lease = self._lease

        async def build(control: Document) -> list[Operation]:
            if (
                await lease.store._read(
                    lease.store.messages, self.client_message_id, lease.partition
                )
                is not None
            ):
                raise ApiError(
                    409, "duplicate_message", "clientMessageId has already been submitted."
                )
            if control.get("activeTurn") is not None:
                raise ApiError(409, "conversation_busy", "Conversation recovery is pending.")
            sequence = control["sequence"]
            control["sequence"] = sequence + 2
            control["activeTurn"] = {
                "clientMessageId": self.client_message_id,
                "messageId": self.message_id,
            }
            created = _timestamp()
            common = {
                "docType": "message",
                "userId": lease.partition[0],
                "conversationId": lease.partition[1],
                "turnId": self.client_message_id,
                "status": "pending",
                "createdAt": created,
                "model": self.model,
                "promptVersion": self.prompt_version,
            }
            return [
                (
                    "create",
                    (
                        {
                            **common,
                            "id": self.client_message_id,
                            "role": "user",
                            "content": self.message,
                            "sequence": sequence + 1,
                        },
                    ),
                    {},
                ),
                (
                    "create",
                    (
                        {
                            **common,
                            "id": self.message_id,
                            "role": "assistant",
                            "content": "",
                            "sequence": sequence + 2,
                            "feedback": None,
                        },
                    ),
                    {},
                ),
            ]

        await lease.mutate(build)
        self.reserved = True

    async def finish(
        self, status: str, content: str, usage: dict[str, object], metrics: dict[str, object]
    ) -> None:
        if status not in FINAL_STATUSES:
            raise ApiError(400, "invalid_request", "Invalid final message status.")
        async with self._finishing:
            if self.finalized:
                if status != self._final_status or content != self.content:
                    raise ApiError(409, "history_conflict", "Turn is already finalized.")
                if not self._metadata_updated:
                    await self._touch_conversation()
                return
            if not self.reserved:
                raise ApiError(409, "history_conflict", "Turn has not been reserved.")
            self.content = content
            lease = self._lease

            async def build(control: Document) -> list[Operation]:
                active = control.get("activeTurn")
                if active is None or active["messageId"] != self.message_id:
                    raise ApiError(409, "lease_lost", "Turn is no longer owned.")
                operations = []
                for item_id in (self.client_message_id, self.message_id):
                    document = await lease.store._read(
                        lease.store.messages, item_id, lease.partition
                    )
                    if document is None or document["status"] != "pending":
                        raise ApiError(409, "history_conflict", "Pending message changed.")
                    body = _body(document)
                    body.update(status=status, finishedAt=_timestamp())
                    if item_id == self.message_id:
                        body.update(content=content, usage=usage, metrics=metrics)
                    operations.append(_replace_operation(document, body))
                control["activeTurn"] = None
                return operations

            await lease.mutate(build)
            self.finalized = True
            self._final_status = status
            await self._touch_conversation()

    async def _touch_conversation(self) -> None:
        lease = self._lease
        await lease.store._edit_conversation(lease.partition[0], lease.partition[1], {})
        self._metadata_updated = True
