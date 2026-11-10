-- Apply after 202611100001. Existing claimed attempts are eligible for one dispatch.
BEGIN;
ALTER TABLE public.outreach_attempts ADD COLUMN dispatch_started_at TIMESTAMPTZ;

CREATE FUNCTION public.begin_outreach_send(p_user_id UUID, p_attempt_id UUID, p_claim_token UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_campaign_id UUID;
    v_campaign public.campaigns%ROWTYPE;
    v_recipient public.campaign_recipients%ROWTYPE;
    v_attempt public.outreach_attempts%ROWTYPE;
BEGIN
    SELECT c.id INTO v_campaign_id FROM public.outreach_attempts a
    JOIN public.campaign_recipients r ON r.id = a.recipient_id
    JOIN public.campaigns c ON c.id = r.campaign_id
    JOIN public.outreach_members m ON m.user_id = c.created_by AND m.is_active
    WHERE a.id = p_attempt_id AND c.created_by = p_user_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Attempt unavailable'; END IF;
    -- Same parent -> recipient -> attempt lock order as claim and finish.
    SELECT * INTO v_campaign FROM public.campaigns WHERE id = v_campaign_id FOR UPDATE;
    SELECT r.* INTO v_recipient FROM public.campaign_recipients r
    JOIN public.outreach_attempts a ON a.recipient_id = r.id
    WHERE a.id = p_attempt_id FOR UPDATE OF r;
    SELECT * INTO v_attempt FROM public.outreach_attempts WHERE id = p_attempt_id FOR UPDATE;
    IF v_attempt.claim_token IS DISTINCT FROM p_claim_token
       OR v_recipient.claim_token IS DISTINCT FROM p_claim_token THEN
        RAISE EXCEPTION 'Claim token mismatch';
    END IF;
    IF v_attempt.status <> 'sending' OR v_recipient.status <> 'sending'
       OR v_attempt.dispatch_started_at IS NOT NULL THEN
        RETURN jsonb_build_object('authorized', false, 'attempt_status', v_attempt.status,
            'dispatch_started_at', v_attempt.dispatch_started_at);
    END IF;
    IF v_campaign.status <> 'submitted' OR v_campaign.is_html THEN
        RAISE EXCEPTION 'Campaign is not an eligible plain-text submission';
    END IF;
    IF EXISTS (SELECT 1 FROM public.email_suppressions
               WHERE normalized_email = v_recipient.normalized_email) THEN
        RAISE EXCEPTION 'Recipient is suppressed';
    END IF;
    PERFORM 1 FROM public.outlook_connections o
    WHERE o.id = v_campaign.sender_connection_id AND o.user_id = p_user_id AND o.status = 'active'
      AND o.composio_entity_id = v_campaign.sender_entity_snapshot
      AND o.composio_connected_account_id = v_campaign.sender_account_snapshot
      AND lower(btrim(o.mailbox_email)) = lower(btrim(v_campaign.sender_mailbox_snapshot)) FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Reviewed sender is no longer available'; END IF;
    UPDATE public.outreach_attempts SET dispatch_started_at = NOW() WHERE id = p_attempt_id;
    RETURN jsonb_build_object('authorized', true, 'campaign_id', v_campaign.id,
        'recipient_id', v_recipient.id, 'entity_id', v_campaign.sender_entity_snapshot,
        'connected_account_id', v_campaign.sender_account_snapshot,
        'sender_email', v_campaign.sender_mailbox_snapshot,
        'recipient_email', v_recipient.recipient_email, 'subject', v_recipient.rendered_subject,
        'body', v_recipient.rendered_body, 'is_html', v_campaign.is_html);
END;
$$;
REVOKE ALL ON FUNCTION public.begin_outreach_send(UUID, UUID, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.begin_outreach_send(UUID, UUID, UUID) TO service_role;
COMMIT;
