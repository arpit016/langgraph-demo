"""Database access for the `agents` schema (see docs/AI_CHAT_SCHEMA.md).

Every read and write is scoped by account_id + employee_id (from RequestContext).
"""

import uuid
from datetime import datetime

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from tripmate.db import pool
from tripmate.rails_stub import RequestContext

STALE_BUILD_MINUTES = 5


def new_id() -> uuid.UUID:
    return uuid.uuid7()


# ------------------------------------------------------------------ conversations

def create_conversation(ctx: RequestContext) -> dict:
    with pool.connection() as conn:
        return conn.execute(
            """INSERT INTO agents.conversations (id, account_id, employee_id)
               VALUES (%s, %s, %s) RETURNING *""",
            (new_id(), ctx.account_id, ctx.employee_id),
        ).fetchone()


def get_conversation(ctx: RequestContext, conversation_id) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT * FROM agents.conversations
               WHERE id = %s AND account_id = %s AND employee_id = %s AND deleted_at IS NULL""",
            (conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchone()


def list_conversations(ctx: RequestContext) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT id, title, title_source, last_message_at, created_at
               FROM agents.conversations
               WHERE account_id = %s AND employee_id = %s AND deleted_at IS NULL
               ORDER BY last_message_at DESC""",
            (ctx.account_id, ctx.employee_id),
        ).fetchall()


def rename_conversation(ctx: RequestContext, conversation_id, title: str) -> bool:
    with pool.connection() as conn:
        cur = conn.execute(
            """UPDATE agents.conversations
               SET title = %s, title_source = 'user', updated_at = now()
               WHERE id = %s AND account_id = %s AND employee_id = %s AND deleted_at IS NULL""",
            (title, conversation_id, ctx.account_id, ctx.employee_id),
        )
        return cur.rowcount == 1


def delete_conversation(ctx: RequestContext, conversation_id) -> bool:
    with pool.connection() as conn:
        cur = conn.execute(
            """UPDATE agents.conversations SET deleted_at = now(), updated_at = now()
               WHERE id = %s AND account_id = %s AND employee_id = %s AND deleted_at IS NULL""",
            (conversation_id, ctx.account_id, ctx.employee_id),
        )
        return cur.rowcount == 1


def auto_title(ctx: RequestContext, conversation_id, title: str) -> None:
    """Auto-titling only writes while title_source = 'auto' and the title is still empty."""
    with pool.connection() as conn:
        conn.execute(
            """UPDATE agents.conversations SET title = %s, updated_at = now()
               WHERE id = %s AND account_id = %s AND employee_id = %s
                 AND title_source = 'auto' AND title IS NULL""",
            (title[:60], conversation_id, ctx.account_id, ctx.employee_id),
        )


# ------------------------------------------------------------------ events

