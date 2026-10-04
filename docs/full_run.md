Let me pull real rows from the last conversation so every payload below is genuine, not invented.

This is the full journey of one message, using real values from Rahul's conversation. IDs are shortened to their last 4 characters. The message is `"Plan a 3 day trip from Mumbai to Jaipur"`, sent by Rahul (employee 502, account 1001).

---

## Step 0: What the browser sends

```http
POST /agent
X-Employee-Id: 502
Content-Type: application/json

{ "message": "Plan a 3 day trip from Mumbai to Jaipur", "conversation_id": null }
```

`conversation_id` is `null` for a new chat. For a follow-up it holds the conversation's UUID.

## Step 1: Auth (no database write)

`get_ctx` looks up header `502` in `rails_stub.EMPLOYEES` and builds:
```python
RequestContext(account_id=1001, employee_id=502)
```
Unknown or missing employee returns **401**. From here on, every query is filtered by these two numbers.

## Step 2: The API prepares the run (before any streaming)

These steps run in order inside the `/agent` handler. All of them are **API writes**.

**2a. Create the conversation** (only when `conversation_id` is null). An existing id is looked up with the scope filter, and a miss returns 404.

`agents.conversations`:

| id | account_id | employee_id | title | title_source | last_message_at | meta | deleted_at |
|---|---|---|---|---|---|---|---|
| `…7f92` (UUIDv7) | 1001 | 502 | `NULL` | `auto` | now | `{}` | `NULL` |

This id is also the LangGraph `thread_id`.

**2b. Read (not write) the latest artifact** for this conversation, so the runner later knows whether to create or revise. A new chat has none.

**2c. Insert the user's event.** `seq = max(seq)+1`, with the conversation row locked for the insert.

`agents.conversation_events`:

| seq | type | role | content | reply_to_id | parent_id | artifact_id | data | status |
|---|---|---|---|---|---|---|---|---|
| 1 | `text` | `user` | "Plan a 3 day trip from Mumbai to Jaipur" | NULL | NULL | NULL | NULL | `completed` |

This row (`…ca15`) is the **root**. Everything generated for this message will have `reply_to_id = ca15`. The insert also sets `conversations.last_message_at = now()`.

**2d. Auto-title:**
```sql
UPDATE conversations SET title = 'Plan a 3 day trip from Mumbai to Jaipur'
WHERE id = … AND title_source = 'auto' AND title IS NULL
```
It only writes while the title is still empty and `title_source` is `auto`.

At this point the database has 1 conversation row and 1 event row. Nothing has been streamed yet.

## Step 3: The response opens

The handler returns a `StreamingResponse`. A worker thread starts the runner `stream_run`, and an async loop copies the runner's output to the socket. The browser receives headers `200`, `Content-Type: text/event-stream`.

Every event on the wire has this exact shape: a `data:` line, then a blank line.
```
data: {"type":"RUN_STARTED","threadId":"…7f92","runId":"eb4a…"}

data: {"type":"STEP_STARTED","stepName":"flight_agent"}

```
These two events are sent immediately, so the thinking panel appears at once. `threadId` is the conversation id, and the browser stores it. **No database write** for these.

## Step 4: Flight node

`travel_graph.stream(...)` starts, with `thread_id = conversation id` in the config. The initial state:
```python
{"messages": [HumanMessage(...)], "user_query": "Plan a 3 day trip from Mumbai to Jaipur",
 "flight_results": "", "hotel_results": "", "itinerary": "", "llm_calls": 0}
```

`flight_agent` runs `search_flights`. `parse_route` finds `BOM → JAI`, then the Aviationstack API is called. The node finishes and LangGraph emits an `updates` item. The runner then does, **in this order**:

1. **Persist** (runner writes) two events, with the call first so the result can point at it:

| seq | type | role | reply_to | parent | data |
|---|---|---|---|---|---|
| 2 | `tool_call` | `assistant` | ca15 | ca15 | `{"name":"search_flights","args":{"query":"Plan a 3 day trip from Mumbai to Jaipur"}}` |
| 3 | `tool_result` | `tool` | ca15 | **63e1** (the call) | `{"result":"Live flights from BOM to JAI\n\nAirline: Air India\nFlight: AI621…"}` |

2. **Stream** three events:
```
data: {"type":"CUSTOM","name":"step_detail","value":{"step":"flight_agent","detail":"Live flights from BOM to JAI"}}
data: {"type":"STEP_FINISHED","stepName":"flight_agent"}
data: {"type":"STEP_STARTED","stepName":"hotel_agent"}
```
`STEP_STARTED` for the next node is sent by us, because the graph only reports when a node ends.

