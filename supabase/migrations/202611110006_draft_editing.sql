BEGIN;
ALTER TABLE public.campaigns ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);
ALTER TABLE public.campaigns ADD COLUMN last_edit JSONB;
ALTER TABLE public.campaign_recipients ADD COLUMN customized BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.campaign_recipients ADD COLUMN template_subject TEXT;
ALTER TABLE public.campaign_recipients ADD COLUMN template_body TEXT;
UPDATE public.campaign_recipients SET template_subject = rendered_subject, template_body = rendered_body;
ALTER TABLE public.campaign_recipients ALTER COLUMN template_subject SET NOT NULL;
ALTER TABLE public.campaign_recipients ALTER COLUMN template_body SET NOT NULL;
-- Initial preview inserts also populate the reset-to-template snapshot.
CREATE OR REPLACE FUNCTION public.guard_recipient_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE v_status TEXT;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.campaign_id IS DISTINCT FROM OLD.campaign_id THEN
        RAISE EXCEPTION 'Recipient campaign cannot change';
    END IF;
    SELECT status INTO v_status FROM public.campaigns
    WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.campaign_id ELSE NEW.campaign_id END FOR UPDATE;
    IF v_status <> 'draft' THEN
        IF TG_OP IN ('INSERT', 'DELETE') THEN RAISE EXCEPTION 'Finalized recipient set is immutable'; END IF;
        IF ROW(NEW.close_lead_id, NEW.recipient_email, NEW.recipient_name, NEW.company_name,
               NEW.rendered_subject, NEW.rendered_body, NEW.step_number, NEW.customized, NEW.template_subject, NEW.template_body)
           IS DISTINCT FROM ROW(OLD.close_lead_id, OLD.recipient_email, OLD.recipient_name, OLD.company_name,
               OLD.rendered_subject, OLD.rendered_body, OLD.step_number, OLD.customized, OLD.template_subject, OLD.template_body) THEN
            RAISE EXCEPTION 'Finalized recipient snapshot is immutable';
        END IF;
    END IF;
    IF TG_OP = 'INSERT' THEN
        NEW.template_subject := COALESCE(NEW.template_subject, NEW.rendered_subject);
        NEW.template_body := COALESCE(NEW.template_body, NEW.rendered_body);
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION public.guard_campaign_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF ROW(NEW.created_by, NEW.request_id, NEW.sender_connection_id, NEW.sender_mailbox_snapshot,
           NEW.sender_entity_snapshot, NEW.sender_account_snapshot)
       IS DISTINCT FROM ROW(OLD.created_by, OLD.request_id, OLD.sender_connection_id, OLD.sender_mailbox_snapshot,
           OLD.sender_entity_snapshot, OLD.sender_account_snapshot) THEN
        RAISE EXCEPTION 'Campaign ownership and sender are immutable';
    END IF;
    IF OLD.status = 'submitted' AND ROW(NEW.status, NEW.subject_template, NEW.body_template, NEW.is_html,
        NEW.expected_recipient_count, NEW.send_mode, NEW.scheduled_at, NEW.timezone, NEW.preview_ready_at,
        NEW.submitted_at, NEW.app_request, NEW.preview_exclusions, NEW.revision, NEW.last_edit)
       IS DISTINCT FROM ROW(OLD.status, OLD.subject_template, OLD.body_template, OLD.is_html,
        OLD.expected_recipient_count, OLD.send_mode, OLD.scheduled_at, OLD.timezone, OLD.preview_ready_at,
        OLD.submitted_at, OLD.app_request, OLD.preview_exclusions, OLD.revision, OLD.last_edit) THEN
        RAISE EXCEPTION 'Submitted campaign is immutable';
    END IF;
    IF NEW.status = 'draft' AND NEW.submitted_at IS NOT NULL THEN
        RAISE EXCEPTION 'Draft campaign cannot have a submission time';
    END IF;
    RETURN NEW;
END;
$$;

