import asyncio
import copy
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError

from app.core.errors import ApiError
from app.repositories.chat_repository import CONTROL_ID, HistoryStore, Turn

USER = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"


def cosmos_error(status):
    return CosmosHttpResponseError(status_code=status, message="fake Cosmos failure")


class FakeContainer:
    """In-memory Cosmos partitions, atomic batches, and mandatory conditional writes."""

    def __init__(self, hierarchical):
        self.hierarchical = hierarchical
        self.documents = {}
        self.version = 0
        self.calls = []
        self.failures = {}
        self.before_batch = None
        self.after_batch_error = None
        self.batch_sizes = []

    def partition(self, body):
        if self.hierarchical:
            return (body["userId"], body["conversationId"])
        return (body["userId"],)

    @staticmethod
    def key(partition, item_id):
        return (*partition, item_id)

    def fail(self, method):
        failures = self.failures.get(method, [])
        if failures:
            error = failures.pop(0)
            if error is not None:
                raise error

    def store(self, body, documents=None):
        self.version += 1
        value = copy.deepcopy(body)
        value["_etag"] = str(self.version)
        target = self.documents if documents is None else documents
        target[self.key(self.partition(body), body["id"])] = value
        return copy.deepcopy(value)

    async def create_item(self, *, body):
        await asyncio.sleep(0)
        self.fail("create")
        self.calls.append(("create", self.partition(body), body["id"]))
        if self.key(self.partition(body), body["id"]) in self.documents:
            raise cosmos_error(409)
        return self.store(body)

    async def read_item(self, *, item, partition_key):
        await asyncio.sleep(0)
        self.fail("read")
        partition = tuple(partition_key) if isinstance(partition_key, list) else (partition_key,)
        assert len(partition) == (2 if self.hierarchical else 1)
        self.calls.append(("read", partition, item))
        key = self.key(partition, item)
        if key not in self.documents:
            raise cosmos_error(404)
        return copy.deepcopy(self.documents[key])

    async def replace_item(self, *, item, body, etag, match_condition):
        await asyncio.sleep(0)
        self.fail("replace")
        assert match_condition == MatchConditions.IfNotModified
        key = self.key(self.partition(body), item)
        self.calls.append(("replace", self.partition(body), item))
        current = self.documents.get(key)
        if current is None:
            raise cosmos_error(404)
        if current["_etag"] != etag:
            raise cosmos_error(412)
        return self.store(body)

    async def execute_item_batch(self, *, batch_operations, partition_key):
        await asyncio.sleep(0)
        if self.before_batch is not None:
            await self.before_batch(batch_operations)
        self.fail("batch")
        partition = tuple(partition_key)
        assert self.hierarchical and len(partition) == 2
        assert 1 <= len(batch_operations) <= 100
        self.batch_sizes.append(len(batch_operations))
        self.calls.append(("batch", partition, None))
        staged = copy.deepcopy(self.documents)
        results = []
        for index, (kind, args, kwargs) in enumerate(batch_operations):
            if kind == "create":
                body = args[0]
                assert self.partition(body) == partition
                key = self.key(partition, body["id"])
                status = 409 if key in staged else None
            else:
                item_id = args[0]
                key = self.key(partition, item_id)
                current = staged.get(key)
                status = 404 if current is None else None
                if current is not None and current["_etag"] != kwargs["if_match_etag"]:
                    status = 412
                if kind == "replace":
                    body = args[1]
                    assert self.partition(body) == partition
            if status is not None:
                raise CosmosBatchOperationError(
                    error_index=index,
                    headers={},
                    status_code=status,
                    message="fake atomic batch conflict",
                    operation_responses=[],
                )
            if kind == "delete":
                del staged[key]
                results.append({"statusCode": 204})
            else:
                saved = self.store(body, staged)
                results.append({"statusCode": 200, "resourceBody": saved, "eTag": saved["_etag"]})
        self.documents = staged
        if self.after_batch_error is not None:
            error, self.after_batch_error = self.after_batch_error, None
            raise error
        return results

    def query_items(self, *, query, parameters, partition_key, max_item_count):
        assert "OFFSET" not in query.upper()
        assert "SELECT TOP @take" in query
        values = {value["name"]: value["value"] for value in parameters}
        assert values["@take"] == max_item_count
        assert "c.userId = @user" in query
        partition = tuple(partition_key) if isinstance(partition_key, list) else (partition_key,)
        assert values["@user"] == partition[0]
        assert len(partition) == (2 if self.hierarchical else 1)
        if self.hierarchical:
            assert "c.conversationId = @conversation" in query
            assert values["@conversation"] == partition[1]
        self.calls.append(("query", partition, query))

        async def generate():
            await asyncio.sleep(0)
            self.fail("query")
            rows = [
                copy.deepcopy(row)
                for row in self.documents.values()
                if self.partition(row) == partition
            ]
            if self.hierarchical:
                rows = [row for row in rows if row["docType"] == "message"]
                rows = [row for row in rows if row["sequence"] > values["@after"]]
                if "c.status = 'completed'" in query:
                    rows = [row for row in rows if row["status"] == "completed"]
                rows.sort(key=lambda row: row["sequence"])
            else:
                rows = [
                    row
                    for row in rows
                    if row["docType"] == "conversation" and row["state"] == "active"
                ]
                if "@after" in values:
                    rows = [row for row in rows if row["sortKey"] < values["@after"]]
                rows.sort(key=lambda row: row["sortKey"], reverse=True)
            for row in rows[:max_item_count]:
                yield row

        return generate()


