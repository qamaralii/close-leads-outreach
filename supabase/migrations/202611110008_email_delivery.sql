BEGIN;
CREATE TABLE public.outreach_attachments (
    id UUID PRIMARY KEY, created_by UUID NOT NULL REFERENCES auth.users(id),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200 AND name !~ '[[:cntrl:]/\\]'),
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 25000000),
    content_type TEXT NOT NULL CHECK (length(content_type) BETWEEN 1 AND 150 AND content_type !~ '[[:cntrl:]]'),
    sha256 TEXT CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ready','deleting')),
    ever_linked BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'ready' OR sha256 IS NOT NULL)
);
ALTER TABLE public.outreach_attachments ENABLE ROW LEVEL SECURITY;
CREATE POLICY attachments_owner_read ON public.outreach_attachments FOR SELECT TO authenticated
    USING (created_by=auth.uid() AND public.is_outreach_member() AND status='ready');
GRANT SELECT ON public.outreach_attachments TO authenticated,service_role;
REVOKE INSERT,UPDATE,DELETE ON public.outreach_attachments FROM anon,authenticated,service_role;

ALTER TABLE public.campaigns ADD COLUMN default_cc TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE public.campaigns ADD COLUMN default_attachment_ids UUID[] NOT NULL DEFAULT '{}';
ALTER TABLE public.campaign_recipients ADD COLUMN cc_override TEXT[];
ALTER TABLE public.campaign_recipients ADD COLUMN attachment_ids_override UUID[];

CREATE FUNCTION public.register_outreach_attachment(p_user_id UUID,p_id UUID,p_name TEXT,p_size INTEGER,p_type TEXT,p_sha256 TEXT DEFAULT NULL)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE a public.outreach_attachments%ROWTYPE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.outreach_members WHERE user_id=p_user_id AND is_active) THEN RAISE EXCEPTION 'Membership unavailable'; END IF;
    SELECT * INTO a FROM public.outreach_attachments WHERE id=p_id FOR UPDATE;
    IF FOUND THEN
        IF a.created_by<>p_user_id OR ROW(a.name,a.size_bytes,a.content_type) IS DISTINCT FROM ROW(p_name,p_size,p_type) OR a.status='deleting' THEN RAISE EXCEPTION 'Upload conflict'; END IF;
        IF p_sha256 IS NOT NULL THEN
            IF a.sha256 IS NOT NULL AND a.sha256<>p_sha256 THEN RAISE EXCEPTION 'File changed'; END IF;
            UPDATE public.outreach_attachments SET sha256=p_sha256,status='ready' WHERE id=p_id;
        END IF;
    ELSE
        IF p_sha256 IS NOT NULL THEN RAISE EXCEPTION 'Upload unavailable'; END IF;
        INSERT INTO public.outreach_attachments(id,created_by,name,size_bytes,content_type) VALUES(p_id,p_user_id,p_name,p_size,p_type);
    END IF;
    RETURN p_id;
END;
$$;

