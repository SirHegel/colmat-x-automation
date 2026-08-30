BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(320) NOT NULL UNIQUE,
    username VARCHAR(64),
    display_name VARCHAR(120) NOT NULL,
    password_hash VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_users_email_lowercase CHECK (email = lower(email))
);

-- Compatibilidad con instalaciones creadas antes de incorporar username.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username
    ON users(username) WHERE username IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_users_username_lowercase'
          AND conrelid = 'users'::regclass
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT ck_users_username_lowercase
            CHECK (username IS NULL OR username = lower(username));
    END IF;
END
$$;

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
        role IN ('owner', 'admin', 'editor', 'reviewer', 'publisher', 'scheduler', 'auditor')
    )
);

-- Reemplaza de forma repetible el check antiguo que no conocía scheduler.
ALTER TABLE memberships DROP CONSTRAINT IF EXISTS ck_memberships_role;
ALTER TABLE memberships
    ADD CONSTRAINT ck_memberships_role CHECK (
        role IN ('owner', 'admin', 'editor', 'reviewer', 'publisher', 'scheduler', 'auditor')
    );

CREATE INDEX IF NOT EXISTS ix_memberships_workspace_role
    ON memberships(workspace_id, role);

CREATE TABLE IF NOT EXISTS automation_settings (
    workspace_id VARCHAR(80) PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    mode VARCHAR(20) NOT NULL DEFAULT 'human_review',
    timezone VARCHAR(64) NOT NULL DEFAULT 'America/Bogota',
    slots JSONB NOT NULL DEFAULT '[]'::jsonb,
    generate_images BOOLEAN NOT NULL DEFAULT FALSE,
    min_engagement_score INTEGER NOT NULL DEFAULT 0,
    max_posts_per_day INTEGER NOT NULL DEFAULT 2,
    version INTEGER NOT NULL DEFAULT 1,
    direct_authorized_by VARCHAR(36) REFERENCES users(id) ON DELETE RESTRICT,
    direct_authorized_at TIMESTAMPTZ,
    updated_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_automation_settings_mode CHECK (
        mode IN ('human_review', 'direct')
    ),
    CONSTRAINT ck_automation_settings_slots CHECK (jsonb_typeof(slots) = 'array'),
    CONSTRAINT ck_automation_settings_engagement CHECK (
        min_engagement_score BETWEEN 0 AND 100
    ),
    CONSTRAINT ck_automation_settings_daily_limit CHECK (
        max_posts_per_day BETWEEN 1 AND 100
    ),
    CONSTRAINT ck_automation_settings_version CHECK (version >= 1),
    CONSTRAINT ck_automation_settings_direct_authorization CHECK (
        (
            mode = 'direct'
            AND direct_authorized_by IS NOT NULL
            AND direct_authorized_at IS NOT NULL
        ) OR (
            mode = 'human_review'
            AND direct_authorized_by IS NULL
            AND direct_authorized_at IS NULL
        )
    )
);

-- Corrige instalaciones donde SQLAlchemy alcanzó a crear `slots` como JSON,
-- sin repetir una conversión ni tomar su lock cuando ya es JSONB.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'automation_settings'
          AND column_name = 'slots'
          AND udt_name <> 'jsonb'
    ) THEN
        ALTER TABLE automation_settings
            ALTER COLUMN slots TYPE JSONB USING slots::jsonb;
    END IF;
END
$$;