class FakeDatabase:
    def __init__(self, settings):
        self.containers = {
            settings.cosmos_conversations_container: FakeContainer(False),
            settings.cosmos_messages_container: FakeContainer(True),
        }

    def get_container_client(self, name):
        return self.containers[name]


@pytest.fixture
def store(settings):
    return HistoryStore(FakeDatabase(settings), settings)


async def conversation(store, user=USER, title="A conversation"):
    return (await store.create_conversation(user, title))["id"]


def control(store, conversation_id, user=USER):
    return store.messages.documents[(user, conversation_id, CONTROL_ID)]


def documents(store, conversation_id, user=USER):
    return sorted(
        [
            row
            for row in store.messages.documents.values()
            if row["userId"] == user
            and row["conversationId"] == conversation_id
            and row["docType"] == "message"
        ],
        key=lambda row: row["sequence"],
    )


async def completed_turn(store, conversation_id, *, client_id=None, message="hello", answer="hi"):
    client_id = client_id or str(uuid4())
    async with store.open_turn(USER, conversation_id, client_id, "luna", message, "v1") as turn:
        await turn.finish(
            "completed", answer, {"promptTokens": 3, "completionTokens": 2}, {"durationMs": 10}
        )
    return turn


async def assert_error(status, operation, code=None):
    with pytest.raises(ApiError) as error:
        await operation
    assert error.value.status == status
    if code is not None:
        assert error.value.code == code


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def other_instance(store):
    class SharedDatabase:
        def get_container_client(self, name):
            if name == store.settings.cosmos_conversations_container:
                return store.conversations
            assert name == store.settings.cosmos_messages_container
            return store.messages

    return HistoryStore(SharedDatabase(), store.settings)


async def test_crud_public_shapes_retention_and_owner_isolation(store):
    first = await store.create_conversation(USER, "Private")
    assert set(first) == {"id", "title", "createdAt", "updatedAt"}
    renamed = await store.rename_conversation(USER, first["id"], "Renamed")
    assert renamed["title"] == "Renamed"
    assert renamed["updatedAt"] >= first["updatedAt"]
    other = await conversation(store, OTHER)
    assert [item["id"] for item in (await store.list_conversations(USER, 20, None))["items"]] == [
        first["id"]
    ]
    await assert_error(404, store.rename_conversation(OTHER, first["id"], "stolen"))
    await assert_error(404, store.list_messages(OTHER, first["id"], 20, None))
    await assert_error(404, store.delete_conversation(OTHER, first["id"]))
    async with AsyncExitStack() as stack:
        await assert_error(
            404,
            stack.enter_async_context(
                store.open_turn(OTHER, first["id"], str(uuid4()), "luna", "stolen", "v1")
            ),
        )
    for row in store.conversations.documents.values():
        assert "ttl" not in row
    await store.delete_conversation(USER, first["id"])
    await store.delete_conversation(USER, first["id"])
    assert (await store.list_conversations(USER, 20, None))["items"] == []
    assert (await store.list_conversations(OTHER, 20, None))["items"][0]["id"] == other


