BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(320) NOT NULL UNIQUE,
    display_name VARCHAR(120) NOT NULL,
    password_hash VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_users_email_lowercase CHECK (email = lower(email))
);

CREATE TABLE IF NOT EXISTS memberships (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    created_by VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_membership_workspace_user UNIQUE (workspace_id, user_id),
    CONSTRAINT ck_memberships_role CHECK (
        role IN ('owner', 'admin', 'editor', 'reviewer', 'publisher', 'auditor')
    )
);

CREATE INDEX IF NOT EXISTS ix_memberships_workspace_role
    ON memberships(workspace_id, role);

CREATE TABLE IF NOT EXISTS drafts (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    current_revision_id VARCHAR(36) NOT NULL,
    approved_revision_id VARCHAR(36),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_drafts_status CHECK (
        status IN ('draft', 'in_review', 'approved', 'rejected', 'published')
    )
);

CREATE INDEX IF NOT EXISTS ix_drafts_workspace_status
    ON drafts(workspace_id, status);

CREATE TABLE IF NOT EXISTS revisions (
    id VARCHAR(36) PRIMARY KEY,
    draft_id VARCHAR(36) NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    category VARCHAR(80) NOT NULL,
    publish_at TIMESTAMPTZ NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    image_sha256 VARCHAR(64),
    snapshot_hash VARCHAR(64) NOT NULL,
    created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_revision_draft_number UNIQUE (draft_id, revision_number),
    CONSTRAINT ck_revisions_image_sha256 CHECK (
        image_sha256 IS NULL OR image_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_revisions_snapshot_hash CHECK (snapshot_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS ix_revisions_draft_created
    ON revisions(draft_id, created_at);

-- Se agregan después para evitar un ciclo durante la creación inicial.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_drafts_current_revision'
    ) THEN
        ALTER TABLE drafts
            ADD CONSTRAINT fk_drafts_current_revision
            FOREIGN KEY (current_revision_id) REFERENCES revisions(id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_drafts_approved_revision'
    ) THEN
        ALTER TABLE drafts
            ADD CONSTRAINT fk_drafts_approved_revision
            FOREIGN KEY (approved_revision_id) REFERENCES revisions(id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS approvals (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    draft_id VARCHAR(36) NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    revision_id VARCHAR(36) NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    decision VARCHAR(20) NOT NULL,
    snapshot_hash VARCHAR(64) NOT NULL,
    actor_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_approvals_decision CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT ck_approvals_snapshot_hash CHECK (snapshot_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS ix_approvals_draft_created
    ON approvals(draft_id, created_at);

CREATE TABLE IF NOT EXISTS telegram_bindings (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    telegram_user_id VARCHAR(32) NOT NULL,
    chat_id VARCHAR(32) NOT NULL,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose VARCHAR(20) NOT NULL DEFAULT 'control',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(36) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_telegram_binding_identity_chat UNIQUE (
        workspace_id, telegram_user_id, chat_id
    ),
    CONSTRAINT ck_telegram_bindings_purpose CHECK (
        purpose IN ('control', 'review', 'alerts')
    )
);

CREATE INDEX IF NOT EXISTS ix_telegram_binding_identity
    ON telegram_bindings(workspace_id, telegram_user_id, is_active);

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id BIGINT PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    chat_id VARCHAR(32),
    telegram_user_id VARCHAR(32),
    actor_id VARCHAR(80) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'received',
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    error TEXT,
    CONSTRAINT ck_telegram_updates_status CHECK (
        status IN ('received', 'processed', 'failed')
    )
);

CREATE INDEX IF NOT EXISTS ix_telegram_updates_status_received
    ON telegram_updates(status, received_at);

CREATE TABLE IF NOT EXISTS callback_intents (
    id VARCHAR(36) PRIMARY KEY,
    nonce_hash VARCHAR(64) NOT NULL UNIQUE,
    workspace_id VARCHAR(80) NOT NULL,
    action VARCHAR(20) NOT NULL,
    draft_id VARCHAR(36) NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    revision_id VARCHAR(36) NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    snapshot_hash VARCHAR(64) NOT NULL,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_user_id VARCHAR(32) NOT NULL,
    chat_id VARCHAR(32) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    consumed_by VARCHAR(36),
    created_by VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_callback_intents_action CHECK (
        action IN ('approve', 'reject', 'publish')
    ),
    CONSTRAINT ck_callback_snapshot_hash CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_callback_nonce_hash CHECK (nonce_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS ix_callback_intents_expiry
    ON callback_intents(expires_at, consumed_at);
CREATE INDEX IF NOT EXISTS ix_callback_intents_draft
    ON callback_intents(draft_id, revision_id);

CREATE TABLE IF NOT EXISTS media_assets (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    draft_id VARCHAR(36) REFERENCES drafts(id) ON DELETE SET NULL,
    kind VARCHAR(40) NOT NULL,
    url TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    mime_type VARCHAR(120) NOT NULL,
    byte_size BIGINT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_media_workspace_sha256 UNIQUE (workspace_id, sha256),
    CONSTRAINT ck_media_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_media_byte_size CHECK (byte_size IS NULL OR byte_size >= 0)
);

CREATE INDEX IF NOT EXISTS ix_media_assets_draft ON media_assets(draft_id);

CREATE TABLE IF NOT EXISTS publish_attempts (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    draft_id VARCHAR(36) NOT NULL REFERENCES drafts(id) ON DELETE RESTRICT,
    revision_id VARCHAR(36) NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    requested_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    channel VARCHAR(30) NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    snapshot_hash VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    provider_post_id VARCHAR(100),
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_publish_workspace_channel_key UNIQUE (
        workspace_id, channel, idempotency_key
    ),
    CONSTRAINT ck_publish_attempts_status CHECK (
        status IN ('pending', 'succeeded', 'failed', 'unknown')
    ),
    CONSTRAINT ck_publish_snapshot_hash CHECK (snapshot_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS ix_publish_attempts_draft_started
    ON publish_attempts(draft_id, started_at);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    actor_id VARCHAR(80) NOT NULL,
    action VARCHAR(100) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id VARCHAR(100) NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_workspace_sequence
    ON audit_events(workspace_id, sequence);
CREATE INDEX IF NOT EXISTS ix_audit_entity
    ON audit_events(entity_type, entity_id);

COMMIT;
