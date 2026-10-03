
## Running locally

```bash
docker compose up -d                              # Hindsight memory server (Postgres must also be running)
uv run uvicorn tripmate.app:app --port 8000       # API: POST /agent streams AG-UI events
npm --prefix web install && npm --prefix web run dev   # UI at http://localhost:5173 (React 17)
```