@pytest.mark.parametrize("invalid", ["../other", "not-a-uuid", "", "null", "a" * 200])
async def test_invalid_resource_ids_are_uniform_404(store, invalid):
    await assert_error(404, store.rename_conversation(USER, invalid, "title"))
    await assert_error(404, store.delete_conversation(USER, invalid))
    await assert_error(404, store.list_messages(USER, invalid, 10, None))
    await assert_error(404, store.feedback(USER, invalid, str(uuid4()), "up"))
    assert store.messages.calls == []


async def test_missing_and_wrong_partition_resources_are_uniform_404(store):
    missing = str(uuid4())
    await assert_error(404, store.rename_conversation(USER, missing, "title"))
    await assert_error(404, store.delete_conversation(USER, missing))
    await assert_error(404, store.list_messages(USER, missing, 10, None))
    cid = await conversation(store)
    await assert_error(404, store.feedback(USER, cid, missing, "up"))
    await assert_error(404, store.feedback(USER, cid, "invalid", "up"))
    await assert_error(
        400, store.open_turn(USER, cid, "invalid", "luna", "text", "v1").__aenter__()
    )


async def test_conversation_keyset_pages_are_signed_owner_bound_and_shared(store):
    ids = [await conversation(store, title=str(index)) for index in range(7)]
    reader = other_instance(store)
    token = None
    found = []
    first_token = None
    while True:
        page = await reader.list_conversations(USER, 2, token)
        found.extend(item["id"] for item in page["items"])
        token = page["continuationToken"]
        first_token = first_token or token
        if token is None:
            break
    assert found == list(reversed(ids))
    assert len(found) == len(set(found))
    await assert_error(400, store.list_conversations(OTHER, 2, first_token), "invalid_continuation")
    await assert_error(
        400, store.list_conversations(USER, 2, first_token + "x"), "invalid_continuation"
    )
    await assert_error(400, store.list_conversations(USER, 2, "invalid"), "invalid_continuation")
    for limit in (0, 101, -1, True):
        await assert_error(400, store.list_conversations(USER, limit, None))


async def test_messages_page_in_pair_order_and_cursor_cannot_cross_conversations(store):
    cid = await conversation(store)
    turns = [await completed_turn(store, cid, message=str(i)) for i in range(4)]
    token = None
    found = []
    while True:
        page = await store.list_messages(USER, cid, 3, token)
        found.extend(page["items"])
        token = page["continuationToken"]
        if token is None:
            break
        other_cid = await conversation(store)
        await assert_error(
            400, store.list_messages(USER, other_cid, 3, token), "invalid_continuation"
        )
        await assert_error(400, store.list_conversations(USER, 3, token), "invalid_continuation")
    assert [row["id"] for row in found] == [
        item for turn in turns for item in (turn.client_message_id, turn.message_id)
    ]
    assert [row["role"] for row in found] == ["user", "assistant"] * 4
    assert found[1]["usage"] == {"promptTokens": 3, "completionTokens": 2}
    assert found[1]["metrics"] == {"durationMs": 10}
    assert found[1]["promptVersion"] == "v1"
    assert all(
        "userId" not in row and "_etag" not in row and "sequence" not in row for row in found
    )


async def test_context_contains_only_completed_pairs_and_current_user_message(store):
    cid = await conversation(store)
    await completed_turn(store, cid, message="first", answer="answer")
    for status in ("failed", "cancelled", "interrupted"):
        async with store.open_turn(USER, cid, str(uuid4()), "luna", status, "v1") as turn:
            await turn.finish(status, "partial", {}, {})
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "current", "v2") as turn:
        assert turn.messages == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "current"},
        ]
        assert all(item["role"] != "system" for item in turn.messages)
        await turn.finish("completed", "new answer", {}, {})


