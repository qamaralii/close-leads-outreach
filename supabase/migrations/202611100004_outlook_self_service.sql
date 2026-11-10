BEGIN;

CREATE TABLE public.outlook_identities (
    user_id UUID PRIMARY KEY REFERENCES public.outreach_members(user_id),
    entity_id TEXT NOT NULL UNIQUE
);
INSERT INTO public.outlook_identities SELECT user_id, composio_entity_id FROM public.outlook_connections;

CREATE TABLE public.outlook_auth_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.outlook_identities(user_id),
    state_hash TEXT NOT NULL UNIQUE CHECK (state_hash ~ '^[a-f0-9]{64}$'),
    auth_config_id TEXT NOT NULL CHECK (btrim(auth_config_id) <> ''),
    account_id TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '10 minutes'
);
CREATE INDEX outlook_auth_owner_idx ON public.outlook_auth_attempts(user_id, created_at DESC);
CREATE UNIQUE INDEX outlook_auth_one_pending ON public.outlook_auth_attempts(user_id) WHERE status = 'pending';

-- Durable disable-only queue. An account placed here must never be activated again.
CREATE TABLE public.outlook_account_cleanup (
    account_id TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.outreach_members(user_id),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.outlook_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outlook_auth_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outlook_account_cleanup ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.outlook_identities, public.outlook_auth_attempts, public.outlook_account_cleanup FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.outlook_identities, public.outlook_auth_attempts, public.outlook_account_cleanup TO service_role;

-- Called only by authenticated application server code with a verified user UUID.
-- Serialize start/complete/disconnect for each member, including first connection.
CREATE FUNCTION public.manage_outlook_connection(p_user_id UUID, p_operation TEXT, p_data JSONB DEFAULT '{}')
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_entity TEXT;
    v_attempt public.outlook_auth_attempts%ROWTYPE;
    v_old public.outlook_connections%ROWTYPE;
BEGIN
    PERFORM 1 FROM public.outreach_members WHERE user_id = p_user_id AND is_active FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Active membership required'; END IF;
    INSERT INTO public.outlook_identities(user_id, entity_id)
        VALUES (p_user_id, 'close-outreach-' || p_user_id::text) ON CONFLICT (user_id) DO NOTHING;
    SELECT entity_id INTO v_entity FROM public.outlook_identities WHERE user_id = p_user_id;

    IF p_operation = 'begin' THEN
        IF EXISTS (SELECT 1 FROM public.outlook_auth_attempts WHERE user_id = p_user_id AND created_at > now() - interval '30 seconds') THEN
            RAISE EXCEPTION 'Wait before starting another authorization';
        END IF;
    END IF;
    IF p_operation IN ('begin', 'cancel', 'disconnect') THEN
        INSERT INTO public.outlook_account_cleanup(account_id, user_id)
            SELECT account_id, user_id FROM public.outlook_auth_attempts
            WHERE user_id = p_user_id AND status = 'pending' AND account_id IS NOT NULL
            ON CONFLICT DO NOTHING;
        UPDATE public.outlook_auth_attempts SET status = 'cancelled' WHERE user_id = p_user_id AND status = 'pending';
        IF p_operation = 'begin' THEN
            INSERT INTO public.outlook_auth_attempts(user_id, state_hash, auth_config_id)
                VALUES (p_user_id, p_data->>'state_hash', p_data->>'auth_config_id') RETURNING * INTO v_attempt;
            RETURN jsonb_build_object('id', v_attempt.id, 'entity_id', v_entity);
        ELSIF p_operation = 'cancel' THEN RETURN '{}'; END IF;
    END IF;

    IF p_operation IN ('bind', 'complete') THEN
        SELECT * INTO v_attempt FROM public.outlook_auth_attempts
            WHERE id = (p_data->>'attempt_id')::uuid AND user_id = p_user_id AND state_hash = p_data->>'state_hash' FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'Invalid authorization attempt'; END IF;
        IF p_operation = 'bind' THEN
            IF COALESCE(p_data->>'account_id', '') = '' OR v_attempt.account_id IS NOT NULL THEN RAISE EXCEPTION 'Invalid account binding'; END IF;
            -- A concurrent cancel/start can win while the provider creates the link.
            IF v_attempt.status <> 'pending' OR v_attempt.expires_at <= now() THEN
                INSERT INTO public.outlook_account_cleanup(account_id, user_id) VALUES (p_data->>'account_id', p_user_id) ON CONFLICT DO NOTHING;
                RETURN jsonb_build_object('bound', false);
            END IF;
            IF EXISTS (SELECT 1 FROM public.outlook_account_cleanup WHERE account_id = p_data->>'account_id')
                OR EXISTS (SELECT 1 FROM public.outlook_connections WHERE composio_connected_account_id = p_data->>'account_id') THEN
                RAISE EXCEPTION 'Account already used';
            END IF;
            UPDATE public.outlook_auth_attempts SET account_id = p_data->>'account_id' WHERE id = v_attempt.id;
            RETURN jsonb_build_object('bound', true);
        END IF;
        IF v_attempt.account_id IS DISTINCT FROM p_data->>'account_id' OR v_attempt.account_id IS NULL THEN RAISE EXCEPTION 'Account mismatch'; END IF;
        IF v_attempt.status = 'completed' THEN RETURN jsonb_build_object('completed', true); END IF;
        IF v_attempt.status <> 'pending' OR v_attempt.expires_at <= now() THEN RAISE EXCEPTION 'Authorization expired or superseded'; END IF;
        IF EXISTS (SELECT 1 FROM public.outlook_account_cleanup WHERE account_id = v_attempt.account_id) THEN RAISE EXCEPTION 'Account retired'; END IF;
        IF COALESCE(btrim(p_data->>'mailbox'), '') = '' THEN RAISE EXCEPTION 'Verified mailbox required'; END IF;
        SELECT * INTO v_old FROM public.outlook_connections WHERE user_id = p_user_id FOR UPDATE;
        IF FOUND AND v_old.composio_connected_account_id <> v_attempt.account_id THEN
            INSERT INTO public.outlook_account_cleanup(account_id, user_id) VALUES (v_old.composio_connected_account_id, p_user_id) ON CONFLICT DO NOTHING;
        END IF;
        INSERT INTO public.outlook_connections(user_id, composio_entity_id, composio_connected_account_id, mailbox_email)
            VALUES (p_user_id, v_entity, v_attempt.account_id, lower(btrim(p_data->>'mailbox')))
            ON CONFLICT (user_id) DO UPDATE SET composio_connected_account_id = EXCLUDED.composio_connected_account_id,
                mailbox_email = EXCLUDED.mailbox_email, status = 'active', verified_at = now();
        UPDATE public.outlook_auth_attempts SET status = 'completed' WHERE id = v_attempt.id;
        RETURN jsonb_build_object('completed', true);
    ELSIF p_operation = 'disconnect' THEN
        SELECT * INTO v_old FROM public.outlook_connections WHERE user_id = p_user_id FOR UPDATE;
        IF FOUND THEN
            UPDATE public.outlook_connections SET status = 'disconnected' WHERE id = v_old.id;
            INSERT INTO public.outlook_account_cleanup(account_id, user_id) VALUES (v_old.composio_connected_account_id, p_user_id) ON CONFLICT DO NOTHING;
        END IF;
        RETURN '{}';
    ELSIF p_operation = 'cleanup_done' THEN
        UPDATE public.outlook_account_cleanup SET completed_at = now() WHERE user_id = p_user_id AND account_id = p_data->>'account_id';
        RETURN '{}';
    END IF;
    RAISE EXCEPTION 'Unsupported operation';
END;
$$;
REVOKE ALL ON FUNCTION public.manage_outlook_connection(UUID,TEXT,JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.manage_outlook_connection(UUID,TEXT,JSONB) TO service_role;
COMMIT;
