-- People sign in; suppliers are reached only through membership. Existing profiles get no members here:
-- until someone is granted access (python -m tenderfit.auth grant) they cannot be reached through the API.
CREATE TABLE app_user (
    id bigserial PRIMARY KEY,
    email text NOT NULL,
    password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz
);
CREATE UNIQUE INDEX app_user_email ON app_user (lower(email));

CREATE TABLE profile_member (
    user_id bigint NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    role text NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'member')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, profile_id)
);
CREATE INDEX profile_member_profile ON profile_member (profile_id);

-- Only a hash of the token is stored, so reading this table does not give anyone a session.
CREATE TABLE user_session (
    token_hash text PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    active_profile_id bigint REFERENCES business_profile (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX user_session_user ON user_session (user_id);

CREATE TABLE login_failure (
    id bigserial PRIMARY KEY,
    email text NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_failure_email ON login_failure (lower(email), at);
