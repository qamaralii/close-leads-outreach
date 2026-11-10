BEGIN;
ALTER TABLE public.campaigns ADD COLUMN app_request JSONB;
ALTER TABLE public.campaigns ADD COLUMN preview_exclusions JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE FUNCTION public.prepare_app_preview(
    p_user_id UUID, p_request_id TEXT, p_sender_connection_id UUID,
    p_subject_template TEXT, p_body_template TEXT, p_recipients JSONB,
    p_app_request JSONB, p_exclusions JSONB
) RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_id UUID; v_request JSONB;
BEGIN
    PERFORM 1 FROM public.outreach_members WHERE user_id = p_user_id AND is_active;
    IF NOT FOUND THEN RAISE EXCEPTION 'Membership required'; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_user_id::text || ':' || p_request_id, 0));
    SELECT id, app_request INTO v_id, v_request FROM public.campaigns
    WHERE created_by = p_user_id AND request_id = p_request_id;
    IF FOUND THEN
        IF v_request IS DISTINCT FROM p_app_request THEN RAISE EXCEPTION 'Request ID conflict'; END IF;
        RETURN v_id;
    END IF;
    v_id := public.prepare_outreach_preview(p_user_id, p_request_id, p_sender_connection_id,
        p_subject_template, p_body_template, false, p_recipients);
    UPDATE public.campaigns SET app_request = p_app_request, preview_exclusions = p_exclusions WHERE id = v_id;
    RETURN v_id;
END;
$$;
REVOKE ALL ON FUNCTION public.prepare_app_preview(UUID,TEXT,UUID,TEXT,TEXT,JSONB,JSONB,JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.prepare_app_preview(UUID,TEXT,UUID,TEXT,TEXT,JSONB,JSONB,JSONB) TO service_role;
COMMIT;