async def test_context_limit_rejects_instead_of_truncating_or_reserving(store):
    cid = await conversation(store)
    await completed_turn(store, cid, message="1234", answer="5678")
    store.settings.max_context_chars = 10
    requested = str(uuid4())
    calls = 0
    with pytest.raises(ApiError, match="context limit") as error:
        async with store.open_turn(USER, cid, requested, "luna", "abc", "v1"):
            calls += 1
    assert error.value.code == "context_limit"
    assert calls == 0
    assert requested not in {item["id"] for item in documents(store, cid)}
    assert control(store, cid)["owner"] is None
    async with store.open_turn(USER, cid, requested, "luna", "ab", "v1") as turn:
        assert sum(len(item["content"]) for item in turn.messages) == 10
        await turn.finish("completed", "ok", {}, {})


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
async def test_durable_duplicate_never_yields_a_second_model_call(store, status):
    cid = await conversation(store)
    requested = str(uuid4())
    calls = 0
    async with store.open_turn(USER, cid, requested, "luna", "hello", "v1") as turn:
        calls += 1
        await turn.finish(status, "answer", {}, {})
    with pytest.raises(ApiError) as error:
        async with other_instance(store).open_turn(USER, cid, requested, "luna", "hello", "v1"):
            calls += 1
    assert error.value.code == "duplicate_message"
    assert calls == 1
    assert len(documents(store, cid)) == 2


async def test_two_instances_serialize_different_requests_and_dedupe_pending(store):
    cid = await conversation(store)
    second = other_instance(store)
    requested = str(uuid4())
    async with store.open_turn(USER, cid, requested, "luna", "first", "v1") as first:
        await assert_error(
            409,
            second.open_turn(USER, cid, requested, "luna", "first", "v1").__aenter__(),
            "duplicate_message",
        )
        await assert_error(
            409,
            second.open_turn(USER, cid, str(uuid4()), "luna", "second", "v1").__aenter__(),
            "conversation_busy",
        )
        await first.finish("completed", "answer", {}, {})
    async with second.open_turn(USER, cid, str(uuid4()), "luna", "second", "v1") as turn:
        assert turn.messages[-2] == {"role": "assistant", "content": "answer"}
        await turn.finish("completed", "done", {}, {})
    assert [row["sequence"] for row in documents(store, cid)] == [1, 2, 3, 4]


async def test_simultaneous_empty_conversation_acquisition_has_one_winner(store):
    cid = await conversation(store)
    contexts = [
        instance.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
        for instance in (store, other_instance(store))
    ]
    results = await asyncio.gather(
        *(context.__aenter__() for context in contexts), return_exceptions=True
    )
    winners = [
        (context, turn)
        for context, turn in zip(contexts, results)
        if not isinstance(turn, ApiError)
    ]
    assert len(winners) == 1
    assert sum(isinstance(result, ApiError) and result.status == 409 for result in results) == 1
    context, turn = winners[0]
    await turn.finish("completed", "answer", {}, {})
    await context.__aexit__(None, None, None)
    assert len(documents(store, cid)) == 2


async def test_heartbeat_renews_and_checkpoints_partial_text_and_stops(store):
    cid = await conversation(store)
    store._heartbeat_seconds = 0.005
    store._lease_seconds = 5
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
        expiry = control(store, cid)["expiresAt"]
        turn.content = "partial response"
        await wait_until(lambda: documents(store, cid)[1]["content"] == "partial response")
        turn.assert_lease()
        assert control(store, cid)["expiresAt"] > expiry
        assert documents(store, cid)[1]["content"] == "partial response"
        await turn.finish("completed", "finished response", {}, {})
    assert turn._lease.task.done()
    assert control(store, cid)["owner"] is None
    assert documents(store, cid)[1]["content"] == "finished response"
    assert documents(store, cid)[1]["status"] == "completed"


async def test_heartbeat_storage_failure_stops_generation_and_retains_pending_for_recovery(store):
    cid = await conversation(store)
    store._heartbeat_seconds = 0.005
    context = store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
    turn = await context.__aenter__()
    turn.content = "checkpointed"
    await wait_until(lambda: documents(store, cid)[1]["content"] == "checkpointed")
    store.messages.failures["batch"] = [cosmos_error(503)]
    await wait_until(turn._lease.task.done)
    with pytest.raises(ApiError) as error:
        turn.assert_lease()
    assert error.value.code == "history_unavailable"
    await assert_error(503, context.__aexit__(None, None, None))
    assert turn._lease.task.done()
    assert [row["status"] for row in documents(store, cid)] == ["pending", "pending"]
    control(store, cid)["expiresAt"] = 0
    page = await other_instance(store).list_messages(USER, cid, 10, None)
    assert [row["status"] for row in page["items"]] == ["interrupted", "interrupted"]
    assert page["items"][1]["content"] == "checkpointed"
    await assert_error(
        409,
        store.open_turn(USER, cid, turn.client_message_id, "luna", "hello", "v1").__aenter__(),
        "duplicate_message",
    )