def _insert_event_once(conn, ctx, conversation_id, *, type, role, content, data,
                       reply_to_id, parent_id, artifact_id, status) -> dict:
    # Queue concurrent writers of the same conversation so they don't all read the same
    # max(seq) and collide. The UNIQUE(conversation_id, seq) constraint stays as the safety net.
    conn.execute("SELECT 1 FROM agents.conversations WHERE id = %s FOR UPDATE", (conversation_id,))
    seq = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 AS seq FROM agents.conversation_events WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()["seq"]
    event = conn.execute(
        """INSERT INTO agents.conversation_events
             (id, account_id, employee_id, conversation_id, artifact_id, reply_to_id,
              parent_id, seq, type, role, content, data, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (new_id(), ctx.account_id, ctx.employee_id, conversation_id, artifact_id,
         reply_to_id, parent_id, seq, type, role, content,
         Jsonb(data) if data is not None else None, status),
    ).fetchone()
    # The runner bumps last_message_at whenever it persists an event
    conn.execute(
        "UPDATE agents.conversations SET last_message_at = now(), updated_at = now() WHERE id = %s",
        (conversation_id,),
    )
    return event


def insert_event(ctx: RequestContext, conversation_id, *, type, role, content=None, data=None,
                 reply_to_id=None, parent_id=None, artifact_id=None, status="completed") -> dict:
    """Insert one event. seq = max(seq)+1; the UNIQUE(conversation_id, seq) catches a race
    and the insert is retried."""
    for attempt in range(5):
        try:
            with pool.connection() as conn:
                return _insert_event_once(
                    conn, ctx, conversation_id, type=type, role=role, content=content,
                    data=data, reply_to_id=reply_to_id, parent_id=parent_id,
                    artifact_id=artifact_id, status=status,
                )
        except UniqueViolation:
            if attempt == 4:
                raise
    raise RuntimeError("unreachable")


def load_events(ctx: RequestContext, conversation_id) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT * FROM agents.conversation_events
               WHERE conversation_id = %s AND account_id = %s AND employee_id = %s
               ORDER BY seq""",
            (conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchall()


# ------------------------------------------------------------------ artifacts

def latest_artifact(ctx: RequestContext, conversation_id) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT id, current_version_id FROM agents.artifacts
               WHERE conversation_id = %s AND account_id = %s AND employee_id = %s
               ORDER BY created_at DESC LIMIT 1""",
            (conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchone()


def create_artifact(ctx: RequestContext, conversation_id, root_event_id, ordinal: int,
                    artifact_type: str, data: dict) -> dict:
    """Artifact + v1 in one transaction. On an idempotency-key conflict (a re-run step)
    the existing artifact is returned."""
    key = f"{conversation_id}:{root_event_id}:{ordinal}"
    with pool.connection() as conn:
        existing = conn.execute(
            """SELECT a.id AS artifact_id, a.current_version_id AS version_id, v.version_number
               FROM agents.artifacts a
               JOIN agents.artifact_versions v ON v.id = a.current_version_id
               WHERE a.account_id = %s AND a.idempotency_key = %s""",
            (ctx.account_id, key),
        ).fetchone()
        if existing:
            return existing
        artifact_id, version_id = new_id(), new_id()
        conn.execute(
            """INSERT INTO agents.artifacts
                 (id, account_id, employee_id, conversation_id, artifact_type, idempotency_key)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (artifact_id, ctx.account_id, ctx.employee_id, conversation_id, artifact_type, key),
        )
        conn.execute(
            """INSERT INTO agents.artifact_versions
                 (id, account_id, employee_id, artifact_id, version_number, data, created_by_event_id)
               VALUES (%s,%s,%s,%s,1,%s,%s)""",
            (version_id, ctx.account_id, ctx.employee_id, artifact_id, Jsonb(data), root_event_id),
        )
        conn.execute(
            "UPDATE agents.artifacts SET current_version_id = %s, updated_at = now() WHERE id = %s",
            (version_id, artifact_id),
        )
        return {"artifact_id": artifact_id, "version_id": version_id, "version_number": 1}


class StaleRevision(Exception):
    """Another revision moved the current-version pointer first."""


def revise_artifact(ctx: RequestContext, artifact_id, seen_version_id, request_event_id,
                    data: dict) -> dict:
    with pool.connection() as conn:
        # 1. lock the scoped parent artifact row to serialise revisions
        locked = conn.execute(
            """SELECT id FROM agents.artifacts
               WHERE id = %s AND account_id = %s AND employee_id = %s FOR UPDATE""",
            (artifact_id, ctx.account_id, ctx.employee_id),
        ).fetchone()
        if not locked:
            raise LookupError("artifact not found")
        # a re-run step returns the version it already made for this request
        again = conn.execute(
            """SELECT id AS version_id, version_number FROM agents.artifact_versions
               WHERE artifact_id = %s AND created_by_event_id = %s""",
            (artifact_id, request_event_id),
        ).fetchone()
        if again:
            return {"artifact_id": artifact_id, **again}
        # 2. insert the next version and move the pointer only if it still equals what we saw
        number = conn.execute(
            "SELECT coalesce(max(version_number), 0) + 1 AS n FROM agents.artifact_versions WHERE artifact_id = %s",
            (artifact_id,),
        ).fetchone()["n"]
        version_id = new_id()
        conn.execute(
            """INSERT INTO agents.artifact_versions
                 (id, account_id, employee_id, artifact_id, version_number, data, created_by_event_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (version_id, ctx.account_id, ctx.employee_id, artifact_id, number, Jsonb(data), request_event_id),
        )
        cur = conn.execute(
            """UPDATE agents.artifacts SET current_version_id = %s, updated_at = now()
               WHERE id = %s AND account_id = %s AND employee_id = %s AND current_version_id = %s""",
            (version_id, artifact_id, ctx.account_id, ctx.employee_id, seen_version_id),
        )
        # 3. 0 rows: another revision won, roll back
        if cur.rowcount == 0:
            conn.rollback()
            raise StaleRevision()
        return {"artifact_id": artifact_id, "version_id": version_id, "version_number": number}


def artifacts_for_conversation(ctx: RequestContext, conversation_id) -> list[dict]:
    with pool.connection() as conn:
        arts = conn.execute(
            """SELECT * FROM agents.artifacts
               WHERE conversation_id = %s AND account_id = %s AND employee_id = %s
               ORDER BY created_at""",
            (conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchall()
        versions = conn.execute(
            """SELECT v.* FROM agents.artifact_versions v
               JOIN agents.artifacts a ON a.id = v.artifact_id
               WHERE a.conversation_id = %s AND v.account_id = %s AND v.employee_id = %s
               ORDER BY v.artifact_id, v.version_number""",
            (conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchall()
    for a in arts:
        a["versions"] = [v for v in versions if v["artifact_id"] == a["id"]]
    return arts


def get_version(ctx: RequestContext, version_id) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT * FROM agents.artifact_versions
               WHERE id = %s AND account_id = %s AND employee_id = %s""",
            (version_id, ctx.account_id, ctx.employee_id),
        ).fetchone()


# ------------------------------------------------------------------ build state

def claim_build(ctx: RequestContext, conversation_id, artifact_id, version_id) -> dict:
    """Claim exactly the clicked version for a build.

    Returns {"outcome": "claimed" | "built" | "building" | "missing", "version": row}.
    """
    with pool.connection() as conn:
        v = conn.execute(
            """SELECT v.* FROM agents.artifact_versions v
               JOIN agents.artifacts a ON a.id = v.artifact_id
               WHERE v.id = %s AND v.artifact_id = %s AND a.conversation_id = %s
                 AND v.account_id = %s AND v.employee_id = %s
               FOR UPDATE OF v""",
            (version_id, artifact_id, conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchone()
        if not v:
            return {"outcome": "missing", "version": None}
        if v["build_status"] == "built":
            return {"outcome": "built", "version": v}
        if v["build_status"] == "building":
            stale = conn.execute(
                "SELECT %s < now() - make_interval(mins => %s) AS stale",
                (v["build_started_at"], STALE_BUILD_MINUTES),
            ).fetchone()["stale"]
            if not stale:
                return {"outcome": "building", "version": v}
        claimed = conn.execute(
            """UPDATE agents.artifact_versions
               SET build_status = 'building', build_started_at = now(), built_by = %s,
                   build_finished_at = NULL, build_results = NULL
               WHERE id = %s RETURNING *""",
            (ctx.employee_id, version_id),
        ).fetchone()
        return {"outcome": "claimed", "version": claimed}


def finish_build(ctx: RequestContext, version_id, started_at: datetime, *, status: str,
                 results: dict) -> bool:
    """Complete a claimed attempt; matches the claim's build_started_at so an older worker
    can't overwrite a newer claim."""
    with pool.connection() as conn:
        cur = conn.execute(
            """UPDATE agents.artifact_versions
               SET build_status = %s, build_finished_at = now(), build_results = %s
               WHERE id = %s AND account_id = %s AND employee_id = %s
                 AND build_status = 'building' AND build_started_at = %s""",
            (status, Jsonb(results), version_id, ctx.account_id, ctx.employee_id, started_at),
        )
        return cur.rowcount == 1


# ------------------------------------------------------------------ feedback

def upsert_feedback(ctx: RequestContext, conversation_id, event_id, rating: str,
                    comment: str | None) -> dict | None:
    with pool.connection() as conn:
        ev = conn.execute(
            """SELECT id FROM agents.conversation_events
               WHERE id = %s AND conversation_id = %s AND account_id = %s AND employee_id = %s""",
            (event_id, conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchone()
        if not ev:
            return None
        return conn.execute(
            """INSERT INTO agents.conversation_event_feedback
                 (id, account_id, conversation_id, conversation_event_id, employee_id, rating, comment)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (conversation_event_id, employee_id)
               DO UPDATE SET rating = EXCLUDED.rating, comment = EXCLUDED.comment, updated_at = now()
               RETURNING *""",
            (new_id(), ctx.account_id, conversation_id, event_id, ctx.employee_id, rating, comment),
        ).fetchone()


def feedback_for_conversation(ctx: RequestContext, conversation_id) -> dict:
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT conversation_event_id, rating FROM agents.conversation_event_feedback
               WHERE conversation_id = %s AND account_id = %s AND employee_id = %s""",
            (conversation_id, ctx.account_id, ctx.employee_id),
        ).fetchall()
    return {str(r["conversation_event_id"]): r["rating"] for r in rows}