-- Las agendas legacy no ligaban la verificación a una cifra y fuente concretas.
-- Solo las filas incompatibles se degradan una vez: evidencia no verificada,
-- slots human_review, agenda pausada, autorización direct revocada y versión nueva.
WITH legacy_settings AS (
    SELECT settings.workspace_id
    FROM automation_settings AS settings
    WHERE jsonb_typeof(settings.slots) = 'array'
      AND EXISTS (
          SELECT 1
          FROM jsonb_array_elements(settings.slots) AS entry(item)
          WHERE CASE
              WHEN jsonb_typeof(item -> 'evidence') IS DISTINCT FROM 'object' THEN TRUE
              ELSE
                  (
                      SELECT count(*)
                      FROM jsonb_object_keys(item -> 'evidence')
                  ) <> 4
                  OR NOT ((item -> 'evidence') ?& ARRAY[
                      'verified', 'reference', 'expected_figure', 'expected_source'
                  ])
                  OR jsonb_typeof(item -> 'evidence' -> 'verified')
                      IS DISTINCT FROM 'boolean'
                  OR NOT (
                      (item -> 'evidence' -> 'reference') = 'null'::jsonb
                      OR jsonb_typeof(item -> 'evidence' -> 'reference') = 'string'
                  )
                  OR NOT (
                      (
                          (item -> 'evidence' -> 'expected_figure') = 'null'::jsonb
                          AND (item -> 'evidence' -> 'expected_source') = 'null'::jsonb
                      ) OR (
                          jsonb_typeof(item -> 'evidence' -> 'expected_figure') = 'string'
                          AND jsonb_typeof(item -> 'evidence' -> 'expected_source') = 'string'
                      )
                  )
                  OR (
                      (item -> 'evidence' -> 'verified') = 'true'::jsonb
                      AND (
                          jsonb_typeof(item -> 'evidence' -> 'reference')
                              IS DISTINCT FROM 'string'
                          OR jsonb_typeof(item -> 'evidence' -> 'expected_figure')
                              IS DISTINCT FROM 'string'
                          OR jsonb_typeof(item -> 'evidence' -> 'expected_source')
                              IS DISTINCT FROM 'string'
                      )
                  )
          END
      )
), rebuilt_slots AS (
    SELECT
        settings.workspace_id,
        COALESCE(
            jsonb_agg(
                jsonb_set(
                    jsonb_set(
                        item,
                        '{evidence}',
                        jsonb_build_object(
                            'verified', FALSE,
                            'reference', NULL,
                            'expected_figure', NULL,
                            'expected_source', NULL
                        ),
                        TRUE
                    ),
                    '{mode}',
                    '"human_review"'::jsonb,
                    TRUE
                )
                ORDER BY ordinal
            ) FILTER (WHERE jsonb_typeof(item) = 'object'),
            '[]'::jsonb
        ) AS slots
    FROM automation_settings AS settings
    JOIN legacy_settings USING (workspace_id)
    CROSS JOIN LATERAL jsonb_array_elements(settings.slots)
        WITH ORDINALITY AS entry(item, ordinal)
    GROUP BY settings.workspace_id
)
UPDATE automation_settings AS settings
SET
    slots = rebuilt.slots,
    enabled = FALSE,
    mode = 'human_review',
    version = settings.version + 1,
    direct_authorized_by = NULL,
    direct_authorized_at = NULL,
    updated_at = CURRENT_TIMESTAMP
FROM rebuilt_slots AS rebuilt
WHERE settings.workspace_id = rebuilt.workspace_id;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_automation_settings_slots'
          AND conrelid = 'automation_settings'::regclass
    ) THEN
        ALTER TABLE automation_settings
            ADD CONSTRAINT ck_automation_settings_slots
            CHECK (jsonb_typeof(slots) = 'array');
    END IF;
END
$$;

-- Backfill seguro: bases previas ya tienen workspaces implícitos en memberships.
INSERT INTO automation_settings (
    workspace_id,
    enabled,
    mode,
    timezone,
    slots,
    generate_images,
    min_engagement_score,
    max_posts_per_day,
    version,
    updated_by,
    updated_at
)
SELECT DISTINCT ON (workspace_id)
    workspace_id,
    FALSE,
    'human_review',
    'America/Bogota',
    '[]'::jsonb,
    FALSE,
    0,
    2,
    1,
    user_id,
    CURRENT_TIMESTAMP