async def test_delayed_expired_writer_cannot_commit_or_release_successor_lease(store):
    cid = await conversation(store)
    first_context = store.open_turn(USER, cid, str(uuid4()), "luna", "old", "v1")
    old = await first_context.__aenter__()
    await old._lease.stop()
    reached, resume = asyncio.Event(), asyncio.Event()

    async def delay_finalization(operations):
        if any(
            kind == "replace" and args[0] == old.message_id and args[1].get("status") == "completed"
            for kind, args, _ in operations
        ):
            store.messages.before_batch = None
            reached.set()
            await resume.wait()

    store.messages.before_batch = delay_finalization
    delayed_finish = asyncio.create_task(old.finish("completed", "obsolete output", {}, {}))
    await reached.wait()
    control(store, cid)["expiresAt"] = 0
    second = other_instance(store)
    async with second.open_turn(USER, cid, str(uuid4()), "luna", "new", "v1") as successor:
        assert successor.messages == [{"role": "user", "content": "new"}]
        successor_owner = control(store, cid)["owner"]
        resume.set()
        await assert_error(409, delayed_finish, "lease_lost")
        await assert_error(409, first_context.__aexit__(None, None, None), "lease_lost")
        assert control(store, cid)["owner"] == successor_owner
        assert documents(store, cid)[1]["status"] == "interrupted"
        assert documents(store, cid)[1]["content"] != "obsolete output"
        await successor.finish("completed", "current output", {}, {})
    assert [row["sequence"] for row in documents(store, cid)] == [1, 2, 3, 4]
    assert old._lease.task.done()


async def test_feedback_only_on_owned_assistant_and_survives_heartbeat_and_finish(store):
    cid = await conversation(store)
    store._heartbeat_seconds = 0.005
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
        await assert_error(404, store.feedback(OTHER, cid, turn.message_id, "up"))
        await assert_error(404, store.feedback(USER, cid, turn.client_message_id, "up"))
        await assert_error(400, store.feedback(USER, cid, turn.message_id, "invalid"))
        await store.feedback(USER, cid, turn.message_id, "up")
        turn.content = "partial"
        await wait_until(lambda: documents(store, cid)[1]["content"] == "partial")
        await turn.finish("completed", "complete", {}, {})
    assert documents(store, cid)[1]["feedback"] == "up"
    await store.feedback(USER, cid, turn.message_id, "down")
    assert documents(store, cid)[1]["feedback"] == "down"
    await store.feedback(USER, cid, turn.message_id, None)
    assert documents(store, cid)[1]["feedback"] is None


async def test_feedback_transaction_conflict_retries_without_losing_generation(store):
    cid = await conversation(store)
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
        injected = False

        async def concurrent_feedback(operations):
            nonlocal injected
            if injected:
                return
            injected = True
            row = copy.deepcopy(documents(store, cid)[1])
            row["feedback"] = "up"
            store.messages.store(row)

        store.messages.before_batch = concurrent_feedback
        await turn.finish("completed", "answer", {}, {})
        store.messages.before_batch = None
    assert injected
    assert documents(store, cid)[1]["feedback"] == "up"
    assert documents(store, cid)[1]["status"] == "completed"


@pytest.mark.parametrize("raise_error", [True, False])
async def test_unfinished_context_finalizes_interrupted_with_partial_output(store, raise_error):
    cid = await conversation(store)

    async def generate():
        async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
            turn.content = "partial"
            if raise_error:
                raise RuntimeError("upstream failed")

    if raise_error:
        with pytest.raises(RuntimeError, match="upstream failed"):
            await generate()
    else:
        await generate()
    rows = documents(store, cid)
    assert [row["status"] for row in rows] == ["interrupted", "interrupted"]
    assert rows[1]["content"] == "partial"
    assert control(store, cid)["owner"] is None


