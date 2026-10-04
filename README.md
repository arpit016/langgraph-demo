
## Running locally

```bash
docker compose up -d                              # Hindsight memory server (Postgres must also be running)
uv run uvicorn tripmate.app:app --port 8000       # API: POST /agent streams AG-UI events
npm --prefix web install && npm --prefix web run dev   # UI at http://localhost:5173 (React 17)
```

## Chat persistence (`agents` schema)

Tables follow `peoplebox-ai/docs/AI_CHAT_SCHEMA.md` (conversations, conversation_events, artifacts,
artifact_versions, conversation_event_feedback); DDL is in `src/tripmate/sql/agents_schema.sql` and is applied
on API startup. Accounts/employees and the Build endpoint are faked in `src/tripmate/rails_stub.py`
(the UI's "Signed in as" picker sends `X-Employee-Id`). Adaptation: the artifact type is `trip_plan`.
