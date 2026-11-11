BEGIN;
ALTER TABLE public.campaigns DROP CONSTRAINT campaigns_status_check;
ALTER TABLE public.campaigns DROP CONSTRAINT campaigns_send_mode_check;
ALTER TABLE public.campaigns DROP CONSTRAINT campaigns_scheduled_at_check;
ALTER TABLE public.campaigns ADD CONSTRAINT campaigns_status_check CHECK (status IN ('draft','scheduled','submitted'));
ALTER TABLE public.campaigns ADD CONSTRAINT campaigns_send_mode_check CHECK (send_mode IN ('now','scheduled'));
ALTER TABLE public.campaigns ADD CONSTRAINT campaigns_schedule_check CHECK (
    (send_mode='now' AND scheduled_at IS NULL AND timezone IS NULL AND status <> 'scheduled') OR
    (send_mode='scheduled' AND scheduled_at IS NOT NULL AND timezone IS NOT NULL AND status IN ('scheduled','submitted')));
ALTER TABLE public.campaigns ADD CONSTRAINT campaigns_scheduled_submission_check CHECK (status <> 'scheduled' OR submitted_at IS NULL);
ALTER TABLE public.campaigns ADD COLUMN last_schedule JSONB;

CREATE OR REPLACE FUNCTION public.guard_campaign_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF ROW(NEW.created_by,NEW.request_id,NEW.sender_connection_id,NEW.sender_mailbox_snapshot,NEW.sender_entity_snapshot,NEW.sender_account_snapshot)
       IS DISTINCT FROM ROW(OLD.created_by,OLD.request_id,OLD.sender_connection_id,OLD.sender_mailbox_snapshot,OLD.sender_entity_snapshot,OLD.sender_account_snapshot) THEN
        RAISE EXCEPTION 'Campaign ownership and sender are immutable';
    END IF;
    IF OLD.status='submitted' AND ROW(NEW.status,NEW.subject_template,NEW.body_template,NEW.is_html,NEW.expected_recipient_count,
        NEW.send_mode,NEW.scheduled_at,NEW.timezone,NEW.preview_ready_at,NEW.submitted_at,NEW.app_request,NEW.preview_exclusions,NEW.revision,NEW.last_edit,NEW.last_schedule)
       IS DISTINCT FROM ROW(OLD.status,OLD.subject_template,OLD.body_template,OLD.is_html,OLD.expected_recipient_count,
        OLD.send_mode,OLD.scheduled_at,OLD.timezone,OLD.preview_ready_at,OLD.submitted_at,OLD.app_request,OLD.preview_exclusions,OLD.revision,OLD.last_edit,OLD.last_schedule) THEN
        RAISE EXCEPTION 'Submitted campaign is immutable';
    END IF;
    IF OLD.status='scheduled' THEN
        IF ROW(NEW.subject_template,NEW.body_template,NEW.is_html,NEW.expected_recipient_count,NEW.preview_ready_at,NEW.app_request,NEW.preview_exclusions,NEW.last_edit)
           IS DISTINCT FROM ROW(OLD.subject_template,OLD.body_template,OLD.is_html,OLD.expected_recipient_count,OLD.preview_ready_at,OLD.app_request,OLD.preview_exclusions,OLD.last_edit) THEN
            RAISE EXCEPTION 'Scheduled content is immutable. Return to draft first';
        END IF;
        IF OLD.scheduled_at <= clock_timestamp() AND
           (NEW.status <> 'submitted' OR ROW(NEW.send_mode,NEW.scheduled_at,NEW.timezone,NEW.revision,NEW.last_schedule)
            IS DISTINCT FROM ROW(OLD.send_mode,OLD.scheduled_at,OLD.timezone,OLD.revision,OLD.last_schedule)) THEN
            RAISE EXCEPTION 'Schedule is due and locked';
        END IF;
    END IF;
    IF NEW.status IN ('draft','scheduled') AND NEW.submitted_at IS NOT NULL THEN
        RAISE EXCEPTION 'Unstarted campaign cannot have a submission time';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.manage_outreach_schedule(
    p_user_id UUID,p_campaign_id UUID,p_revision INTEGER,p_operation_id UUID,p_operation TEXT,
    p_scheduled_at TIMESTAMPTZ DEFAULT NULL,p_timezone TEXT DEFAULT NULL
) RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE c public.campaigns%ROWTYPE; payload JSONB;
BEGIN
    SELECT x.* INTO c FROM public.campaigns x JOIN public.outreach_members m ON m.user_id=x.created_by AND m.is_active
    WHERE x.id=p_campaign_id AND x.created_by=p_user_id FOR UPDATE OF x;
    IF NOT FOUND THEN RAISE EXCEPTION 'Campaign unavailable'; END IF;
    IF p_operation_id IS NULL OR p_operation IS NULL OR p_operation NOT IN ('schedule','reschedule','withdraw') THEN RAISE EXCEPTION 'Invalid schedule operation'; END IF;
    payload := jsonb_build_object('id',p_operation_id,'operation',p_operation,'at',extract(epoch FROM p_scheduled_at),'timezone',p_timezone,'revision',p_revision);
    IF c.last_schedule=payload AND c.revision=p_revision+1 THEN RETURN c.id; END IF;
    IF c.revision IS DISTINCT FROM p_revision THEN RAISE EXCEPTION 'Campaign changed. Refresh and review'; END IF;
    IF c.last_schedule->>'id'=p_operation_id::text THEN RAISE EXCEPTION 'Operation ID conflict'; END IF;
    IF c.status='submitted' THEN RAISE EXCEPTION 'Campaign already started'; END IF;
    IF c.status='scheduled' AND c.scheduled_at <= clock_timestamp() THEN RAISE EXCEPTION 'Schedule is due and locked'; END IF;
    IF p_operation='withdraw' THEN
        IF c.status <> 'scheduled' OR p_scheduled_at IS NOT NULL OR p_timezone IS NOT NULL THEN RAISE EXCEPTION 'Campaign is not scheduled'; END IF;
        IF EXISTS (SELECT 1 FROM public.campaign_recipients r WHERE r.campaign_id=c.id AND r.status <> 'queued') THEN RAISE EXCEPTION 'Campaign already started'; END IF;
        UPDATE public.campaigns SET status='draft',send_mode='now',scheduled_at=NULL,timezone=NULL,revision=revision+1,last_schedule=payload WHERE id=c.id;
        UPDATE public.campaign_recipients SET status='ready',due_at=NULL WHERE campaign_id=c.id;
        RETURN c.id;
    END IF;
    IF (p_operation='schedule' AND c.status <> 'draft') OR (p_operation='reschedule' AND c.status <> 'scheduled') THEN RAISE EXCEPTION 'Invalid campaign state'; END IF;
    IF p_scheduled_at IS NULL OR NOT isfinite(p_scheduled_at) OR p_scheduled_at <= clock_timestamp() THEN RAISE EXCEPTION 'Choose a future send time'; END IF;
    IF p_timezone IS NULL OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name=p_timezone) THEN RAISE EXCEPTION 'Invalid time zone'; END IF;
    IF c.preview_ready_at IS NULL OR (SELECT count(*) FROM public.campaign_recipients r WHERE r.campaign_id=c.id AND r.status=CASE WHEN c.status='draft' THEN 'ready' ELSE 'queued' END) <> c.expected_recipient_count THEN
        RAISE EXCEPTION 'Preview recipients are incomplete';
    END IF;
    PERFORM 1 FROM public.outlook_connections o WHERE o.id=c.sender_connection_id AND o.user_id=p_user_id AND o.status='active'
        AND o.composio_entity_id=c.sender_entity_snapshot AND o.composio_connected_account_id=c.sender_account_snapshot
        AND lower(btrim(o.mailbox_email))=lower(btrim(c.sender_mailbox_snapshot)) FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Reviewed sender is no longer available'; END IF;
    UPDATE public.campaigns SET status='scheduled',send_mode='scheduled',scheduled_at=p_scheduled_at,timezone=p_timezone,
        revision=revision+1,last_schedule=payload WHERE id=c.id;
    UPDATE public.campaign_recipients SET status='queued',due_at=p_scheduled_at WHERE campaign_id=c.id;
    RETURN c.id;