FROM memberships
ORDER BY
    workspace_id,
    CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
    created_at,
    id
ON CONFLICT (workspace_id) DO NOTHING;

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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_revisions_image_sha256'
          AND conrelid = 'revisions'::regclass
    ) THEN
        ALTER TABLE revisions ADD CONSTRAINT ck_revisions_image_sha256
            CHECK (image_sha256 IS NULL OR image_sha256 ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_revisions_snapshot_hash'
          AND conrelid = 'revisions'::regclass
    ) THEN
        ALTER TABLE revisions ADD CONSTRAINT ck_revisions_snapshot_hash
            CHECK (snapshot_hash ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
END $$;
ALTER TABLE revisions VALIDATE CONSTRAINT ck_revisions_image_sha256;
ALTER TABLE revisions VALIDATE CONSTRAINT ck_revisions_snapshot_hash;

CREATE INDEX IF NOT EXISTS ix_revisions_draft_created
    ON revisions(draft_id, created_at);

-- Se agregan después para evitar un ciclo durante la creación inicial.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_drafts_current_revision'
          AND conrelid = 'drafts'::regclass
    ) THEN
        ALTER TABLE drafts
            ADD CONSTRAINT fk_drafts_current_revision
            FOREIGN KEY (current_revision_id) REFERENCES revisions(id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_drafts_approved_revision'
          AND conrelid = 'drafts'::regclass
    ) THEN
        ALTER TABLE drafts
            ADD CONSTRAINT fk_drafts_approved_revision
            FOREIGN KEY (approved_revision_id) REFERENCES revisions(id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS automation_runs (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    slot_id VARCHAR(80) NOT NULL,
    scheduled_for TIMESTAMPTZ NOT NULL,
    mode VARCHAR(20) NOT NULL,
    settings_version INTEGER NOT NULL,
    slot_hash VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'claimed',
    draft_id VARCHAR(36) REFERENCES drafts(id) ON DELETE SET NULL,
    error TEXT,
    claimed_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    finished_by VARCHAR(36) REFERENCES users(id) ON DELETE RESTRICT,
    claimed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_automation_run_workspace_key UNIQUE (workspace_id, idempotency_key),
    CONSTRAINT ck_automation_runs_mode CHECK (mode IN ('human_review', 'direct')),
    CONSTRAINT ck_automation_runs_settings_version CHECK (settings_version >= 1),
    CONSTRAINT ck_automation_runs_slot_hash CHECK (slot_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_automation_runs_status CHECK (
        status IN (
            'claimed', 'awaiting_review', 'ready', 'publishing',
            'succeeded', 'failed', 'unknown'
        )
    ),
    CONSTRAINT ck_automation_runs_error_length CHECK (
        error IS NULL OR char_length(error) <= 1000
    ),
    CONSTRAINT ck_automation_runs_finished CHECK (
        (status IN ('succeeded', 'failed', 'unknown') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed', 'unknown'))
    )
);

ALTER TABLE automation_runs
    ADD COLUMN IF NOT EXISTS settings_version INTEGER;
ALTER TABLE automation_runs
    ADD COLUMN IF NOT EXISTS slot_hash VARCHAR(64);
UPDATE automation_runs AS run
SET settings_version = COALESCE(
    run.settings_version,
    (
        SELECT settings.version
        FROM automation_settings AS settings
        WHERE settings.workspace_id = run.workspace_id
    ),
    1
)
WHERE run.settings_version IS NULL;
UPDATE automation_runs
SET slot_hash =
    md5(concat_ws('|', workspace_id, idempotency_key, slot_id, scheduled_for::text, mode))
    || md5(concat_ws('|', 'legacy', workspace_id, idempotency_key, slot_id))
WHERE slot_hash IS NULL;
ALTER TABLE automation_runs
    ALTER COLUMN settings_version SET NOT NULL;
ALTER TABLE automation_runs
    ALTER COLUMN slot_hash SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_automation_runs_settings_version'
          AND conrelid = 'automation_runs'::regclass
    ) THEN
        ALTER TABLE automation_runs
            ADD CONSTRAINT ck_automation_runs_settings_version
            CHECK (settings_version >= 1);
    END IF;
END
$$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_automation_runs_slot_hash'
          AND conrelid = 'automation_runs'::regclass
    ) THEN
        ALTER TABLE automation_runs
            ADD CONSTRAINT ck_automation_runs_slot_hash
            CHECK (slot_hash ~ '^[0-9a-f]{64}$');
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_automation_runs_workspace_schedule
    ON automation_runs(workspace_id, scheduled_for, status);
CREATE INDEX IF NOT EXISTS ix_automation_runs_draft
    ON automation_runs(draft_id);

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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_approvals_snapshot_hash'
          AND conrelid = 'approvals'::regclass
    ) THEN
        ALTER TABLE approvals ADD CONSTRAINT ck_approvals_snapshot_hash
            CHECK (snapshot_hash ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
END $$;
ALTER TABLE approvals VALIDATE CONSTRAINT ck_approvals_snapshot_hash;

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
    claim_token_hash VARCHAR(64),
    claim_fence INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    prepared_actions JSONB,
    business_result JSONB,
    CONSTRAINT ck_telegram_updates_status CHECK (
        status IN ('received', 'processed', 'failed')
    ),
    CONSTRAINT ck_telegram_updates_claim_fence CHECK (claim_fence >= 0),
    CONSTRAINT ck_telegram_updates_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_telegram_updates_claim_token_hash_length CHECK (
        claim_token_hash IS NULL OR length(claim_token_hash) = 64
    )
);

ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS claim_token_hash VARCHAR(64);
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS claim_fence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS prepared_actions JSONB;
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS business_result JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_telegram_updates_claim_fence'
          AND conrelid = 'telegram_updates'::regclass
    ) THEN
        ALTER TABLE telegram_updates ADD CONSTRAINT ck_telegram_updates_claim_fence
            CHECK (claim_fence >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_telegram_updates_attempt_count'
          AND conrelid = 'telegram_updates'::regclass
    ) THEN
        ALTER TABLE telegram_updates ADD CONSTRAINT ck_telegram_updates_attempt_count
            CHECK (attempt_count >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_telegram_updates_claim_token_hash_length'
          AND conrelid = 'telegram_updates'::regclass
    ) THEN
        ALTER TABLE telegram_updates
            ADD CONSTRAINT ck_telegram_updates_claim_token_hash_length
            CHECK (claim_token_hash IS NULL OR length(claim_token_hash) = 64) NOT VALID;
    END IF;