**Also, a LangGraph write (not ours):** after every node, `PostgresSaver` writes a checkpoint to the `checkpoints`, `checkpoint_blobs` and `checkpoint_writes` tables in the `public` schema, keyed by the thread id. This conversation has 14 checkpoints after two runs. They hold the state: messages, `flight_results`, and so on.

## Step 5: Hotel node

Same pattern with a different tool:

| seq | type | role | reply_to | parent | data |
|---|---|---|---|---|---|
| 4 | `tool_call` | `assistant` | ca15 | ca15 | `{"name":"search_tavily","args":{"query":"Best hotels for Plan a 3 day trip from Mumbai to Jaipur"}}` |
| 5 | `tool_result` | `tool` | ca15 | be22 (the call) | `{"result":"1. **3- Day go …"}` |

Streamed: `CUSTOM step_detail` ("Found 5 hotel sources"), `STEP_FINISHED hotel_agent`, `STEP_STARTED itinerary_agent`. Plus another checkpoint.

## Step 6: Itinerary node (the "thinking")

Grok writes a draft. LangGraph's `messages` mode yields each token, and the runner forwards it:
```
data: {"type":"REASONING_MESSAGE_START","messageId":"4fdf…","role":"reasoning"}      ← sent once, on the first token
data: {"type":"REASONING_MESSAGE_CONTENT","messageId":"4fdf…","delta":"#"}
data: {"type":"REASONING_MESSAGE_CONTENT","messageId":"4fdf…","delta":" Delhi"}
   … about 2,000 of these in the last test …
```
**No database write.** Tokens are never persisted. The draft only lives in the graph state (checkpoint) until the artifact is created. When the node ends: `STEP_FINISHED itinerary_agent`, `STEP_STARTED final_agent`, and a checkpoint.

## Step 7: Final node (the answer)

On the first final-agent token, the runner closes the reasoning message and opens the answer:
```
data: {"type":"REASONING_MESSAGE_END","messageId":"4fdf…"}
data: {"type":"TEXT_MESSAGE_START","messageId":"5dd2…","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"5dd2…","delta":"#"}
   … about 1,800 of these …
```
The runner also appends each delta to an in-memory `answer_text` string. **Still no database write.** The browser collapses the thinking panel the moment the first `TEXT_MESSAGE_CONTENT` arrives.

When the graph finishes: `STEP_FINISHED final_agent` and `TEXT_MESSAGE_END`.

## Step 8: Persist the result (runner writes)

The answer is now final, so the runner writes, in this order.

**8a. Assistant text event:**

| seq | type | role | content | reply_to | parent | status |
|---|---|---|---|---|---|---|
| 6 | `text` | `assistant` | the full markdown answer | ca15 | ca15 | `completed` |

**8b. Create the artifact** (first plan in this conversation) in **one transaction**:

`agents.artifacts`:

| id | artifact_type | current_version_id | idempotency_key |
|---|---|---|---|
| `…35ba` | `trip_plan` | `…70e1` | `{conversation_id}:{ca15}:0` |

The key is `{conversation}:{root event}:{ordinal}`, so a re-run of this step finds the same artifact instead of making a second one. The artifact row is inserted first with `current_version_id = NULL`, then v1, then the pointer is set. The deferred foreign key is checked at commit.

`agents.artifact_versions` (v1):

