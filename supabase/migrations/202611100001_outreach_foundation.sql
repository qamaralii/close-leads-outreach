-- Run once in the Supabase SQL Editor as the project database administrator.
-- Prerequisite: public.close_leads from the working Flow 1 already exists.
BEGIN;

CREATE TABLE public.outreach_members (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE FUNCTION public.is_outreach_member()
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = '' AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.outreach_members
        WHERE user_id = auth.uid() AND is_active
    );
$$;
REVOKE ALL ON FUNCTION public.is_outreach_member() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.is_outreach_member() TO authenticated, service_role;

-- One managed Outlook connection per app user for the first version.
-- No OAuth tokens or API secrets belong in this table.
CREATE TABLE public.outlook_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES public.outreach_members(user_id),
    composio_entity_id TEXT NOT NULL UNIQUE CHECK (btrim(composio_entity_id) <> ''),
    composio_connected_account_id TEXT NOT NULL UNIQUE
        CHECK (btrim(composio_connected_account_id) <> ''),
    mailbox_email TEXT NOT NULL CHECK (btrim(mailbox_email) <> ''),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disconnected', 'error')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, id)
);

CREATE TABLE public.campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by UUID NOT NULL REFERENCES public.outreach_members(user_id),
    request_id TEXT NOT NULL CHECK (btrim(request_id) <> ''),
    sender_connection_id UUID NOT NULL,
    -- Freeze the reviewed sender as well as the recipients.
    sender_mailbox_snapshot TEXT NOT NULL CHECK (btrim(sender_mailbox_snapshot) <> ''),
    sender_entity_snapshot TEXT NOT NULL CHECK (btrim(sender_entity_snapshot) <> ''),
    sender_account_snapshot TEXT NOT NULL CHECK (btrim(sender_account_snapshot) <> ''),
    subject_template TEXT NOT NULL CHECK (btrim(subject_template) <> ''),
    body_template TEXT NOT NULL CHECK (btrim(body_template) <> ''),
    is_html BOOLEAN NOT NULL DEFAULT FALSE,
    expected_recipient_count INTEGER NOT NULL CHECK (expected_recipient_count > 0),
    preview_ready_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted')),
    -- Scheduling fields are reserved; scheduled submission is deliberately disabled.
    send_mode TEXT NOT NULL DEFAULT 'now' CHECK (send_mode = 'now'),
    scheduled_at TIMESTAMPTZ CHECK (scheduled_at IS NULL),
    timezone TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    UNIQUE (created_by, request_id),
    FOREIGN KEY (created_by, sender_connection_id)
        REFERENCES public.outlook_connections(user_id, id),
    CHECK (status <> 'submitted' OR submitted_at IS NOT NULL)
);
CREATE INDEX campaigns_owner_created_idx ON public.campaigns(created_by, created_at DESC);

CREATE TABLE public.campaign_recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES public.campaigns(id),
    close_lead_id TEXT NOT NULL REFERENCES public.close_leads(close_lead_id),
    recipient_email TEXT NOT NULL CHECK (btrim(recipient_email) <> ''),
    normalized_email TEXT GENERATED ALWAYS AS (lower(btrim(recipient_email))) STORED,
    recipient_name TEXT,
    company_name TEXT,
    rendered_subject TEXT NOT NULL CHECK (btrim(rendered_subject) <> ''),
    rendered_body TEXT NOT NULL CHECK (btrim(rendered_body) <> ''),
    step_number INTEGER NOT NULL DEFAULT 1 CHECK (step_number = 1),
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'queued', 'sending', 'submitted', 'failed', 'unknown', 'cancelled')),
    due_at TIMESTAMPTZ,
    claim_token UUID,
    claimed_at TIMESTAMPTZ,
    submitted_at TIMESTAMPTZ,
    provider_status_code INTEGER,
    provider_message_id TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, normalized_email, step_number),
    UNIQUE (campaign_id, close_lead_id, step_number),
    CHECK (status <> 'sending' OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL)),
    CHECK (status <> 'submitted' OR submitted_at IS NOT NULL)
);
CREATE INDEX campaign_recipients_status_idx ON public.campaign_recipients(campaign_id, status);

