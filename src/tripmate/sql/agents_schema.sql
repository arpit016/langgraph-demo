-- Peoplebox Agents AI chat schema, adapted for TripMate.
-- Source: peoplebox-ai/docs/AI_CHAT_SCHEMA.md
-- Deviations from the doc:
--   * artifacts.artifact_type allows 'trip_plan' (TripMate's artifact) instead of 'goal_draft'
--   * conversation_event_feedback UNIQUE uses conversation_event_id (the doc says event_id,
--     a column that does not exist)

CREATE SCHEMA IF NOT EXISTS agents;

CREATE TABLE IF NOT EXISTS agents.conversations (
  id               uuid        PRIMARY KEY,
  account_id       bigint      NOT NULL,
  employee_id      bigint      NOT NULL,
  title            text,
  title_source     text        NOT NULL DEFAULT 'auto',
  last_message_at  timestamptz NOT NULL DEFAULT now(),
  meta             jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  CONSTRAINT conversations_title_source_check CHECK (title_source IN ('auto', 'user'))
);

CREATE INDEX IF NOT EXISTS conversations_history
  ON agents.conversations (account_id, employee_id, last_message_at DESC)
  WHERE deleted_at IS NULL;

-- artifacts is created before conversation_events; its FKs to
-- artifact_versions / conversation_events are added below.
CREATE TABLE IF NOT EXISTS agents.artifacts (
  id                  uuid        PRIMARY KEY,
  account_id          bigint      NOT NULL,
  employee_id         bigint      NOT NULL,
  conversation_id     uuid        NOT NULL REFERENCES agents.conversations (id),
  artifact_type       text        NOT NULL,
  current_version_id  uuid,
  idempotency_key     text        NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT artifacts_idempotency_key_unique UNIQUE (account_id, idempotency_key),
  CONSTRAINT artifacts_type_check CHECK (artifact_type IN ('trip_plan'))
);

CREATE TABLE IF NOT EXISTS agents.conversation_events (
  id               uuid        PRIMARY KEY,
  account_id       bigint      NOT NULL,
  employee_id      bigint      NOT NULL,
  conversation_id  uuid        NOT NULL REFERENCES agents.conversations (id),
  artifact_id      uuid,
  reply_to_id      uuid        REFERENCES agents.conversation_events (id),
  parent_id        uuid        REFERENCES agents.conversation_events (id),
  seq              integer     NOT NULL,
  type             text        NOT NULL,
  role             text,
  content          text,
  data             jsonb,
  status           text        NOT NULL DEFAULT 'completed',
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT conversation_events_seq_unique UNIQUE (conversation_id, seq),
  CONSTRAINT conversation_events_type_check
    CHECK (type IN ('text', 'tool_call', 'tool_result', 'artifact', 'action', 'hitl_request', 'error')),
  CONSTRAINT conversation_events_role_check
    CHECK (role IN ('user', 'assistant', 'system', 'tool')),
  CONSTRAINT conversation_events_status_check
    CHECK (status IN ('streaming', 'completed', 'cancelled', 'failed'))
);

CREATE INDEX IF NOT EXISTS conversation_events_by_reply_to
  ON agents.conversation_events (reply_to_id);
CREATE INDEX IF NOT EXISTS conversation_events_by_parent
  ON agents.conversation_events (parent_id);
CREATE INDEX IF NOT EXISTS conversation_events_by_artifact
  ON agents.conversation_events (artifact_id)
  WHERE artifact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agents.artifact_versions (
  id                   uuid        PRIMARY KEY,
  account_id           bigint      NOT NULL,
  employee_id          bigint      NOT NULL,
  artifact_id          uuid        NOT NULL REFERENCES agents.artifacts (id),
  version_number       integer     NOT NULL,
  data                 jsonb       NOT NULL,
  created_by_event_id  uuid        NOT NULL REFERENCES agents.conversation_events (id),
  build_status         text,
  build_started_at     timestamptz,
  build_finished_at    timestamptz,
  build_results        jsonb,
  built_by             bigint,
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT artifact_versions_number_unique UNIQUE (artifact_id, version_number),
  CONSTRAINT artifact_versions_one_per_request UNIQUE (artifact_id, created_by_event_id),
  CONSTRAINT artifact_versions_build_status_check
    CHECK (build_status IN ('building', 'built', 'failed'))
);

DO $$ BEGIN
  ALTER TABLE agents.artifacts
    ADD CONSTRAINT artifacts_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES agents.artifact_versions (id)
    DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE agents.conversation_events
    ADD CONSTRAINT conversation_events_artifact_fk
    FOREIGN KEY (artifact_id) REFERENCES agents.artifacts (id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS artifacts_by_conversation
  ON agents.artifacts (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS agents.conversation_event_feedback (
  id                     uuid        PRIMARY KEY,
  account_id             bigint      NOT NULL,
  conversation_id        uuid        NOT NULL REFERENCES agents.conversations (id),
  conversation_event_id  uuid        NOT NULL REFERENCES agents.conversation_events (id),
  employee_id            bigint      NOT NULL,
  rating                 text        NOT NULL,
  comment                text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT conversation_event_feedback_one_per_rater UNIQUE (conversation_event_id, employee_id),
  CONSTRAINT conversation_event_feedback_rating_check CHECK (rating IN ('up', 'down'))
);

CREATE INDEX IF NOT EXISTS conversation_event_feedback_by_account
  ON agents.conversation_event_feedback (account_id, created_at);