CREATE FUNCTION public.validate_outreach_delivery(p_user_id UUID,p_cc TEXT[],p_ids UUID[],p_to TEXT DEFAULT NULL,p_suppression BOOLEAN DEFAULT FALSE)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE n INTEGER; total BIGINT;
BEGIN
    IF p_cc IS NULL OR p_ids IS NULL OR cardinality(p_cc)>50 OR cardinality(p_ids)>20 THEN RAISE EXCEPTION 'Invalid delivery lists'; END IF;
    IF EXISTS (SELECT 1 FROM unnest(p_cc) x WHERE x IS NULL OR length(x)>254 OR x<>lower(btrim(x)) OR x !~ '^[A-Za-z0-9.!#$%&''*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z0-9-]+$'
       OR length(split_part(x,'@',1))>64 OR split_part(x,'@',1) ~ '^\.|\.$|\.\.'
       OR EXISTS (SELECT 1 FROM unnest(string_to_array(split_part(x,'@',2),'.')) part WHERE length(part)>63 OR part !~ '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$')) OR
       (SELECT count(DISTINCT x) FROM unnest(p_cc) x)<>cardinality(p_cc) THEN RAISE EXCEPTION 'Invalid or duplicate CC addresses'; END IF;
    IF (SELECT count(DISTINCT x) FROM unnest(p_ids) x)<>cardinality(p_ids) THEN RAISE EXCEPTION 'Invalid or duplicate attachment IDs'; END IF;
    -- Update locks and a durable link marker protect against cleanup's earlier
    -- READ COMMITTED snapshot. Once reviewed, retain source bytes for history.
    PERFORM 1 FROM public.outreach_attachments WHERE id=ANY(p_ids) ORDER BY id FOR UPDATE;
    SELECT count(*),coalesce(sum(size_bytes),0) INTO n,total FROM public.outreach_attachments WHERE id=ANY(p_ids) AND created_by=p_user_id AND status='ready';
    IF n<>cardinality(p_ids) THEN RAISE EXCEPTION 'Attachment unavailable or not owned'; END IF;
    IF total>25000000 THEN RAISE EXCEPTION 'Attachments exceed 25 MB per email'; END IF;
    IF (SELECT count(DISTINCT lower(name)) FROM public.outreach_attachments WHERE id=ANY(p_ids))<>n THEN RAISE EXCEPTION 'Attachment filenames must be distinct per email'; END IF;
    IF p_suppression AND EXISTS (SELECT 1 FROM public.email_suppressions WHERE normalized_email=ANY(p_cc) AND normalized_email IS DISTINCT FROM lower(btrim(p_to))) THEN RAISE EXCEPTION 'CC address is suppressed'; END IF;
    UPDATE public.outreach_attachments SET ever_linked=TRUE WHERE id=ANY(p_ids) AND NOT ever_linked;
END;
$$;

CREATE FUNCTION public.guard_campaign_delivery() RETURNS TRIGGER LANGUAGE plpgsql SET search_path='' AS $$
BEGIN
    IF TG_OP='UPDATE' AND OLD.status<>'draft' AND
       ROW(NEW.default_cc,NEW.default_attachment_ids) IS DISTINCT FROM ROW(OLD.default_cc,OLD.default_attachment_ids) THEN RAISE EXCEPTION 'Finalized delivery is immutable'; END IF;
    IF NEW.status='draft' AND NEW.app_request IS NOT NULL THEN
        NEW.default_cc := ARRAY(SELECT jsonb_array_elements_text(coalesce(NEW.app_request->'cc','[]')));
        NEW.default_attachment_ids := ARRAY(SELECT jsonb_array_elements_text(coalesce(NEW.app_request->'attachmentIds','[]'))::uuid);
    END IF;
    PERFORM public.validate_outreach_delivery(NEW.created_by,NEW.default_cc,NEW.default_attachment_ids);
    RETURN NEW;
END;
$$;
CREATE TRIGGER campaign_delivery_guard BEFORE INSERT OR UPDATE ON public.campaigns FOR EACH ROW EXECUTE FUNCTION public.guard_campaign_delivery();

CREATE FUNCTION public.guard_recipient_delivery() RETURNS TRIGGER LANGUAGE plpgsql SET search_path='' AS $$
DECLARE c public.campaigns%ROWTYPE;
BEGIN
    SELECT * INTO c FROM public.campaigns WHERE id=NEW.campaign_id FOR UPDATE;
    IF TG_OP='UPDATE' AND c.status<>'draft' AND ROW(NEW.cc_override,NEW.attachment_ids_override) IS DISTINCT FROM ROW(OLD.cc_override,OLD.attachment_ids_override) THEN RAISE EXCEPTION 'Finalized delivery is immutable'; END IF;
    IF TG_OP='INSERT' OR ROW(NEW.cc_override,NEW.attachment_ids_override,NEW.recipient_email) IS DISTINCT FROM ROW(OLD.cc_override,OLD.attachment_ids_override,OLD.recipient_email) THEN
        PERFORM public.validate_outreach_delivery(c.created_by,coalesce(NEW.cc_override,c.default_cc),coalesce(NEW.attachment_ids_override,c.default_attachment_ids),NEW.recipient_email,FALSE);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER recipient_delivery_guard BEFORE INSERT OR UPDATE ON public.campaign_recipients FOR EACH ROW EXECUTE FUNCTION public.guard_recipient_delivery();

