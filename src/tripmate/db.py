import os
from pathlib import Path

from dotenv import load_dotenv
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

SCHEMA_SQL = Path(__file__).parent / "sql" / "agents_schema.sql"

# Shared pool for the `agents` schema tables (conversations, events, artifacts, ...)
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=True,
)


def apply_schema() -> None:
    """Create the agents.* tables (idempotent)."""
    with pool.connection() as conn:
        conn.execute(SCHEMA_SQL.read_text())


def setup() -> None:
    """Create the checkpointer tables (short-term memory) and the agents.* tables."""
    with PostgresSaver.from_conn_string(DATABASE_URL) as checkpointer:
        checkpointer.setup()
    apply_schema()