async def test_cancellation_has_bounded_cleanup_and_no_orphan_heartbeat(store):
    cid = await conversation(store)
    ready = asyncio.Event()
    captured = []

    async def generate():
        async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
            captured.append(turn)
            turn.content = "partial before disconnect"
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(generate())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [row["status"] for row in documents(store, cid)] == ["cancelled", "cancelled"]
    assert documents(store, cid)[1]["content"] == "partial before disconnect"
    assert captured[0]._lease.task.done()
    assert control(store, cid)["owner"] is None


async def test_async_exit_stack_completed_turn_does_not_finalize_twice_or_check_expired_lease(
    store,
):
    cid = await conversation(store)
    stack = AsyncExitStack()
    turn = await stack.enter_async_context(
        store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
    )
    await turn.finish("completed", "answer", {}, {})
    turn._lease.expires_at = 0
    await stack.aclose()
    assert documents(store, cid)[1]["status"] == "completed"
    assert turn._lease.task.done()


async def test_finish_persistence_failure_is_reported_not_success_shaped(store):
    cid = await conversation(store)
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
        store.messages.failures["batch"] = [cosmos_error(503)]
        await assert_error(503, turn.finish("completed", "partial", {}, {}), "history_unavailable")
        assert not turn.finalized
        assert documents(store, cid)[1]["status"] == "pending"
    assert documents(store, cid)[1]["status"] == "interrupted"


async def test_ambiguous_reservation_is_not_replayed_and_is_recovered_after_expiry(store):
    cid = await conversation(store)
    requested = str(uuid4())
    inference_calls = 0

    async def fail_after_reservation(operations):
        if any(kind == "create" for kind, _, _ in operations):
            store.messages.before_batch = None
            store.messages.after_batch_error = cosmos_error(503)

    store.messages.before_batch = fail_after_reservation
    with pytest.raises(ApiError) as error:
        async with store.open_turn(USER, cid, requested, "luna", "hello", "v1"):
            inference_calls += 1
    assert error.value.status == 503
    assert inference_calls == 0
    assert [row["status"] for row in documents(store, cid)] == ["pending", "pending"]
    assert control(store, cid)["owner"] is not None
    await assert_error(
        409,
        other_instance(store).open_turn(USER, cid, requested, "luna", "hello", "v1").__aenter__(),
        "duplicate_message",
    )
    control(store, cid)["expiresAt"] = 0
    assert all(
        row["status"] == "interrupted"
        for row in (await store.list_messages(USER, cid, 10, None))["items"]
    )


async def test_ambiguous_final_commit_stays_durable_and_duplicate_is_rejected(store):
    cid = await conversation(store)
    context = store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
    turn = await context.__aenter__()
    store.messages.after_batch_error = cosmos_error(503)
    await assert_error(503, turn.finish("completed", "committed answer", {}, {}))
    await assert_error(409, context.__aexit__(None, None, None))
    assert documents(store, cid)[1]["status"] == "completed"
    assert documents(store, cid)[1]["content"] == "committed answer"
    await assert_error(
        409,
        other_instance(store)
        .open_turn(USER, cid, turn.client_message_id, "luna", "hello", "v1")
        .__aenter__(),
        "duplicate_message",
    )
    assert turn._lease.task.done()


async def test_cleanup_timeout_preserves_pending_and_does_not_orphan_heartbeat(store):
    cid = await conversation(store)
    store._cleanup_seconds = 0.02
    context = store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
    turn = await context.__aenter__()

    async def stall(operations):
        await asyncio.Event().wait()

    store.messages.before_batch = stall
    await assert_error(503, context.__aexit__(None, None, None), "history_unavailable")
    assert turn._lease.task.done()
    assert documents(store, cid)[1]["status"] == "pending"
    store.messages.before_batch = None
    control(store, cid)["expiresAt"] = 0
    await store.list_messages(USER, cid, 10, None)
    assert documents(store, cid)[1]["status"] == "interrupted"