CREATE FUNCTION public.check_campaign_delivery() RETURNS TRIGGER LANGUAGE plpgsql SET search_path='' AS $$
DECLARE r RECORD;
BEGIN
    IF ROW(NEW.default_cc,NEW.default_attachment_ids,NEW.status) IS DISTINCT FROM ROW(OLD.default_cc,OLD.default_attachment_ids,OLD.status) THEN
        FOR r IN SELECT * FROM public.campaign_recipients WHERE campaign_id=NEW.id LOOP
            PERFORM public.validate_outreach_delivery(NEW.created_by,coalesce(r.cc_override,NEW.default_cc),coalesce(r.attachment_ids_override,NEW.default_attachment_ids),r.recipient_email,OLD.status='draft' AND NEW.status<>'draft');
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER campaign_delivery_check AFTER UPDATE ON public.campaigns FOR EACH ROW EXECUTE FUNCTION public.check_campaign_delivery();

-- Preserve existing editing behavior, including custom text and exact retry IDs.
ALTER FUNCTION public.edit_outreach_draft(UUID,UUID,INTEGER,JSONB) RENAME TO edit_outreach_text;
REVOKE ALL ON FUNCTION public.edit_outreach_text(UUID,UUID,INTEGER,JSONB) FROM PUBLIC,anon,authenticated,service_role;
CREATE FUNCTION public.edit_outreach_draft(p_user_id UUID,p_campaign_id UUID,p_revision INTEGER,p_edit JSONB)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE c public.campaigns%ROWTYPE;
BEGIN
    IF p_edit->>'operation'<>'delivery' THEN RETURN public.edit_outreach_text(p_user_id,p_campaign_id,p_revision,p_edit); END IF;
    SELECT x.* INTO c FROM public.campaigns x JOIN public.outreach_members m ON m.user_id=x.created_by AND m.is_active WHERE x.id=p_campaign_id AND x.created_by=p_user_id FOR UPDATE OF x;
    IF NOT FOUND THEN RAISE EXCEPTION 'Campaign unavailable'; END IF;
    IF c.status<>'draft' THEN RAISE EXCEPTION 'Campaign is not a draft'; END IF;
    IF c.last_edit=p_edit THEN RETURN c.id; END IF;
    IF p_edit->>'editId' IS NULL OR c.last_edit->>'editId'=p_edit->>'editId' THEN RAISE EXCEPTION 'Edit ID conflict'; END IF;
    IF c.revision IS DISTINCT FROM p_revision THEN RAISE EXCEPTION 'Draft changed'; END IF;
    IF NOT (p_edit ? 'cc' AND p_edit ? 'attachmentIds') OR jsonb_typeof(p_edit->'cc') NOT IN ('array','null') OR jsonb_typeof(p_edit->'attachmentIds') NOT IN ('array','null') THEN RAISE EXCEPTION 'Invalid delivery override'; END IF;
    UPDATE public.campaign_recipients SET
        cc_override=CASE WHEN p_edit->'cc'='null'::jsonb THEN NULL ELSE ARRAY(SELECT jsonb_array_elements_text(p_edit->'cc')) END,
        attachment_ids_override=CASE WHEN p_edit->'attachmentIds'='null'::jsonb THEN NULL ELSE ARRAY(SELECT jsonb_array_elements_text(p_edit->'attachmentIds')::uuid) END
    WHERE campaign_id=c.id AND id=(p_edit->>'recipientId')::uuid AND status='ready';
    IF NOT FOUND THEN RAISE EXCEPTION 'Recipient unavailable'; END IF;
    UPDATE public.campaigns SET revision=revision+1,last_edit=p_edit,preview_ready_at=now() WHERE id=c.id;
    RETURN c.id;