END;
$$;

-- Retain the existing claim, suppression and sender checks behind a due-time gate.
ALTER FUNCTION public.claim_outreach_recipient(UUID,UUID,UUID) RENAME TO claim_started_outreach_recipient;
REVOKE ALL ON FUNCTION public.claim_started_outreach_recipient(UUID,UUID,UUID) FROM PUBLIC,anon,authenticated,service_role;
CREATE FUNCTION public.claim_outreach_recipient(p_user_id UUID,p_campaign_id UUID,p_recipient_id UUID)
RETURNS TABLE(recipient_id UUID,attempt_id UUID,claim_token UUID,entity_id TEXT,connected_account_id TEXT,
    sender_email TEXT,recipient_email TEXT,subject TEXT,body TEXT,is_html BOOLEAN)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE c public.campaigns%ROWTYPE;
BEGIN
    SELECT x.* INTO c FROM public.campaigns x JOIN public.outreach_members m ON m.user_id=x.created_by AND m.is_active
    WHERE x.id=p_campaign_id AND x.created_by=p_user_id FOR UPDATE OF x;
    IF NOT FOUND THEN RAISE EXCEPTION 'Campaign unavailable'; END IF;
    IF c.status='scheduled' THEN
        IF c.scheduled_at > clock_timestamp() THEN RETURN; END IF;
        IF NOT EXISTS (SELECT 1 FROM public.campaign_recipients r WHERE r.id=p_recipient_id AND r.campaign_id=c.id AND r.status='queued' AND r.due_at <= NOW()) THEN RETURN; END IF;
        UPDATE public.campaigns SET status='submitted',submitted_at=NOW() WHERE id=c.id;
    END IF;
    RETURN QUERY SELECT * FROM public.claim_started_outreach_recipient(p_user_id,p_campaign_id,p_recipient_id);
END;
$$;
REVOKE ALL ON FUNCTION public.manage_outreach_schedule(UUID,UUID,INTEGER,UUID,TEXT,TIMESTAMPTZ,TEXT),public.claim_outreach_recipient(UUID,UUID,UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.manage_outreach_schedule(UUID,UUID,INTEGER,UUID,TEXT,TIMESTAMPTZ,TEXT),public.claim_outreach_recipient(UUID,UUID,UUID) TO service_role;
COMMIT;