async def test_large_manual_delete_is_batched_and_retains_only_fencing_tombstones(store):
    cid = await conversation(store)
    for _ in range(51):
        await completed_turn(store, cid)
    assert len(documents(store, cid)) == 102
    await store.delete_conversation(USER, cid)
    assert documents(store, cid) == []
    assert max(store.messages.batch_sizes) == 100
    assert control(store, cid)["state"] == "deleted"
    tombstone = store.conversations.documents[(USER, cid)]
    assert tombstone["state"] == "deleted" and tombstone["title"] == ""
    await store.delete_conversation(USER, cid)
    await assert_error(404, store.list_messages(USER, cid, 10, None))
    assert not any("ttl" in item for item in store.messages.documents.values())


async def test_delete_failure_is_tombstoned_hidden_and_retryable(store):
    cid = await conversation(store)
    turn = await completed_turn(store, cid)
    store.messages.failures["batch"] = [cosmos_error(503)]
    await assert_error(503, store.delete_conversation(USER, cid))
    assert store.conversations.documents[(USER, cid)]["state"] == "deleting"
    assert control(store, cid)["state"] == "deleting"
    assert control(store, cid)["owner"] is None
    assert (await store.list_conversations(USER, 10, None))["items"] == []
    await assert_error(404, store.feedback(USER, cid, turn.message_id, "up"))
    await assert_error(404, store.rename_conversation(USER, cid, "rename"))
    await assert_error(
        404, store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1").__aenter__()
    )
    await other_instance(store).delete_conversation(USER, cid)
    await store.delete_conversation(USER, cid)
    assert documents(store, cid) == []


async def test_delete_recovers_after_control_tombstone_but_before_metadata_completion(store):
    cid = await conversation(store)
    await completed_turn(store, cid)

    async def fail_last_metadata_update(operations):
        if operations[0][1][1].get("state") == "deleted":
            store.conversations.failures["replace"] = [cosmos_error(503)]
            store.messages.before_batch = None

    store.messages.before_batch = fail_last_metadata_update
    await assert_error(503, store.delete_conversation(USER, cid))
    assert control(store, cid)["state"] == "deleted"
    assert store.conversations.documents[(USER, cid)]["state"] == "deleting"
    await store.delete_conversation(USER, cid)
    assert store.conversations.documents[(USER, cid)]["state"] == "deleted"


async def test_delete_rejects_live_generation_then_cleans_up_expired_generation(store):
    cid = await conversation(store)
    context = store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
    turn = await context.__aenter__()
    await assert_error(
        409, other_instance(store).delete_conversation(USER, cid), "conversation_busy"
    )
    assert len(documents(store, cid)) == 2
    await turn._lease.stop()
    control(store, cid)["expiresAt"] = 0
    await other_instance(store).delete_conversation(USER, cid)
    await assert_error(409, context.__aexit__(None, None, None), "lease_lost")
    assert documents(store, cid) == []
    assert control(store, cid)["owner"] is None


async def test_missing_pending_pair_is_reported_without_silent_recovery(store):
    cid = await conversation(store)
    context = store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
    turn = await context.__aenter__()
    await turn._lease.stop()
    del store.messages.documents[(USER, cid, turn.message_id)]
    control(store, cid)["expiresAt"] = 0
    await assert_error(503, store.list_messages(USER, cid, 10, None), "history_inconsistent")
    await assert_error(409, context.__aexit__(None, None, None), "lease_lost")


async def test_storage_errors_are_sanitized_and_do_not_return_empty_success(store):
    store.conversations.failures["create"] = [cosmos_error(403)]
    with pytest.raises(ApiError) as error:
        await store.create_conversation(USER, "title")
    assert error.value.status == 503
    assert "fake Cosmos failure" not in error.value.message
    store.conversations.failures["query"] = [cosmos_error(429)]
    await assert_error(503, store.list_conversations(USER, 20, None), "history_unavailable")


async def test_live_pending_messages_are_visible_without_recovery_or_lease_changes(store):
    cid = await conversation(store)
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
        owner = control(store, cid)["owner"]
        page = await other_instance(store).list_messages(USER, cid, 20, None)
        assert [row["status"] for row in page["items"]] == ["pending", "pending"]
        assert control(store, cid)["owner"] == owner
        await turn.finish("completed", "answer", {}, {})