| version_number | created_by_event_id | data (jsonb) | build_status | build_* | built_by |
|---|---|---|---|---|---|
| 1 | ca15 (the user's message) | `{"query": "...", "flights": "...(3601 chars)", "hotels": "...(2036 chars)", "itinerary": "...(7589 chars)"}` | NULL | NULL | NULL |

`build_status = NULL` means this version has never been claimed for a build.

**8c. Artifact event:**

| seq | type | role | reply_to | parent | artifact_id | data |
|---|---|---|---|---|---|---|
| 7 | `artifact` | `assistant` | ca15 | **782c** (the answer text, seq 6) | `…35ba` | `{"artifact_version_id":"…70e1"}` |

It records **which version was shown here**, which is what lets an older card show its own version later.

**8d. Checkpoint update, ids only:**
```python
travel_graph.update_state(config, {"active_artifact_id": "…35ba"}, as_node="final_agent")
```
This writes one more checkpoint that carries only the artifact id, never the plan body.

**8e. Final stream events:**
```
data: {"type":"CUSTOM","name":"persisted","value":{
        "assistant_event_id":"…782c",
        "artifact":{"artifact_id":"…35ba","version_id":"…70e1","version_number":1,
                    "event_id":"…a755","build_status":null,"query":"Plan a 3 day trip…"}}}
data: {"type":"RUN_FINISHED","threadId":"…7f92","runId":"eb4a…"}
```
The browser uses `persisted` to draw the Trip plan card and the thumbs buttons without reloading. The stream closes.

---

## Step 9: User clicks Build (a new request, not part of the stream)

```http
POST /conversations/{cid}/artifacts/{aid}/versions/{vid}/build
X-Employee-Id: 502
```
There is no body, because the ids are in the URL. The response is plain JSON, not SSE.

1. **Claim** (API writes): lock the version row, verify it belongs to this artifact, conversation and scope. Because `build_status` is NULL, the row is updated:
   `build_status='building', build_started_at=now(), built_by=502, build_finished_at=NULL, build_results=NULL`.
   If it were already `built`, the API returns the stored results. If it were `building`, it reports in progress. A `failed` or stale claim can be re-claimed.
2. **Action event** (API writes): seq 8, type `action`, role `user`, `reply_to_id` NULL (new root), `parent_id` = the artifact event (a755), `artifact_id` set, `data = {"action":"build","artifact_version_id":"…70e1"}`.
3. **Tool call** (runner writes): seq 9, `data = {"name":"rails_build_trip","args":{"artifact_version_id":"…70e1"}}`, `reply_to = parent = d666` (the action).
4. **Call Rails** with `Idempotency-Key: agents:artifact-version:{version_id}:build`. This key is derived, not stored.
5. **Outcome**:
   - **Success:** the version becomes `built`, `build_finished_at=now()`, `build_results={"account_id":1001,"booking_ids":["BK-3929F4EB","BK-334BAB6D"]}`. A `tool_result` event (seq 10) holds the same result. Then the assistant `text` "Booked! Booking IDs: …" (seq 11).
   - **Definite rejection:** the version becomes `failed`, with `build_results={"error":{"code":"empty_plan",…}}`. It stays retryable.
   - **Unknown error:** the version stays `building`. A retry with the same key finds out what happened.

   The completion update only matches if `build_status='building' AND build_started_at=<the value I claimed>`, so an older worker can't overwrite a newer claim.

The browser then reloads the conversation, and the card shows `built` plus the IDs.

## Step 10: A follow-up message (revision)

Same as Steps 0 to 8, except:
- Step 2b now finds the artifact. It remembers `seen = current_version_id` (v1).
- The new user event is seq 12, a new root.
- In 8b, **revise** runs instead of create, in one transaction: lock the artifact row, return the existing version if this request already made one, otherwise insert v2 (`created_by_event_id` = the new user event), then
  `UPDATE artifacts SET current_version_id = v2 WHERE id = … AND current_version_id = <seen v1>`.
  If that updates 0 rows, another revision won and everything rolls back.
- v1 keeps its `built` status. Revising doesn't touch builds.

## Step 11: Thumbs click

```http
POST /conversations/{cid}/events/{assistant_event_id}/feedback
{"rating":"up","comment":null}
```
The API checks the event belongs to this conversation and scope, then runs an upsert on `UNIQUE(conversation_event_id, employee_id)`:

| conversation_event_id | employee_id | rating | comment |
|---|---|---|---|
| …782c | 502 | `up` | NULL |

A second click updates the same row.

## Step 12: Failure and Stop paths

- **Crash during the run** (for example the earlier "Could not find a country for 'jaipur'"): the runner inserts an `error` event (role `system`, `status='failed'`, `data={"code":"ValueError","message":"…"}`), then streams `RUN_ERROR`. The user event and any tool events that already completed stay in the table. No artifact is created.
- **Stop or disconnect during the answer:** the response closes, a stop flag is set, and the runner writes the partial `answer_text` as a `text` event with `status='cancelled'`. No artifact event is written.

## Step 13: Loading history later

`GET /conversations/{id}` returns four things: the conversation row, all events ordered by `seq`, all artifacts with their versions (including build state), and a map of the caller's ratings. The browser groups events by `reply_to_id` into exchanges. This is why a reload shows the same thinking steps (from the stored `tool_call`s), answer, cards and thumbs.

---

## Write summary for one new message plus one Build

| Step | Writer | Tables written |
|---|---|---|
| New chat | API | `conversations` |
| User message | API | `conversation_events` (user text), `conversations.last_message_at`, `title` |
| Each tool node done | Runner | `conversation_events` (`tool_call`, `tool_result`), plus LangGraph checkpoint tables |
| Answer done | Runner | `conversation_events` (assistant text) |
| Artifact | Runner | `artifacts`, `artifact_versions`, `conversation_events` (`artifact`), a checkpoint with `active_artifact_id` |
| Build click | API, then runner | `artifact_versions` (`build_*`), `conversation_events` (`action`, `tool_call`, `tool_result`, `text`) |
| Thumbs | API | `conversation_event_feedback` |

Streamed tokens (about 3,800 per run) are never stored. The whole event log for one plan plus one build is only about 11 rows.

I can also show this as a diagram, or add a debug panel in the UI that displays each database write as it happens.