CREATE TABLE public.outreach_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_id UUID NOT NULL REFERENCES public.campaign_recipients(id),
    claim_token UUID NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'sending'
        CHECK (status IN ('sending', 'submitted', 'failed', 'unknown')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    provider_status_code INTEGER,
    provider_message_id TEXT,
    error TEXT
);
CREATE INDEX outreach_attempts_recipient_idx ON public.outreach_attempts(recipient_id);

-- Shared eligibility state, maintained by the backend, not exposed to browsers.
CREATE TABLE public.email_suppressions (
    normalized_email TEXT PRIMARY KEY
        CHECK (normalized_email = lower(btrim(normalized_email)) AND normalized_email <> ''),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.outreach_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outlook_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.campaign_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outreach_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_suppressions ENABLE ROW LEVEL SECURITY;

-- Browser access is read-only. Privileged writes go through authenticated server routes.
REVOKE ALL ON public.outreach_members, public.outlook_connections, public.campaigns,
    public.campaign_recipients, public.outreach_attempts, public.email_suppressions
    FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.outreach_members, public.outlook_connections, public.campaigns,
    public.campaign_recipients, public.outreach_attempts TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.outreach_members,
    public.outlook_connections, public.email_suppressions TO service_role;
REVOKE INSERT, UPDATE, DELETE ON public.campaigns, public.campaign_recipients,
    public.outreach_attempts FROM service_role;
GRANT SELECT ON public.campaigns, public.campaign_recipients,
    public.outreach_attempts TO service_role;

CREATE POLICY outreach_members_read_self ON public.outreach_members
    FOR SELECT TO authenticated USING (user_id = auth.uid());
CREATE POLICY outlook_connections_read_self ON public.outlook_connections
    FOR SELECT TO authenticated
    USING (user_id = auth.uid() AND public.is_outreach_member());
CREATE POLICY campaigns_read_own ON public.campaigns
    FOR SELECT TO authenticated
    USING (created_by = auth.uid() AND public.is_outreach_member());
CREATE POLICY campaign_recipients_read_own ON public.campaign_recipients
    FOR SELECT TO authenticated USING (
        public.is_outreach_member() AND EXISTS (
            SELECT 1 FROM public.campaigns c
            WHERE c.id = campaign_id AND c.created_by = auth.uid()
        )
    );
CREATE POLICY outreach_attempts_read_own ON public.outreach_attempts
    FOR SELECT TO authenticated USING (
        public.is_outreach_member() AND EXISTS (
            SELECT 1 FROM public.campaign_recipients r
            JOIN public.campaigns c ON c.id = r.campaign_id
            WHERE r.id = recipient_id AND c.created_by = auth.uid()
        )
    );

-- Preserve the existing Flow 1 table and service-role writer grants.
ALTER TABLE public.close_leads ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.close_leads FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.close_leads TO authenticated;
CREATE POLICY close_leads_read_team ON public.close_leads
    FOR SELECT TO authenticated USING (public.is_outreach_member());

-- Recipient preview content becomes immutable once the preview is finalized.
-- UPDATE/DELETE already hold a recipient row lock before this trigger obtains
-- the parent lock, so privileged callers must retry SQLSTATE 40P01 on a rare
-- deadlock with concurrent finalization/submission.
CREATE FUNCTION public.guard_recipient_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE
    v_status TEXT;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.campaign_id IS DISTINCT FROM OLD.campaign_id THEN
        RAISE EXCEPTION 'Recipient campaign cannot change';
    END IF;
    SELECT status INTO v_status FROM public.campaigns
    WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.campaign_id ELSE NEW.campaign_id END
    FOR UPDATE;
    IF v_status <> 'draft' OR EXISTS (
        SELECT 1 FROM public.campaigns
        WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.campaign_id ELSE NEW.campaign_id END
          AND preview_ready_at IS NOT NULL
    ) THEN
        IF TG_OP IN ('INSERT', 'DELETE') THEN
            RAISE EXCEPTION 'Finalized recipient set is immutable';
        END IF;
        IF ROW(NEW.close_lead_id, NEW.recipient_email, NEW.recipient_name, NEW.company_name,
               NEW.rendered_subject, NEW.rendered_body, NEW.step_number)
           IS DISTINCT FROM
           ROW(OLD.close_lead_id, OLD.recipient_email, OLD.recipient_name, OLD.company_name,
               OLD.rendered_subject, OLD.rendered_body, OLD.step_number) THEN
            RAISE EXCEPTION 'Finalized recipient snapshot is immutable';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER guard_recipient_snapshot BEFORE INSERT OR UPDATE OR DELETE
    ON public.campaign_recipients FOR EACH ROW EXECUTE FUNCTION public.guard_recipient_snapshot();

CREATE FUNCTION public.guard_campaign_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF ROW(NEW.created_by, NEW.request_id, NEW.sender_connection_id,
           NEW.sender_mailbox_snapshot, NEW.sender_entity_snapshot, NEW.sender_account_snapshot)
       IS DISTINCT FROM
       ROW(OLD.created_by, OLD.request_id, OLD.sender_connection_id,
           OLD.sender_mailbox_snapshot, OLD.sender_entity_snapshot, OLD.sender_account_snapshot) THEN
        RAISE EXCEPTION 'Campaign ownership and sender are immutable';
    END IF;
    IF OLD.status = 'submitted' AND NEW.status <> 'submitted' THEN
        RAISE EXCEPTION 'Submitted campaign cannot transition state';
    END IF;
    IF OLD.status = 'draft' AND NEW.status NOT IN ('draft', 'submitted') THEN
        RAISE EXCEPTION 'Invalid campaign state transition';
    END IF;
    IF (OLD.preview_ready_at IS NOT NULL OR NEW.status <> 'draft') AND
       ROW(NEW.subject_template, NEW.body_template, NEW.is_html,
           NEW.expected_recipient_count, NEW.send_mode, NEW.scheduled_at, NEW.timezone)
       IS DISTINCT FROM
       ROW(OLD.subject_template, OLD.body_template, OLD.is_html,
           OLD.expected_recipient_count, OLD.send_mode, OLD.scheduled_at, OLD.timezone) THEN
        RAISE EXCEPTION 'Finalized campaign content is immutable';
    END IF;
    IF OLD.preview_ready_at IS NOT NULL
       AND NEW.preview_ready_at IS DISTINCT FROM OLD.preview_ready_at THEN
        RAISE EXCEPTION 'Preview finalization is immutable';
    END IF;
    IF NEW.status = 'draft' AND NEW.submitted_at IS NOT NULL THEN
        RAISE EXCEPTION 'Draft campaign cannot have a submission time';
    END IF;
    IF OLD.status = 'submitted'
       AND NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN
        RAISE EXCEPTION 'Submission time is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER guard_campaign_snapshot BEFORE UPDATE ON public.campaigns
    FOR EACH ROW EXECUTE FUNCTION public.guard_campaign_snapshot();

-- Atomically create the campaign and exact rendered recipient preview.
-- p_recipients is an array of objects with close_lead_id, recipient_email,
-- recipient_name, company_name, rendered_subject, and rendered_body.
CREATE FUNCTION public.prepare_outreach_preview(
    p_user_id UUID, p_request_id TEXT, p_sender_connection_id UUID,
    p_subject_template TEXT, p_body_template TEXT, p_is_html BOOLEAN,
    p_recipients JSONB
)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_connection public.outlook_connections%ROWTYPE;
    v_campaign_id UUID;
    v_expected INTEGER;
    v_inserted INTEGER;
BEGIN
    IF p_request_id IS NULL OR btrim(p_request_id) = ''
       OR p_subject_template IS NULL OR btrim(p_subject_template) = ''
       OR p_body_template IS NULL OR btrim(p_body_template) = '' THEN
        RAISE EXCEPTION 'Request ID, subject, and body are required';
    END IF;
    IF p_recipients IS NULL OR jsonb_typeof(p_recipients) <> 'array' THEN
        RAISE EXCEPTION 'Recipients must be a JSON array';
    END IF;
    v_expected := jsonb_array_length(p_recipients);
    IF v_expected = 0 THEN RAISE EXCEPTION 'At least one recipient is required'; END IF;
    SELECT o.* INTO v_connection FROM public.outlook_connections o
    JOIN public.outreach_members m ON m.user_id = o.user_id AND m.is_active
    WHERE o.id = p_sender_connection_id AND o.user_id = p_user_id
      AND o.status = 'active' FOR SHARE OF o;
    IF NOT FOUND THEN RAISE EXCEPTION 'Sender connection unavailable'; END IF;
    INSERT INTO public.campaigns(
        created_by, request_id, sender_connection_id, sender_mailbox_snapshot,
        sender_entity_snapshot, sender_account_snapshot, subject_template,
        body_template, is_html, expected_recipient_count
    ) VALUES (
        p_user_id, p_request_id, p_sender_connection_id, v_connection.mailbox_email,
        v_connection.composio_entity_id, v_connection.composio_connected_account_id,
        p_subject_template, p_body_template, COALESCE(p_is_html, FALSE), v_expected
    ) RETURNING id INTO v_campaign_id;
    INSERT INTO public.campaign_recipients(
        campaign_id, close_lead_id, recipient_email, recipient_name, company_name,
        rendered_subject, rendered_body
    )
    SELECT v_campaign_id, x.close_lead_id, x.recipient_email, x.recipient_name,
           x.company_name, x.rendered_subject, x.rendered_body
    FROM jsonb_to_recordset(p_recipients) AS x(
        close_lead_id TEXT, recipient_email TEXT, recipient_name TEXT,
        company_name TEXT, rendered_subject TEXT, rendered_body TEXT
    );
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    IF v_inserted <> v_expected THEN
        RAISE EXCEPTION 'Expected % recipients, inserted %', v_expected, v_inserted;
    END IF;
    UPDATE public.campaigns SET preview_ready_at = NOW() WHERE id = v_campaign_id;
    RETURN v_campaign_id;
END;
$$;

-- Service-only RPC. p_user_id MUST come from the server's verified auth session.
CREATE FUNCTION public.submit_outreach_campaign(p_user_id UUID, p_campaign_id UUID)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_campaign public.campaigns%ROWTYPE;
BEGIN
    SELECT c.* INTO v_campaign FROM public.campaigns c
    JOIN public.outreach_members m ON m.user_id = c.created_by AND m.is_active
    WHERE c.id = p_campaign_id AND c.created_by = p_user_id FOR UPDATE OF c;
    IF NOT FOUND THEN RAISE EXCEPTION 'Campaign unavailable'; END IF;
    IF v_campaign.status = 'submitted' THEN RETURN v_campaign.id; END IF;
    IF v_campaign.status <> 'draft' THEN RAISE EXCEPTION 'Campaign cannot be submitted'; END IF;
    IF v_campaign.preview_ready_at IS NULL THEN RAISE EXCEPTION 'Preview is incomplete'; END IF;
    IF (SELECT count(*) FROM public.campaign_recipients
        WHERE campaign_id = p_campaign_id AND status = 'ready')
       <> v_campaign.expected_recipient_count THEN
        RAISE EXCEPTION 'Finalized recipient count changed';
    END IF;
    IF EXISTS (SELECT 1 FROM public.campaign_recipients
               WHERE campaign_id = p_campaign_id AND status <> 'ready') THEN
        RAISE EXCEPTION 'Draft contains unexpected recipient states';
    END IF;
    PERFORM 1 FROM public.outlook_connections o
    WHERE o.id = v_campaign.sender_connection_id AND o.user_id = p_user_id
      AND o.status = 'active'
      AND o.composio_entity_id = v_campaign.sender_entity_snapshot
      AND o.composio_connected_account_id = v_campaign.sender_account_snapshot
      AND lower(btrim(o.mailbox_email)) = lower(btrim(v_campaign.sender_mailbox_snapshot))
    FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Reviewed sender is no longer available'; END IF;
    UPDATE public.campaigns SET status = 'submitted', submitted_at = NOW()
    WHERE id = p_campaign_id;
    UPDATE public.campaign_recipients SET status = 'queued', due_at = NOW()
    WHERE campaign_id = p_campaign_id;
    RETURN p_campaign_id;
END;
$$;

-- Claim exactly one job. An empty result means it was already claimed, is not
-- eligible, or was cancelled by suppression. Never reclaim sending/unknown jobs.
CREATE FUNCTION public.claim_outreach_recipient(p_user_id UUID, p_campaign_id UUID, p_recipient_id UUID)
RETURNS TABLE (
    recipient_id UUID, attempt_id UUID, claim_token UUID,
    entity_id TEXT, connected_account_id TEXT, sender_email TEXT,
    recipient_email TEXT, subject TEXT, body TEXT, is_html BOOLEAN
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_campaign public.campaigns%ROWTYPE;
    v_recipient public.campaign_recipients%ROWTYPE;
    v_token UUID := gen_random_uuid();
    v_attempt UUID;
BEGIN
    SELECT c.* INTO v_campaign FROM public.campaigns c
    JOIN public.outreach_members m ON m.user_id = c.created_by AND m.is_active
    WHERE c.id = p_campaign_id AND c.created_by = p_user_id FOR UPDATE OF c;
    IF NOT FOUND THEN RAISE EXCEPTION 'Campaign unavailable'; END IF;
    IF v_campaign.status <> 'submitted' THEN RETURN; END IF;
    SELECT r.* INTO v_recipient FROM public.campaign_recipients r
    WHERE r.id = p_recipient_id AND r.campaign_id = p_campaign_id
      AND r.status = 'queued' AND r.due_at <= NOW() FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF EXISTS (SELECT 1 FROM public.email_suppressions s
               WHERE s.normalized_email = v_recipient.normalized_email) THEN
        UPDATE public.campaign_recipients SET status = 'cancelled', error = 'Recipient suppressed'
        WHERE id = p_recipient_id;
        RETURN;
    END IF;
    -- Hold the connection lock through the claim transaction.
    PERFORM 1 FROM public.outlook_connections o
    WHERE o.id = v_campaign.sender_connection_id AND o.user_id = p_user_id
      AND o.status = 'active'
      AND o.composio_entity_id = v_campaign.sender_entity_snapshot
      AND o.composio_connected_account_id = v_campaign.sender_account_snapshot
      AND lower(btrim(o.mailbox_email)) = lower(btrim(v_campaign.sender_mailbox_snapshot))
    FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Reviewed sender is no longer available'; END IF;
    UPDATE public.campaign_recipients SET status = 'sending', claim_token = v_token,
        claimed_at = NOW(), error = NULL WHERE id = p_recipient_id;
    INSERT INTO public.outreach_attempts(recipient_id, claim_token)
    VALUES (p_recipient_id, v_token) RETURNING id INTO v_attempt;
    RETURN QUERY SELECT v_recipient.id, v_attempt, v_token,
        v_campaign.sender_entity_snapshot, v_campaign.sender_account_snapshot,
        v_campaign.sender_mailbox_snapshot, v_recipient.recipient_email,
        v_recipient.rendered_subject, v_recipient.rendered_body, v_campaign.is_html;
END;
$$;

-- Persist a send outcome using the original claim token, without calling Outlook again.
CREATE FUNCTION public.finish_outreach_attempt(
    p_user_id UUID, p_attempt_id UUID, p_claim_token UUID, p_status TEXT,
    p_provider_status_code INTEGER DEFAULT NULL,
    p_provider_message_id TEXT DEFAULT NULL, p_error TEXT DEFAULT NULL
)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_attempt public.outreach_attempts%ROWTYPE;
    v_campaign_id UUID;
    v_recipient public.campaign_recipients%ROWTYPE;
BEGIN
    IF p_status IS NULL OR p_status NOT IN ('submitted', 'failed', 'unknown') THEN
        RAISE EXCEPTION 'Invalid attempt outcome';
    END IF;
    IF p_status = 'submitted' AND p_provider_status_code IS DISTINCT FROM 202 THEN
        RAISE EXCEPTION 'Submitted requires the verified Outlook 202 response';
    END IF;
    SELECT c.id INTO v_campaign_id FROM public.outreach_attempts a
    JOIN public.campaign_recipients r ON r.id = a.recipient_id
    JOIN public.campaigns c ON c.id = r.campaign_id
    WHERE a.id = p_attempt_id AND c.created_by = p_user_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Attempt unavailable'; END IF;
    -- Use the same parent -> recipient -> attempt lock order as claiming.
    PERFORM 1 FROM public.campaigns WHERE id = v_campaign_id FOR UPDATE;
    SELECT r.* INTO v_recipient FROM public.campaign_recipients r
    JOIN public.outreach_attempts a ON a.recipient_id = r.id
    WHERE a.id = p_attempt_id FOR UPDATE OF r;
    SELECT * INTO v_attempt FROM public.outreach_attempts
    WHERE id = p_attempt_id FOR UPDATE;
    IF v_attempt.claim_token IS DISTINCT FROM p_claim_token
       OR v_recipient.claim_token IS DISTINCT FROM p_claim_token THEN
        RAISE EXCEPTION 'Claim token mismatch';
    END IF;
    IF v_attempt.status <> 'sending' THEN
        IF v_attempt.status = p_status
           AND v_attempt.provider_status_code IS NOT DISTINCT FROM p_provider_status_code
           AND v_attempt.provider_message_id IS NOT DISTINCT FROM p_provider_message_id
           AND v_attempt.error IS NOT DISTINCT FROM p_error THEN
            RETURN v_attempt.status;
        END IF;
        RAISE EXCEPTION 'Attempt already finalized with a different outcome';
    END IF;
    IF v_recipient.status <> 'sending' THEN RAISE EXCEPTION 'Job is not sending'; END IF;
    UPDATE public.outreach_attempts SET status = p_status, finished_at = NOW(),
        provider_status_code = p_provider_status_code,
        provider_message_id = p_provider_message_id, error = p_error
    WHERE id = p_attempt_id;
    UPDATE public.campaign_recipients SET status = p_status,
        submitted_at = CASE WHEN p_status = 'submitted' THEN NOW() ELSE NULL END,
        provider_status_code = p_provider_status_code,
        provider_message_id = p_provider_message_id, error = p_error
    WHERE id = v_attempt.recipient_id;
    RETURN p_status;
END;
$$;

-- Lifecycle RPCs are SECURITY DEFINER so the service role does not need broad
-- table mutation grants. Never grant them to browser roles: service callers supply
-- verified identity, whereas arbitrary browser-supplied p_user_id is untrusted.
REVOKE ALL ON FUNCTION public.guard_recipient_snapshot(), public.guard_campaign_snapshot()
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.prepare_outreach_preview(UUID, TEXT, UUID, TEXT, TEXT, BOOLEAN, JSONB),
    public.submit_outreach_campaign(UUID, UUID),
    public.claim_outreach_recipient(UUID, UUID, UUID),
    public.finish_outreach_attempt(UUID, UUID, UUID, TEXT, INTEGER, TEXT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.prepare_outreach_preview(UUID, TEXT, UUID, TEXT, TEXT, BOOLEAN, JSONB),
    public.submit_outreach_campaign(UUID, UUID),
    public.claim_outreach_recipient(UUID, UUID, UUID),
    public.finish_outreach_attempt(UUID, UUID, UUID, TEXT, INTEGER, TEXT, TEXT)
    TO service_role;

COMMIT;