END $$;

ALTER TABLE telegram_updates VALIDATE CONSTRAINT ck_telegram_updates_claim_fence;
ALTER TABLE telegram_updates VALIDATE CONSTRAINT ck_telegram_updates_attempt_count;
ALTER TABLE telegram_updates VALIDATE CONSTRAINT ck_telegram_updates_claim_token_hash_length;

CREATE INDEX IF NOT EXISTS ix_telegram_updates_status_received
    ON telegram_updates(status, received_at);

CREATE INDEX IF NOT EXISTS ix_telegram_updates_lease
    ON telegram_updates(status, lease_expires_at);

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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_callback_snapshot_hash'
          AND conrelid = 'callback_intents'::regclass
    ) THEN
        ALTER TABLE callback_intents ADD CONSTRAINT ck_callback_snapshot_hash
            CHECK (snapshot_hash ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_callback_nonce_hash'
          AND conrelid = 'callback_intents'::regclass
    ) THEN
        ALTER TABLE callback_intents ADD CONSTRAINT ck_callback_nonce_hash
            CHECK (nonce_hash ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
END $$;
ALTER TABLE callback_intents VALIDATE CONSTRAINT ck_callback_snapshot_hash;
ALTER TABLE callback_intents VALIDATE CONSTRAINT ck_callback_nonce_hash;

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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_media_sha256'
          AND conrelid = 'media_assets'::regclass
    ) THEN
        ALTER TABLE media_assets ADD CONSTRAINT ck_media_sha256
            CHECK (sha256 ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_media_byte_size'
          AND conrelid = 'media_assets'::regclass
    ) THEN
        ALTER TABLE media_assets ADD CONSTRAINT ck_media_byte_size
            CHECK (byte_size IS NULL OR byte_size >= 0) NOT VALID;
    END IF;
