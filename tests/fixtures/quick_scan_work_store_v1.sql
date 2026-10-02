PRAGMA user_version=1;
CREATE TABLE work_item (
        work_item_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        scope TEXT NOT NULL CHECK (scope IN ('entity','security','segment')),
        scope_id TEXT NOT NULL,
        identity_revision INTEGER NOT NULL CHECK (identity_revision >= 1),
        source_binding_version INTEGER NOT NULL CHECK (source_binding_version >= 1),
        identity_state TEXT NOT NULL CHECK (identity_state IN ('provisional','verified')),
        source_binding_ref TEXT NOT NULL,
        source_binding_refs_json TEXT NOT NULL,
        identity_snapshot_sha256 TEXT NOT NULL,
        question_fingerprint TEXT NOT NULL,
        routing_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('pending','leased','uncertain','result_ready','delivered','failed','cancelled')),
        lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
        lease_token TEXT,
        lease_expires_at REAL,
        uncertain_attempt_id TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (entity_id,question_id,generation,scope,scope_id,
            identity_revision,source_binding_version,identity_state,source_binding_refs_json),
        CHECK ((status='leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR (status!='leased' AND lease_token IS NULL AND lease_expires_at IS NULL))
    );
CREATE TABLE work_run_ref (
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        run_id TEXT NOT NULL,
        scan_id TEXT NOT NULL,
        attached_at REAL NOT NULL,
        PRIMARY KEY (work_item_id,run_id,scan_id)
    );
CREATE TABLE attempt (
        attempt_id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        lease_epoch INTEGER NOT NULL CHECK (lease_epoch >= 1),
        lease_token TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
        route_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_requested TEXT NOT NULL,
        request_cache_key TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN
            ('prepared','abandoned_unsent','send_intent','confirmed_failure',
             'response_available','uncertain')),
        prepared_at REAL NOT NULL,
        send_intent_at REAL,
        completed_at REAL,
        http_status_code INTEGER,
        request_id TEXT,
        receipt_sha256 TEXT,
        failure_category TEXT,
        provider_error_code TEXT,
        late_receipt_sha256 TEXT,
        UNIQUE (work_item_id,ordinal)
    );
CREATE TABLE work_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        attempt_id TEXT REFERENCES attempt(attempt_id),
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        lease_epoch INTEGER NOT NULL,
        occurred_at REAL NOT NULL
    );
CREATE INDEX work_item_status_expiry_idx ON work_item(status,lease_expires_at);
CREATE INDEX attempt_work_phase_idx ON attempt(work_item_id,phase);
CREATE INDEX attempt_request_cache_key_idx ON attempt(request_cache_key);
INSERT INTO work_item (
    work_item_id,entity_id,question_id,generation,scope,scope_id,identity_revision,
    source_binding_version,identity_state,source_binding_ref,source_binding_refs_json,
    identity_snapshot_sha256,question_fingerprint,routing_fingerprint,status,lease_epoch,
    lease_token,lease_expires_at,uncertain_attempt_id,created_at,updated_at
) VALUES (
    'WK_V1_FIXTURE','ENT_FIXTURE','IQS_01',1,'entity','ENT_FIXTURE',1,
    1,'verified','BND_FIXTURE','["BND_FIXTURE"]',
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    'pending',0,NULL,NULL,NULL,1800000000.0,1800000000.0
);
INSERT INTO work_run_ref (work_item_id,run_id,scan_id,attached_at)
VALUES ('WK_V1_FIXTURE','RUN_V1_FIXTURE','SCAN_V1_FIXTURE',1800000000.0);
INSERT INTO attempt (
    attempt_id,work_item_id,lease_epoch,lease_token,ordinal,route_id,provider,
    model_requested,request_cache_key,prompt_sha256,phase,prepared_at,send_intent_at,
    completed_at,http_status_code,request_id,receipt_sha256,failure_category,
    provider_error_code,late_receipt_sha256
) VALUES (
    'ATT_V1_FIXTURE','WK_V1_FIXTURE',1,'LEASE_V1_FIXTURE',1,'route-v1','openai',
    'gpt-fixture','REQ_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    'prepared',1800000000.0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL
);
INSERT INTO work_event (
    event_id,work_item_id,attempt_id,event_type,from_status,to_status,lease_epoch,occurred_at
) VALUES
    (1,'WK_V1_FIXTURE',NULL,'created',NULL,'pending',0,1800000000.0),
    (2,'WK_V1_FIXTURE','ATT_V1_FIXTURE','attempt_prepared',NULL,'pending',1,1800000000.0);