-- One parent lock serializes all edits with confirmation. Exact retries are no-ops.
CREATE FUNCTION public.edit_outreach_draft(p_user_id UUID, p_campaign_id UUID, p_revision INTEGER, p_edit JSONB)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE c public.campaigns%ROWTYPE; r JSONB; n INTEGER;
BEGIN
    SELECT x.* INTO c FROM public.campaigns x JOIN public.outreach_members m ON m.user_id=x.created_by AND m.is_active
    WHERE x.id=p_campaign_id AND x.created_by=p_user_id FOR UPDATE OF x;
    IF NOT FOUND THEN RAISE EXCEPTION 'Campaign unavailable'; END IF;
    IF c.status <> 'draft' THEN RAISE EXCEPTION 'Campaign already submitted'; END IF;
    IF jsonb_typeof(p_edit) IS DISTINCT FROM 'object' OR p_edit->>'editId' IS NULL THEN RAISE EXCEPTION 'Invalid edit'; END IF;
    IF c.last_edit = p_edit THEN RETURN c.id; END IF;
    IF c.last_edit->>'editId' = p_edit->>'editId' THEN RAISE EXCEPTION 'Edit ID conflict'; END IF;
    IF c.revision IS DISTINCT FROM p_revision THEN RAISE EXCEPTION 'Draft changed. Refresh before editing or confirming'; END IF;
    IF p_edit->>'operation' = 'campaign' THEN
        r := p_edit->'recipients';
        IF jsonb_typeof(r) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'Invalid recipients'; END IF;
        n := jsonb_array_length(r);
        IF n < 1 OR n > 1000 OR btrim(p_edit->'request'->>'subject') = '' OR btrim(p_edit->'request'->>'body') = '' THEN
            RAISE EXCEPTION 'Invalid campaign';
        END IF;
        DELETE FROM public.campaign_recipients old WHERE old.campaign_id=c.id AND NOT EXISTS (
            SELECT 1 FROM jsonb_to_recordset(r) AS x(close_lead_id TEXT, recipient_email TEXT)
            WHERE x.close_lead_id=old.close_lead_id AND x.recipient_email=old.recipient_email);
        UPDATE public.campaign_recipients old SET recipient_name=x.recipient_name, company_name=x.company_name,
            template_subject=x.rendered_subject, template_body=x.rendered_body,
            rendered_subject=CASE WHEN old.customized THEN old.rendered_subject ELSE x.rendered_subject END,
            rendered_body=CASE WHEN old.customized THEN old.rendered_body ELSE x.rendered_body END
        FROM jsonb_to_recordset(r) AS x(close_lead_id TEXT, recipient_email TEXT, recipient_name TEXT,
            company_name TEXT, rendered_subject TEXT, rendered_body TEXT)
        WHERE old.campaign_id=c.id AND old.close_lead_id=x.close_lead_id AND old.recipient_email=x.recipient_email;
        INSERT INTO public.campaign_recipients(campaign_id,close_lead_id,recipient_email,recipient_name,company_name,rendered_subject,rendered_body)
        SELECT c.id,x.close_lead_id,x.recipient_email,x.recipient_name,x.company_name,x.rendered_subject,x.rendered_body
        FROM jsonb_to_recordset(r) AS x(close_lead_id TEXT,recipient_email TEXT,recipient_name TEXT,company_name TEXT,rendered_subject TEXT,rendered_body TEXT)
        WHERE NOT EXISTS (SELECT 1 FROM public.campaign_recipients old WHERE old.campaign_id=c.id AND old.close_lead_id=x.close_lead_id AND old.recipient_email=x.recipient_email);
        IF (SELECT count(*) FROM public.campaign_recipients WHERE campaign_id=c.id AND status='ready') <> n THEN
            RAISE EXCEPTION 'Invalid recipient set';
        END IF;
        UPDATE public.campaigns SET subject_template=p_edit->'request'->>'subject', body_template=p_edit->'request'->>'body',
            app_request=p_edit->'request', preview_exclusions=p_edit->'exclusions', expected_recipient_count=n WHERE id=c.id;
    ELSIF p_edit->>'operation' IN ('message','reset') THEN
        UPDATE public.campaign_recipients SET
            rendered_subject=CASE WHEN p_edit->>'operation'='reset' THEN template_subject ELSE p_edit->>'subject' END,
            rendered_body=CASE WHEN p_edit->>'operation'='reset' THEN template_body ELSE p_edit->>'body' END,
            customized=p_edit->>'operation'='message'
        WHERE campaign_id=c.id AND id=(p_edit->>'recipientId')::uuid AND status='ready';
        IF NOT FOUND THEN RAISE EXCEPTION 'Recipient unavailable'; END IF;
    ELSE RAISE EXCEPTION 'Invalid operation';
    END IF;
    UPDATE public.campaigns SET revision=revision+1, last_edit=p_edit, preview_ready_at=now() WHERE id=c.id;
    RETURN c.id;
END;
$$;
CREATE FUNCTION public.confirm_outreach_revision(p_user_id UUID, p_campaign_id UUID, p_revision INTEGER)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_revision INTEGER;
BEGIN
    SELECT c.revision INTO v_revision FROM public.campaigns c JOIN public.outreach_members m ON m.user_id=c.created_by AND m.is_active
    WHERE c.id=p_campaign_id AND c.created_by=p_user_id FOR UPDATE OF c;
    IF NOT FOUND THEN RAISE EXCEPTION 'Campaign unavailable'; END IF;
    IF v_revision IS DISTINCT FROM p_revision THEN RAISE EXCEPTION 'Draft changed. Refresh and review before confirming'; END IF;
    RETURN public.submit_outreach_campaign(p_user_id,p_campaign_id);
END;
$$;
REVOKE ALL ON FUNCTION public.edit_outreach_draft(UUID,UUID,INTEGER,JSONB), public.confirm_outreach_revision(UUID,UUID,INTEGER) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.edit_outreach_draft(UUID,UUID,INTEGER,JSONB), public.confirm_outreach_revision(UUID,UUID,INTEGER) TO service_role;
REVOKE EXECUTE ON FUNCTION public.submit_outreach_campaign(UUID,UUID) FROM service_role;
COMMIT;