END $$;
ALTER TABLE media_assets VALIDATE CONSTRAINT ck_media_sha256;
ALTER TABLE media_assets VALIDATE CONSTRAINT ck_media_byte_size;

CREATE INDEX IF NOT EXISTS ix_media_assets_draft ON media_assets(draft_id);

CREATE TABLE IF NOT EXISTS automation_review_notifications (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    automation_run_id VARCHAR(36) NOT NULL
        REFERENCES automation_runs(id) ON DELETE RESTRICT,
    draft_id VARCHAR(36) NOT NULL REFERENCES drafts(id) ON DELETE RESTRICT,
    revision_id VARCHAR(36) NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    snapshot_hash VARCHAR(64) NOT NULL,
    telegram_user_id VARCHAR(32) NOT NULL,
    chat_id VARCHAR(32) NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL,
    engagement_score INTEGER NOT NULL,
    media_sha256 VARCHAR(64),
    approve_intent_id VARCHAR(36)
        REFERENCES callback_intents(id) ON DELETE RESTRICT,
    reject_intent_id VARCHAR(36)
        REFERENCES callback_intents(id) ON DELETE RESTRICT,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    claim_token_hash VARCHAR(64),
    claim_fence INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(36) REFERENCES users(id) ON DELETE RESTRICT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    photo_message_id BIGINT,
    review_message_id BIGINT,
    photo_sent_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_automation_review_notification_run UNIQUE (automation_run_id),
    CONSTRAINT uq_automation_review_notification_claim_token_hash UNIQUE (claim_token_hash),
    CONSTRAINT ck_automation_review_notifications_status CHECK (
        status IN ('queued', 'claimed', 'sent', 'failed', 'unknown')
    ),
    CONSTRAINT ck_automation_review_notifications_snapshot_hash_length CHECK (
        snapshot_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_automation_review_notifications_media_sha256_length CHECK (
        media_sha256 IS NULL OR media_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_automation_review_notifications_engagement CHECK (
        engagement_score BETWEEN 0 AND 100
    ),
    CONSTRAINT ck_automation_review_notifications_claim_token_hash_length CHECK (
        claim_token_hash IS NULL OR claim_token_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_automation_review_notifications_claim_fence CHECK (claim_fence >= 0),
    CONSTRAINT ck_automation_review_notifications_claim_state CHECK (
        (
            status = 'queued'
            AND claim_fence = 0
            AND claim_token_hash IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
            AND lease_expires_at IS NULL
        ) OR (
            status <> 'queued'
            AND claim_fence >= 1
            AND claim_token_hash IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_automation_review_notifications_lease CHECK (
        lease_expires_at IS NULL OR lease_expires_at > claimed_at
    ),
    CONSTRAINT ck_automation_review_notifications_finished CHECK (
        (status IN ('queued', 'claimed') AND finished_at IS NULL)
        OR (status IN ('sent', 'failed', 'unknown') AND finished_at IS NOT NULL)
    ),
    CONSTRAINT ck_automation_review_notifications_callback_pair CHECK (
        (approve_intent_id IS NULL AND reject_intent_id IS NULL)
        OR (approve_intent_id IS NOT NULL AND reject_intent_id IS NOT NULL)
    ),
    CONSTRAINT ck_automation_review_notifications_photo_pair CHECK (
        (photo_message_id IS NULL AND photo_sent_at IS NULL)
        OR (photo_message_id IS NOT NULL AND photo_sent_at IS NOT NULL)
    ),
    CONSTRAINT ck_automation_review_notifications_sent_result CHECK (
        status <> 'sent'
        OR (
            review_message_id IS NOT NULL
            AND sent_at IS NOT NULL
            AND approve_intent_id IS NOT NULL
            AND reject_intent_id IS NOT NULL
        )
    ),
    CONSTRAINT ck_automation_review_notifications_error_state CHECK (
        (status IN ('failed', 'unknown') AND error IS NOT NULL)
        OR (status NOT IN ('failed', 'unknown') AND error IS NULL)
    ),
    CONSTRAINT ck_automation_review_notifications_error_length CHECK (
        error IS NULL OR char_length(error) <= 1000
    ),
    CONSTRAINT ck_automation_review_notifications_detail_length CHECK (
        char_length(detail) BETWEEN 1 AND 1000
    )
);

CREATE INDEX IF NOT EXISTS ix_automation_review_notifications_queue
    ON automation_review_notifications(workspace_id, status, created_at);
CREATE INDEX IF NOT EXISTS ix_automation_review_notifications_lease
    ON automation_review_notifications(workspace_id, status, lease_expires_at);

CREATE TABLE IF NOT EXISTS generation_requests (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    brief TEXT NOT NULL,
    category VARCHAR(80),
    institution VARCHAR(80),
    generate_image BOOLEAN NOT NULL DEFAULT TRUE,
    requested_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    telegram_user_id VARCHAR(32) NOT NULL,
    chat_id VARCHAR(32) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    claim_token_hash VARCHAR(64),
    claim_fence INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(36) REFERENCES users(id) ON DELETE RESTRICT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    draft_id VARCHAR(36) REFERENCES drafts(id) ON DELETE RESTRICT,
    revision_id VARCHAR(36) REFERENCES revisions(id) ON DELETE RESTRICT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_generation_request_workspace_key UNIQUE (
        workspace_id, idempotency_key
    ),
    CONSTRAINT uq_generation_request_claim_token_hash UNIQUE (claim_token_hash),
    CONSTRAINT ck_generation_requests_status CHECK (
        status IN ('queued', 'claimed', 'succeeded', 'failed', 'unknown')
    ),
    CONSTRAINT ck_generation_requests_brief_length CHECK (
        char_length(brief) BETWEEN 10 AND 1000
    ),
    CONSTRAINT ck_generation_requests_category CHECK (
        category IS NULL OR category IN (
            'dato_semana', 'ficha_territorio', 'lamina', 'correccion_publica'
        )
    ),
    CONSTRAINT ck_generation_requests_institution CHECK (
        institution IS NULL OR institution IN (
            'colmat', 'escuela_colombiana_de_filosofia', 'tierra_firme'
        )
    ),
    CONSTRAINT ck_generation_requests_claim_token_hash_length CHECK (
        claim_token_hash IS NULL OR claim_token_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_generation_requests_claim_fence CHECK (claim_fence >= 0),
    CONSTRAINT ck_generation_requests_claim_state CHECK (
        (
            status = 'queued'
            AND claim_fence = 0
            AND claim_token_hash IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
            AND lease_expires_at IS NULL
        ) OR (
            status <> 'queued'
            AND claim_fence >= 1
            AND claim_token_hash IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_generation_requests_lease CHECK (
        lease_expires_at IS NULL OR lease_expires_at > claimed_at
    ),
    CONSTRAINT ck_generation_requests_finished CHECK (
        (status IN ('queued', 'claimed') AND finished_at IS NULL)
        OR (status IN ('succeeded', 'failed', 'unknown') AND finished_at IS NOT NULL)
    ),
    CONSTRAINT ck_generation_requests_result CHECK (
        (status = 'succeeded' AND draft_id IS NOT NULL AND revision_id IS NOT NULL)
        OR (status <> 'succeeded' AND draft_id IS NULL AND revision_id IS NULL)
    ),
    CONSTRAINT ck_generation_requests_error_length CHECK (
        error IS NULL OR char_length(error) <= 1000
    )
);

CREATE INDEX IF NOT EXISTS ix_generation_requests_queue
    ON generation_requests(workspace_id, status, created_at);
CREATE INDEX IF NOT EXISTS ix_generation_requests_lease
    ON generation_requests(workspace_id, status, lease_expires_at);

CREATE TABLE IF NOT EXISTS generation_notifications (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    generation_request_id VARCHAR(36) NOT NULL
        REFERENCES generation_requests(id) ON DELETE CASCADE,
    draft_id VARCHAR(36) NOT NULL REFERENCES drafts(id) ON DELETE RESTRICT,
    revision_id VARCHAR(36) NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    snapshot_hash VARCHAR(64) NOT NULL,
    telegram_user_id VARCHAR(32) NOT NULL,
    chat_id VARCHAR(32) NOT NULL,
    text TEXT NOT NULL,
    engagement_score INTEGER NOT NULL,
    media_sha256 VARCHAR(64),
    approve_intent_id VARCHAR(36)
        REFERENCES callback_intents(id) ON DELETE RESTRICT,
    reject_intent_id VARCHAR(36)
        REFERENCES callback_intents(id) ON DELETE RESTRICT,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    claim_token_hash VARCHAR(64),
    claim_fence INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(36) REFERENCES users(id) ON DELETE RESTRICT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    photo_message_id BIGINT,
    review_message_id BIGINT,
    photo_sent_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_generation_notification_request UNIQUE (generation_request_id),
    CONSTRAINT uq_generation_notification_claim_token_hash UNIQUE (claim_token_hash),
    CONSTRAINT ck_generation_notifications_status CHECK (
        status IN ('queued', 'claimed', 'sent', 'failed', 'unknown')
    ),
    CONSTRAINT ck_generation_notifications_snapshot_hash_length CHECK (
        snapshot_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_generation_notifications_media_sha256_length CHECK (
        media_sha256 IS NULL OR media_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_generation_notifications_engagement CHECK (
        engagement_score BETWEEN 0 AND 100
    ),
    CONSTRAINT ck_generation_notifications_claim_token_hash_length CHECK (
        claim_token_hash IS NULL OR claim_token_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_generation_notifications_claim_fence CHECK (claim_fence >= 0),
    CONSTRAINT ck_generation_notifications_claim_state CHECK (
        (
            status = 'queued'
            AND claim_fence = 0
            AND claim_token_hash IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
            AND lease_expires_at IS NULL
        ) OR (
            status <> 'queued'
            AND claim_fence >= 1
            AND claim_token_hash IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_generation_notifications_lease CHECK (
        lease_expires_at IS NULL OR lease_expires_at > claimed_at
    ),
    CONSTRAINT ck_generation_notifications_finished CHECK (
        (status IN ('queued', 'claimed') AND finished_at IS NULL)
        OR (status IN ('sent', 'failed', 'unknown') AND finished_at IS NOT NULL)
    ),
    CONSTRAINT ck_generation_notifications_callback_pair CHECK (
        (approve_intent_id IS NULL AND reject_intent_id IS NULL)
        OR (approve_intent_id IS NOT NULL AND reject_intent_id IS NOT NULL)
    ),
    CONSTRAINT ck_generation_notifications_sent_result CHECK (
        status <> 'sent' OR (review_message_id IS NOT NULL AND sent_at IS NOT NULL)
    ),
    CONSTRAINT ck_generation_notifications_error_length CHECK (
        error IS NULL OR char_length(error) <= 1000
    )
);

CREATE INDEX IF NOT EXISTS ix_generation_notifications_queue
    ON generation_notifications(workspace_id, status, created_at);
CREATE INDEX IF NOT EXISTS ix_generation_notifications_lease
    ON generation_notifications(workspace_id, status, lease_expires_at);

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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_publish_snapshot_hash'
          AND conrelid = 'publish_attempts'::regclass
    ) THEN
        ALTER TABLE publish_attempts ADD CONSTRAINT ck_publish_snapshot_hash
            CHECK (snapshot_hash ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
END $$;
ALTER TABLE publish_attempts VALIDATE CONSTRAINT ck_publish_snapshot_hash;

CREATE INDEX IF NOT EXISTS ix_publish_attempts_draft_started
    ON publish_attempts(draft_id, started_at);

CREATE TABLE IF NOT EXISTS publication_requests (
    id VARCHAR(36) PRIMARY KEY,
    workspace_id VARCHAR(80) NOT NULL,
    draft_id VARCHAR(36) NOT NULL REFERENCES drafts(id) ON DELETE RESTRICT,
    revision_id VARCHAR(36) NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    approval_id VARCHAR(36) NOT NULL REFERENCES approvals(id) ON DELETE RESTRICT,
    requested_by VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    channel VARCHAR(30) NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    snapshot_hash VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    claim_token_hash VARCHAR(64),
    claim_fence INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(36) REFERENCES users(id) ON DELETE RESTRICT,
    claimed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    publish_attempt_id VARCHAR(36) REFERENCES publish_attempts(id) ON DELETE RESTRICT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_publication_request_workspace_channel_key UNIQUE (
        workspace_id, channel, idempotency_key
    ),
    CONSTRAINT uq_publication_request_snapshot UNIQUE (
        workspace_id, channel, draft_id, revision_id
    ),
    CONSTRAINT uq_publication_request_publish_attempt UNIQUE (publish_attempt_id),
    CONSTRAINT uq_publication_request_claim_token_hash UNIQUE (claim_token_hash),
    CONSTRAINT ck_publication_requests_status CHECK (
        status IN ('queued', 'claimed', 'succeeded', 'failed', 'unknown')
    ),
    CONSTRAINT ck_publication_requests_snapshot_hash CHECK (
        snapshot_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_publication_requests_claim_token_hash CHECK (
        claim_token_hash IS NULL OR claim_token_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_publication_requests_claim_fence CHECK (claim_fence >= 0),
    CONSTRAINT ck_publication_requests_claim_state CHECK (
        (
            status = 'queued'
            AND claim_fence = 0
            AND claim_token_hash IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
            AND lease_expires_at IS NULL
        ) OR (
            status <> 'queued'
            AND claim_fence >= 1
            AND claim_token_hash IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_publication_requests_lease CHECK (
        lease_expires_at IS NULL OR lease_expires_at > claimed_at
    ),
    CONSTRAINT ck_publication_requests_finished CHECK (
        (status IN ('queued', 'claimed') AND finished_at IS NULL)
        OR (status IN ('succeeded', 'failed', 'unknown') AND finished_at IS NOT NULL)
    ),
    CONSTRAINT ck_publication_requests_attempt_state CHECK (
        (status IN ('queued', 'claimed') AND publish_attempt_id IS NULL)
        OR status IN ('succeeded', 'failed', 'unknown')
    ),
    CONSTRAINT ck_publication_requests_final_attempt CHECK (
        status NOT IN ('succeeded', 'failed') OR publish_attempt_id IS NOT NULL
    ),
    CONSTRAINT ck_publication_requests_error_length CHECK (
        error IS NULL OR char_length(error) <= 1000
    )
);

CREATE INDEX IF NOT EXISTS ix_publication_requests_queue
    ON publication_requests(workspace_id, channel, status, created_at);
CREATE INDEX IF NOT EXISTS ix_publication_requests_lease
    ON publication_requests(workspace_id, status, lease_expires_at);

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
