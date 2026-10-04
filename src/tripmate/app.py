import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager

from ag_ui.core import (
    CustomEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage
from pydantic import BaseModel

from tripmate import rails_stub, repo
from tripmate.backend import travel_graph
from tripmate.db import apply_schema, pool
from tripmate.rails_stub import RequestContext


@asynccontextmanager
async def lifespan(_: FastAPI):
    apply_schema()
    yield
    pool.close()


app = FastAPI(title="TripMate", lifespan=lifespan)
encoder = EventEncoder()

# Graph nodes in the order they run (see backend.py)
NODE_ORDER = ["flight_agent", "hotel_agent", "itinerary_agent", "final_agent"]

ARTIFACT_TYPE = "trip_plan"


# ------------------------------------------------------------------ request context

def get_ctx(x_employee_id: int | None = Header(default=None)) -> RequestContext:
    """Stand-in for Rails auth: the caller says which employee they are."""
    ctx = rails_stub.resolve_context(x_employee_id) if x_employee_id else None
    if not ctx:
        raise HTTPException(401, "Unknown employee (send an X-Employee-Id header)")
    return ctx


class ChatRequest(BaseModel):
    message: str
    conversation_id: uuid.UUID | None = None


class RenameRequest(BaseModel):
    title: str


class FeedbackRequest(BaseModel):
    rating: str
    comment: str | None = None


# ------------------------------------------------------------------ helpers

def step_detail(node: str, update: dict) -> str | None:
    """A one-line summary of what a node produced, shown in the thinking panel."""
    if node == "flight_agent":
        return (update.get("flight_results") or "").strip().split("\n")[0] or None
    if node == "hotel_agent":
        text = update.get("hotel_results") or ""
        count = sum(1 for line in text.splitlines() if line[:1].isdigit())
        return f"Found {count} hotel sources" if count else None
    return None


# What each tool-using node calls, so we can record it as tool_call / tool_result events
def tool_for(node: str, message: str, update: dict) -> tuple[str, dict, str] | None:
    if node == "flight_agent":
        return "search_flights", {"query": message}, update.get("flight_results", "")
    if node == "hotel_agent":
        return "search_tavily", {"query": f"Best hotels for {message}"}, update.get("hotel_results", "")
    return None


def persist_tool_events(ctx, conversation_id, root_id, name, args, result) -> None:
    call = repo.insert_event(
        ctx, conversation_id, type="tool_call", role="assistant",
        reply_to_id=root_id, parent_id=root_id, data={"name": name, "args": args},
    )
    repo.insert_event(
        ctx, conversation_id, type="tool_result", role="tool",
        reply_to_id=root_id, parent_id=call["id"], data={"result": result},
    )


# ------------------------------------------------------------------ the runner

def stream_run(ctx: RequestContext, conversation_id, user_event: dict,
               seen_artifact: dict | None) -> Iterator[str]:
    """Runs the graph and (a) streams AG-UI events to the browser, (b) persists each event
    to agents.conversation_events once it is final."""
    message = user_event["content"]
    root_id = user_event["id"]
    run_id = uuid.uuid4().hex
    message_id = uuid.uuid4().hex
    reasoning_id = uuid.uuid4().hex
    reasoning_open = False
    answer_open = False
    answer_text = ""
    collected: dict = {}

    def emit(event) -> str:
        return encoder.encode(event)

    yield emit(RunStartedEvent(thread_id=str(conversation_id), run_id=run_id))
    yield emit(StepStartedEvent(step_name=NODE_ORDER[0]))

    config = {"configurable": {"thread_id": str(conversation_id)}}  # thread_id == conversation id
    try:
        for mode, payload in travel_graph.stream(
            {
                "messages": [HumanMessage(content=message)],
                "user_query": message,
                "flight_results": "",
                "hotel_results": "",
                "itinerary": "",
                "llm_calls": 0,
            },
            config=config,
            stream_mode=["updates", "messages"],
        ):
            if mode == "messages":
                chunk, meta = payload
                node = meta.get("langgraph_node")
                if not isinstance(chunk, AIMessageChunk) or not chunk.content:
                    continue
                text = chunk.content if isinstance(chunk.content, str) else ""
                if not text:
                    continue

                # The itinerary draft is the "thinking"; the final agent writes the answer
                if node == "itinerary_agent":
                    if not reasoning_open:
                        reasoning_open = True
                        yield emit(ReasoningMessageStartEvent(message_id=reasoning_id, role="reasoning"))
                    yield emit(ReasoningMessageContentEvent(message_id=reasoning_id, delta=text))
                elif node == "final_agent":
                    if reasoning_open:
                        reasoning_open = False
                        yield emit(ReasoningMessageEndEvent(message_id=reasoning_id))
                    if not answer_open:
                        answer_open = True
                        yield emit(TextMessageStartEvent(message_id=message_id, role="assistant"))
                    answer_text += text
                    yield emit(TextMessageContentEvent(message_id=message_id, delta=text))

            elif mode == "updates":
                for node, update in payload.items():
                    if node not in NODE_ORDER:
                        continue
                    update = update or {}
                    collected.update(
                        {k: v for k, v in update.items() if k in ("flight_results", "hotel_results", "itinerary")}
                    )

                    # A tool node finished: its call + result are final, so persist them now
                    tool = tool_for(node, message, update)
                    if tool:
                        persist_tool_events(ctx, conversation_id, root_id, *tool)

                    detail = step_detail(node, update)
                    if detail:
                        yield emit(CustomEvent(name="step_detail", value={"step": node, "detail": detail}))
                    yield emit(StepFinishedEvent(step_name=node))
                    idx = NODE_ORDER.index(node)
                    if idx + 1 < len(NODE_ORDER):
                        yield emit(StepStartedEvent(step_name=NODE_ORDER[idx + 1]))

        if reasoning_open:
            yield emit(ReasoningMessageEndEvent(message_id=reasoning_id))
        if answer_open:
            yield emit(TextMessageEndEvent(message_id=message_id))

        # The answer is final: persist it, then the artifact it produced
        answer_event = repo.insert_event(
            ctx, conversation_id, type="text", role="assistant", content=answer_text,
            reply_to_id=root_id, parent_id=root_id,
        )
        plan = {
            "query": message,
            "flights": collected.get("flight_results", ""),
            "hotels": collected.get("hotel_results", ""),
            "itinerary": collected.get("itinerary", ""),
        }
        if seen_artifact:
            created = repo.revise_artifact(
                ctx, seen_artifact["id"], seen_artifact["current_version_id"], root_id, plan
            )
        else:
            created = repo.create_artifact(ctx, conversation_id, root_id, 0, ARTIFACT_TYPE, plan)
        artifact_event = repo.insert_event(
            ctx, conversation_id, type="artifact", role="assistant",
            reply_to_id=root_id, parent_id=answer_event["id"], artifact_id=created["artifact_id"],
            data={"artifact_version_id": str(created["version_id"])},
        )
        # Checkpoint keeps the id only, never the artifact body
        travel_graph.update_state(
            config, {"active_artifact_id": str(created["artifact_id"])}, as_node="final_agent"
        )
        yield emit(
            CustomEvent(
                name="persisted",
                value={
                    "assistant_event_id": str(answer_event["id"]),
                    "artifact": {
                        "artifact_id": str(created["artifact_id"]),
                        "version_id": str(created["version_id"]),
                        "version_number": created["version_number"],
                        "event_id": str(artifact_event["id"]),
                        "build_status": None,
                        "build_results": None,
                        "query": message,
                    },
                },
            )
        )
        yield emit(RunFinishedEvent(thread_id=str(conversation_id), run_id=run_id))

    except GeneratorExit:
        # The client stopped/disconnected: keep the partial answer, marked cancelled
        if answer_text:
            repo.insert_event(
                ctx, conversation_id, type="text", role="assistant", content=answer_text,
                reply_to_id=root_id, parent_id=root_id, status="cancelled",
            )
        raise
    except Exception as e:  # surface failures to the UI instead of dropping the stream
        err = {"code": type(e).__name__, "message": str(e)}
        repo.insert_event(
            ctx, conversation_id, type="error", role="system",
            reply_to_id=root_id, parent_id=root_id, data=err, status="failed",
        )
        yield emit(RunErrorEvent(message=f"{err['code']}: {err['message']}"))


async def event_stream(make_gen: Callable[[], Iterator[str]]) -> AsyncIterator[str]:
    """Run the graph in its own thread and relay its events to the response.

    The runner is not tied to the HTTP connection: when the client disconnects, the
    `finally` below sets `stop`, and the runner thread (which is never suspended on the
    response) sees it right after its next event, closes the generator, and so saves the
    partial answer as `cancelled`.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    done = object()

    def put(item) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # event loop already closed (server shutting down)
            pass

    def worker() -> None:
        gen = make_gen()
        try:
            for chunk in gen:
                put(chunk)
                if stop.is_set():
                    gen.close()  # runs the GeneratorExit handler in stream_run
                    break
        finally:
            put(done)

    threading.Thread(target=worker, daemon=True).start()
    try:
        while True:
            chunk = await queue.get()
            if chunk is done:
                break
            yield chunk
    finally:
        stop.set()


# ------------------------------------------------------------------ routes

@app.get("/employees")
def employees():
    """Dummy login list (Rails owns the real accounts / employees)."""
    return rails_stub.list_employees()


@app.get("/conversations")
def conversations(ctx: RequestContext = Depends(get_ctx)):
    return repo.list_conversations(ctx)


@app.get("/conversations/{conversation_id}")
def conversation(conversation_id: uuid.UUID, ctx: RequestContext = Depends(get_ctx)):
    conv = repo.get_conversation(ctx, conversation_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return {
        "conversation": conv,
        "events": repo.load_events(ctx, conversation_id),
        "artifacts": repo.artifacts_for_conversation(ctx, conversation_id),
        "feedback": repo.feedback_for_conversation(ctx, conversation_id),
    }


@app.patch("/conversations/{conversation_id}")
def rename(conversation_id: uuid.UUID, body: RenameRequest, ctx: RequestContext = Depends(get_ctx)):
    if not repo.rename_conversation(ctx, conversation_id, body.title):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


@app.delete("/conversations/{conversation_id}")
def delete(conversation_id: uuid.UUID, ctx: RequestContext = Depends(get_ctx)):
    if not repo.delete_conversation(ctx, conversation_id):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


@app.post("/agent")
def agent(req: ChatRequest, ctx: RequestContext = Depends(get_ctx)):
    # The API creates the conversation and inserts the user's event
    if req.conversation_id:
        conv = repo.get_conversation(ctx, req.conversation_id)
        if not conv:
            raise HTTPException(404, "Conversation not found")
    else:
        conv = repo.create_conversation(ctx)
    seen_artifact = repo.latest_artifact(ctx, conv["id"])
    user_event = repo.insert_event(ctx, conv["id"], type="text", role="user", content=req.message)
    repo.auto_title(ctx, conv["id"], req.message)

    return StreamingResponse(
        event_stream(lambda: stream_run(ctx, conv["id"], user_event, seen_artifact)),
        media_type=encoder.get_content_type(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/conversations/{conversation_id}/artifacts/{artifact_id}/versions/{version_id}/build")
def build(conversation_id: uuid.UUID, artifact_id: uuid.UUID, version_id: uuid.UUID,
          ctx: RequestContext = Depends(get_ctx)):
    """Build is a new invocation (not an interrupt): the user's click is an `action` event."""
    if not repo.get_conversation(ctx, conversation_id):
        raise HTTPException(404, "Conversation not found")
    claim = repo.claim_build(ctx, conversation_id, artifact_id, version_id)
    if claim["outcome"] == "missing":
        raise HTTPException(404, "Artifact version not found")

    # The artifact event that showed this version is the one that was clicked
    shown = [
        e for e in repo.load_events(ctx, conversation_id)
        if e["type"] == "artifact" and (e["data"] or {}).get("artifact_version_id") == str(version_id)
    ]
    action = repo.insert_event(
        ctx, conversation_id, type="action", role="user", artifact_id=artifact_id,
        parent_id=shown[-1]["id"] if shown else None,
        data={"action": "build", "artifact_version_id": str(version_id)},
    )

    def reply(text: str, parent_id):
        return repo.insert_event(
            ctx, conversation_id, type="text", role="assistant", content=text,
            reply_to_id=action["id"], parent_id=parent_id, artifact_id=artifact_id,
        )

    version = claim["version"]
    if claim["outcome"] == "built":
        ids = ", ".join(version["build_results"]["booking_ids"])
        reply(f"This version is already booked. Booking IDs: {ids}", action["id"])
    elif claim["outcome"] == "building":
        reply("A build for this version is already in progress.", action["id"])
    else:
        call = repo.insert_event(
            ctx, conversation_id, type="tool_call", role="assistant", artifact_id=artifact_id,
            reply_to_id=action["id"], parent_id=action["id"],
            data={"name": "rails_build_trip", "args": {"artifact_version_id": str(version_id)}},
        )
        key = f"agents:artifact-version:{version_id}:build"  # derived, stable across retries
        started_at = version["build_started_at"]
        try:
            result = rails_stub.build_trip(key, ctx.account_id, version["data"])
        except rails_stub.RailsRejected as e:
            # definite rejection -> failed (retryable)
            error = {"error": {"code": e.code, "message": e.message}}
            repo.finish_build(ctx, version_id, started_at, status="failed", results=error)
            res = repo.insert_event(
                ctx, conversation_id, type="tool_result", role="tool", artifact_id=artifact_id,
                reply_to_id=action["id"], parent_id=call["id"], data=error,
            )
            reply(f"The booking was rejected: {e.message}", res["id"])
        except Exception as e:
            # unknown outcome -> leave `building`; a retry with the same key finds out
            error = {"error": {"code": type(e).__name__, "message": str(e)}}
            res = repo.insert_event(
                ctx, conversation_id, type="tool_result", role="tool", artifact_id=artifact_id,
                reply_to_id=action["id"], parent_id=call["id"], data=error, status="failed",
            )
            reply("I couldn't confirm the booking yet. You can retry safely.", res["id"])
        else:
            repo.finish_build(ctx, version_id, started_at, status="built", results=result)
            res = repo.insert_event(
                ctx, conversation_id, type="tool_result", role="tool", artifact_id=artifact_id,
                reply_to_id=action["id"], parent_id=call["id"], data={"result": result},
            )
            reply(f"Booked! Booking IDs: {', '.join(result['booking_ids'])}", res["id"])

    return {
        "outcome": claim["outcome"],
        "version": repo.get_version(ctx, version_id),
        "events": [e for e in repo.load_events(ctx, conversation_id) if e["seq"] >= action["seq"]],
    }


@app.post("/conversations/{conversation_id}/events/{event_id}/feedback")
def feedback(conversation_id: uuid.UUID, event_id: uuid.UUID, body: FeedbackRequest,
             ctx: RequestContext = Depends(get_ctx)):
    if body.rating not in ("up", "down"):
        raise HTTPException(422, "rating must be 'up' or 'down'")
    row = repo.upsert_feedback(ctx, conversation_id, event_id, body.rating, body.comment)
    if not row:
        raise HTTPException(404, "Event not found")
    return row


@app.get("/health")
def health():
    return {"status": "ok"}