END;
$$;

-- Preparation is repeatable and does not authorize dispatch. The job is always
-- derived from immutable database content; never from worker/browser payloads.
CREATE FUNCTION public.prepare_outreach_delivery(p_user_id UUID,p_attempt_id UUID,p_claim_token UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE c public.campaigns%ROWTYPE; r public.campaign_recipients%ROWTYPE; a public.outreach_attempts%ROWTYPE; cc TEXT[]; ids UUID[]; files JSONB;
BEGIN
    SELECT x.* INTO c FROM public.campaigns x JOIN public.campaign_recipients y ON y.campaign_id=x.id JOIN public.outreach_attempts z ON z.recipient_id=y.id JOIN public.outreach_members m ON m.user_id=x.created_by AND m.is_active WHERE z.id=p_attempt_id AND x.created_by=p_user_id FOR UPDATE OF x;
    IF NOT FOUND THEN RAISE EXCEPTION 'Attempt unavailable'; END IF;
    SELECT * INTO a FROM public.outreach_attempts WHERE id=p_attempt_id FOR UPDATE;
    SELECT * INTO r FROM public.campaign_recipients WHERE id=a.recipient_id FOR UPDATE;
    IF a.claim_token IS DISTINCT FROM p_claim_token OR r.claim_token IS DISTINCT FROM p_claim_token THEN RAISE EXCEPTION 'Claim token mismatch'; END IF;
    IF a.status<>'sending' OR r.status<>'sending' OR a.dispatch_started_at IS NOT NULL THEN RETURN jsonb_build_object('authorized',false); END IF;
    IF c.status<>'submitted' OR c.is_html THEN RAISE EXCEPTION 'Campaign is not eligible'; END IF;
    cc := ARRAY(SELECT x FROM unnest(coalesce(r.cc_override,c.default_cc)) x WHERE x<>r.normalized_email);
    ids := coalesce(r.attachment_ids_override,c.default_attachment_ids);
    PERFORM public.validate_outreach_delivery(p_user_id,cc,ids,r.recipient_email,TRUE);
    IF EXISTS (SELECT 1 FROM public.email_suppressions WHERE normalized_email=r.normalized_email) THEN RAISE EXCEPTION 'Recipient is suppressed'; END IF;
    PERFORM 1 FROM public.outlook_connections o WHERE o.id=c.sender_connection_id AND o.user_id=p_user_id AND o.status='active' AND o.composio_entity_id=c.sender_entity_snapshot AND o.composio_connected_account_id=c.sender_account_snapshot AND lower(btrim(o.mailbox_email))=lower(btrim(c.sender_mailbox_snapshot)) FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Reviewed sender is no longer available'; END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('id',f.id,'name',f.name,'size_bytes',f.size_bytes,'content_type',f.content_type,'sha256',f.sha256,'path','files/'||f.created_by||'/'||f.id) ORDER BY u.ord),'[]') INTO files FROM unnest(ids) WITH ORDINALITY u(id,ord) JOIN public.outreach_attachments f ON f.id=u.id;
    RETURN jsonb_build_object('authorized',true,'campaign_id',c.id,'recipient_id',r.id,'entity_id',c.sender_entity_snapshot,'connected_account_id',c.sender_account_snapshot,'sender_email',c.sender_mailbox_snapshot,'recipient_email',r.recipient_email,'subject',r.rendered_subject,'body',r.rendered_body,'is_html',c.is_html,'cc',cc,'attachments',files);
END;
$$;

ALTER FUNCTION public.begin_outreach_send(UUID,UUID,UUID) RENAME TO begin_outreach_dispatch;
REVOKE ALL ON FUNCTION public.begin_outreach_dispatch(UUID,UUID,UUID) FROM PUBLIC,anon,authenticated,service_role;
CREATE FUNCTION public.begin_outreach_send(p_user_id UUID,p_attempt_id UUID,p_claim_token UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE job JSONB;
BEGIN
    job := public.prepare_outreach_delivery(p_user_id,p_attempt_id,p_claim_token);
    IF job->>'authorized'='true' AND (jsonb_array_length(job->'cc')>0 OR jsonb_array_length(job->'attachments')>0) THEN RAISE EXCEPTION 'Updated delivery worker required'; END IF;
    RETURN public.begin_outreach_dispatch(p_user_id,p_attempt_id,p_claim_token);
END;
$$;
CREATE FUNCTION public.dispatch_outreach_delivery(p_user_id UUID,p_attempt_id UUID,p_claim_token UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE job JSONB; gate JSONB;
BEGIN
    job := public.prepare_outreach_delivery(p_user_id,p_attempt_id,p_claim_token);
    IF job->>'authorized'<>'true' THEN RETURN job; END IF;
    gate := public.begin_outreach_dispatch(p_user_id,p_attempt_id,p_claim_token);
    RETURN job || jsonb_build_object('authorized',gate->'authorized');
END;
$$;
CREATE FUNCTION public.pending_outreach_deliveries() RETURNS TABLE(user_id UUID,campaign_id UUID,recipient_id UUID,attempt_id UUID,claim_token UUID)
LANGUAGE sql SECURITY DEFINER SET search_path='' AS $$
    SELECT c.created_by,c.id,r.id,a.id,a.claim_token FROM public.outreach_attempts a JOIN public.campaign_recipients r ON r.id=a.recipient_id JOIN public.campaigns c ON c.id=r.campaign_id WHERE a.status='sending' AND a.dispatch_started_at IS NULL AND r.status='sending' ORDER BY a.started_at LIMIT 100;
$$;

-- Only staged and never-linked uploads are garbage-collected. A deleting record
-- can no longer be linked, even if cleanup races with a draft save.
CREATE FUNCTION public.collect_outreach_uploads() RETURNS SETOF public.outreach_attachments
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    RETURN QUERY WITH candidates AS (
        SELECT id FROM public.outreach_attachments WHERE created_at<now()-interval '24 hours' AND NOT ever_linked ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED
    ) UPDATE public.outreach_attachments a SET status='deleting'
    WHERE a.id IN (SELECT id FROM candidates) AND NOT a.ever_linked AND NOT EXISTS (SELECT 1 FROM public.campaigns c WHERE a.id=ANY(c.default_attachment_ids)) AND NOT EXISTS (SELECT 1 FROM public.campaign_recipients r WHERE a.id=ANY(r.attachment_ids_override))
    RETURNING a.*;
END;
$$;
CREATE FUNCTION public.finish_outreach_upload_cleanup(p_id UUID) RETURNS VOID
LANGUAGE sql SECURITY DEFINER SET search_path='' AS $$
    DELETE FROM public.outreach_attachments WHERE id=p_id AND status='deleting';
$$;
REVOKE ALL ON FUNCTION public.register_outreach_attachment(UUID,UUID,TEXT,INTEGER,TEXT,TEXT), public.validate_outreach_delivery(UUID,TEXT[],UUID[],TEXT,BOOLEAN), public.guard_campaign_delivery(), public.guard_recipient_delivery(), public.check_campaign_delivery(), public.edit_outreach_draft(UUID,UUID,INTEGER,JSONB), public.prepare_outreach_delivery(UUID,UUID,UUID), public.begin_outreach_send(UUID,UUID,UUID), public.dispatch_outreach_delivery(UUID,UUID,UUID), public.pending_outreach_deliveries(), public.collect_outreach_uploads() FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.finish_outreach_upload_cleanup(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.finish_outreach_upload_cleanup(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.register_outreach_attachment(UUID,UUID,TEXT,INTEGER,TEXT,TEXT), public.edit_outreach_draft(UUID,UUID,INTEGER,JSONB), public.prepare_outreach_delivery(UUID,UUID,UUID), public.begin_outreach_send(UUID,UUID,UUID), public.dispatch_outreach_delivery(UUID,UUID,UUID), public.pending_outreach_deliveries(), public.collect_outreach_uploads() TO service_role;
COMMIT;