async def test_metadata_failure_is_reported_and_repeat_finish_retries_metadata_only(store):
    cid = await conversation(store)
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
        store.conversations.failures["replace"] = [cosmos_error(503)]
        await assert_error(503, turn.finish("completed", "answer", {}, {}))
        assert turn.finalized
        assert documents(store, cid)[1]["status"] == "completed"
        batch_count = len(store.messages.batch_sizes)
        await turn.finish("completed", "answer", {}, {})
        assert turn._metadata_updated
        assert len(store.messages.batch_sizes) == batch_count
        await assert_error(409, turn.finish("failed", "answer", {}, {}), "history_conflict")
    assert documents(store, cid)[1]["status"] == "completed"


async def test_cancellation_storage_failure_retains_pending_without_orphan_heartbeat(store):
    cid = await conversation(store)
    ready = asyncio.Event()
    captured = []

    async def generate():
        async with store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1") as turn:
            captured.append(turn)
            turn.content = "partial"
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(generate())
    await ready.wait()
    store.messages.failures["batch"] = [cosmos_error(503)]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured[0]._lease.task.done()
    assert documents(store, cid)[1]["status"] == "pending"
    control(store, cid)["expiresAt"] = 0
    await store.list_messages(USER, cid, 10, None)
    assert documents(store, cid)[1]["status"] == "interrupted"


async def test_read_documents_with_mismatching_owner_never_escape_projection(store):
    cid = await conversation(store)
    store.conversations.documents[(USER, cid)]["userId"] = OTHER
    await assert_error(404, store.rename_conversation(USER, cid, "title"))
    await assert_error(404, store.list_messages(USER, cid, 10, None))
    await assert_error(404, store.delete_conversation(USER, cid))


async def test_orphan_completed_user_is_not_added_to_provider_context(store):
    cid = await conversation(store)
    first = await completed_turn(store, cid, message="orphan user", answer="removed assistant")
    del store.messages.documents[(USER, cid, first.message_id)]
    async with store.open_turn(USER, cid, str(uuid4()), "luna", "new user", "v1") as turn:
        assert turn.messages == [{"role": "user", "content": "new user"}]
        await turn.finish("completed", "answer", {}, {})


async def test_unexpected_heartbeat_cancellation_fails_synchronous_lease_guard(store):
    cid = await conversation(store)
    context = store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
    turn = await context.__aenter__()
    turn._lease.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn._lease.task
    with pytest.raises(ApiError) as error:
        turn.assert_lease()
    assert error.value.code == "lease_lost"
    await assert_error(409, context.__aexit__(None, None, None), "lease_lost")
    assert documents(store, cid)[1]["status"] == "pending"


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("source", ["unavailable", None])
async def test_endpoint_stream_and_exit_stack_cleanup_can_use_different_tasks(
    store, complete, source
):
    cid = await conversation(store)
    task_ids = []
    usage = {"promptTokens": None, "completionTokens": None, "source": source}

    async def endpoint():
        task_ids.append(asyncio.current_task())
        stack = AsyncExitStack()
        turn = await stack.enter_async_context(
            store.open_turn(USER, cid, str(uuid4()), "luna", "hello", "v1")
        )
        return stack, turn

    stack, turn = await asyncio.create_task(endpoint())
    assert isinstance(turn, Turn)
    assert not turn._lease.task.done()

    async def stream():
        task_ids.append(asyncio.current_task())
        turn.assert_lease()
        turn.content = "response text"
        turn.assert_lease()
        if complete:
            await turn.finish("completed", turn.content, usage, {"durationMs": None})

    await asyncio.create_task(stream())

    async def response_cleanup():
        task_ids.append(asyncio.current_task())
        await stack.aclose()

    await asyncio.create_task(response_cleanup())
    assert len(set(task_ids)) == 3
    assert turn._lease.task.done()
    assert control(store, cid)["owner"] is None
    rows = documents(store, cid)
    assert rows[1]["content"] == "response text"
    assert [row["status"] for row in rows] == ["completed" if complete else "interrupted"] * 2
    if complete:
        assert rows[1]["usage"] == usage
        assert rows[1]["metrics"] == {"durationMs": None}
