import json
import uuid
from collections.abc import Iterator

from ag_ui.core import (
    CustomEvent,
    EventType,
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
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage
from pydantic import BaseModel

from tripmate.backend import travel_graph

app = FastAPI(title="TripMate")
encoder = EventEncoder()

# Graph nodes in the order they run (see backend.py)
NODE_ORDER = ["flight_agent", "hotel_agent", "itinerary_agent", "final_agent"]


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


def step_detail(node: str, update: dict) -> str | None:
    """A one-line summary of what a node produced, shown in the thinking panel."""
    if node == "flight_agent":
        return (update.get("flight_results") or "").strip().split("\n")[0] or None
    if node == "hotel_agent":
        text = update.get("hotel_results") or ""
        count = sum(1 for line in text.splitlines() if line[:1].isdigit())
        return f"Found {count} hotel sources" if count else None
    return None


def stream_run(message: str, thread_id: str) -> Iterator[str]:
    run_id = uuid.uuid4().hex
    message_id = uuid.uuid4().hex
    reasoning_id = uuid.uuid4().hex
    reasoning_open = False
    answer_open = False

    def emit(event) -> str:
        return encoder.encode(event)

    yield emit(RunStartedEvent(thread_id=thread_id, run_id=run_id))
    yield emit(StepStartedEvent(step_name=NODE_ORDER[0]))

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
            config={"configurable": {"thread_id": thread_id}},
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
                        yield emit(
                            ReasoningMessageStartEvent(
                                message_id=reasoning_id, role="reasoning"
                            )
                        )
                    yield emit(
                        ReasoningMessageContentEvent(
                            message_id=reasoning_id, delta=text
                        )
                    )
                elif node == "final_agent":
                    if reasoning_open:
                        reasoning_open = False
                        yield emit(ReasoningMessageEndEvent(message_id=reasoning_id))
                    if not answer_open:
                        answer_open = True
                        yield emit(
                            TextMessageStartEvent(
                                message_id=message_id, role="assistant"
                            )
                        )
                    yield emit(
                        TextMessageContentEvent(message_id=message_id, delta=text)
                    )

            elif mode == "updates":
                for node, update in payload.items():
                    if node not in NODE_ORDER:
                        continue
                    detail = step_detail(node, update or {})
                    if detail:
                        yield emit(
                            CustomEvent(
                                name="step_detail",
                                value={"step": node, "detail": detail},
                            )
                        )
                    yield emit(StepFinishedEvent(step_name=node))
                    idx = NODE_ORDER.index(node)
                    if idx + 1 < len(NODE_ORDER):
                        yield emit(StepStartedEvent(step_name=NODE_ORDER[idx + 1]))

        if reasoning_open:
            yield emit(ReasoningMessageEndEvent(message_id=reasoning_id))
        if answer_open:
            yield emit(TextMessageEndEvent(message_id=message_id))
        yield emit(RunFinishedEvent(thread_id=thread_id, run_id=run_id))
    except Exception as e:  # surface failures to the UI instead of dropping the stream
        yield emit(RunErrorEvent(message=f"{type(e).__name__}: {e}"))


@app.post("/agent")
def agent(req: ChatRequest):
    thread_id = req.thread_id or f"user_{uuid.uuid4().hex}"
    return StreamingResponse(
        stream_run(req.message, thread_id),
        media_type=encoder.get_content_type(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
