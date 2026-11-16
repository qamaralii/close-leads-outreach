BEGIN;
CREATE TABLE public.close_lead_sync_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_by UUID NOT NULL REFERENCES auth.users(id), request_id UUID NOT NULL,
    last_retry_by UUID REFERENCES auth.users(id), last_retry_id UUID,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','exporting','downloading','importing','retrying','completed','failed')),
    phase TEXT NOT NULL DEFAULT 'exporting' CHECK (phase IN ('exporting','downloading','importing')),
    export_id TEXT CHECK (export_id ~ '^expo_[A-Za-z0-9]+$'),
    total_rows INTEGER CHECK (total_rows>0), rows_processed INTEGER NOT NULL DEFAULT 0 CHECK (rows_processed>=0),
    csv_sha256 TEXT CHECK (csv_sha256 ~ '^[a-f0-9]{64}$'),
    last_batch_offset INTEGER, last_batch_size INTEGER, last_batch_hash TEXT,
    failures INTEGER NOT NULL DEFAULT 0 CHECK (failures>=0), retry_at TIMESTAMPTZ,
    error TEXT CHECK (length(error)<=500),
    lease_token UUID, lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
    UNIQUE(requested_by,request_id),
    CHECK (total_rows IS NULL OR rows_processed<=total_rows),
    CHECK (status<>'completed' OR (total_rows=rows_processed AND total_rows IS NOT NULL AND finished_at IS NOT NULL))
);
CREATE UNIQUE INDEX close_sync_one_running ON public.close_lead_sync_jobs ((TRUE))
    WHERE status IN ('queued','exporting','downloading','importing','retrying');
ALTER TABLE public.close_lead_sync_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY close_sync_member_read ON public.close_lead_sync_jobs FOR SELECT TO authenticated USING (public.is_outreach_member());
REVOKE ALL ON public.close_lead_sync_jobs FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT(id,status,phase,total_rows,rows_processed,failures,error,created_at,started_at,updated_at,finished_at,synced_at,retry_at)
    ON public.close_lead_sync_jobs TO authenticated;
GRANT SELECT ON public.close_lead_sync_jobs TO service_role;
CREATE TABLE public.close_lead_sync_requests (
    user_id UUID NOT NULL REFERENCES auth.users(id), request_id UUID NOT NULL,
    job_id UUID NOT NULL REFERENCES public.close_lead_sync_jobs(id), retry_job_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(user_id,request_id)
);
ALTER TABLE public.close_lead_sync_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.close_lead_sync_requests FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.close_lead_sync_requests TO service_role;

CREATE FUNCTION public.start_close_lead_sync(p_user_id UUID,p_request_id UUID,p_retry_job_id UUID DEFAULT NULL)
RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE j public.close_lead_sync_jobs%ROWTYPE; previous public.close_lead_sync_requests%ROWTYPE;
BEGIN
    IF p_request_id IS NULL OR NOT EXISTS(SELECT 1 FROM public.outreach_members WHERE user_id=p_user_id AND is_active) THEN RAISE EXCEPTION 'Active membership required'; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(671160010);
    SELECT * INTO previous FROM public.close_lead_sync_requests WHERE user_id=p_user_id AND request_id=p_request_id;
    IF FOUND THEN
        IF previous.retry_job_id IS DISTINCT FROM p_retry_job_id THEN RAISE EXCEPTION 'Request ID conflict'; END IF;
        RETURN previous.job_id;
    END IF;
    SELECT * INTO j FROM public.close_lead_sync_jobs WHERE status IN ('queued','exporting','downloading','importing','retrying') FOR UPDATE;
    IF NOT FOUND AND p_retry_job_id IS NOT NULL THEN
        SELECT * INTO j FROM public.close_lead_sync_jobs WHERE id=p_retry_job_id AND status='failed' FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'Failed sync unavailable'; END IF;
        UPDATE public.close_lead_sync_jobs SET status='queued',failures=0,error=NULL,retry_at=NULL,finished_at=NULL,
            lease_token=NULL,lease_until=NULL,last_retry_by=p_user_id,last_retry_id=p_request_id,updated_at=clock_timestamp() WHERE id=j.id;
    ELSIF NOT FOUND THEN
        INSERT INTO public.close_lead_sync_jobs(requested_by,request_id) VALUES(p_user_id,p_request_id) RETURNING * INTO j;
    END IF;
    INSERT INTO public.close_lead_sync_requests(user_id,request_id,job_id,retry_job_id) VALUES(p_user_id,p_request_id,j.id,p_retry_job_id);
    RETURN j.id;
END;
$$;

CREATE FUNCTION public.claim_close_lead_sync() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE j public.close_lead_sync_jobs%ROWTYPE;
BEGIN
    SELECT * INTO j FROM public.close_lead_sync_jobs
    WHERE status IN ('queued','exporting','downloading','importing','retrying')
      AND (lease_until IS NULL OR lease_until<=clock_timestamp())
      AND (retry_at IS NULL OR retry_at<=clock_timestamp())
    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE public.close_lead_sync_jobs SET status=phase,lease_token=gen_random_uuid(),
        lease_until=clock_timestamp()+interval '180 seconds',started_at=coalesce(started_at,clock_timestamp()),
        synced_at=coalesce(synced_at,clock_timestamp()),updated_at=clock_timestamp(),retry_at=NULL
    WHERE id=j.id RETURNING * INTO j;
    RETURN to_jsonb(j);
END;
$$;

CREATE FUNCTION public.check_close_sync_lease(p_job_id UUID,p_token UUID) RETURNS public.close_lead_sync_jobs
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE j public.close_lead_sync_jobs%ROWTYPE;
BEGIN
    SELECT * INTO j FROM public.close_lead_sync_jobs WHERE id=p_job_id FOR UPDATE;
    IF NOT FOUND OR j.status NOT IN ('exporting','downloading','importing') OR j.lease_token IS DISTINCT FROM p_token OR p_token IS NULL OR j.lease_until IS NULL OR j.lease_until<=clock_timestamp() THEN RAISE EXCEPTION 'Sync lease lost'; END IF;
    RETURN j;
END;
$$;

CREATE FUNCTION public.update_close_lead_sync(p_job_id UUID,p_token UUID,p_operation TEXT,p_data JSONB DEFAULT '{}')
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE j public.close_lead_sync_jobs%ROWTYPE; total INTEGER; hash TEXT;
BEGIN
    j := public.check_close_sync_lease(p_job_id,p_token);
    IF p_operation='export' THEN
        IF j.phase<>'exporting' OR j.export_id IS NOT NULL AND j.export_id IS DISTINCT FROM p_data->>'export_id' OR p_data->>'export_id' IS NULL OR p_data->>'export_id' !~ '^expo_[A-Za-z0-9]+$' THEN RAISE EXCEPTION 'Invalid export checkpoint'; END IF;
        UPDATE public.close_lead_sync_jobs SET export_id=p_data->>'export_id' WHERE id=j.id;
    ELSIF p_operation='download' THEN
        IF j.export_id IS NULL OR j.phase NOT IN ('exporting','downloading') THEN RAISE EXCEPTION 'Invalid download checkpoint'; END IF;
        UPDATE public.close_lead_sync_jobs SET phase='downloading',status='downloading' WHERE id=j.id;
    ELSIF p_operation='prepared' THEN
        total := (p_data->>'total_rows')::integer; hash := p_data->>'csv_sha256';
        IF j.export_id IS NULL OR j.phase NOT IN ('downloading','importing') OR total IS NULL OR total<1 OR hash IS NULL OR hash !~ '^[a-f0-9]{64}$' THEN RAISE EXCEPTION 'Invalid export manifest'; END IF;
        IF j.csv_sha256 IS NOT NULL AND (j.csv_sha256<>hash OR j.total_rows<>total) THEN RAISE EXCEPTION 'Export changed. Start a new sync'; END IF;
        UPDATE public.close_lead_sync_jobs SET phase='importing',status='importing',total_rows=total,csv_sha256=hash WHERE id=j.id;
    ELSIF p_operation='complete' THEN
        IF j.phase<>'importing' OR j.total_rows IS NULL OR j.rows_processed<>j.total_rows THEN RAISE EXCEPTION 'Sync is incomplete'; END IF;
        UPDATE public.close_lead_sync_jobs SET status='completed',finished_at=clock_timestamp(),lease_token=NULL,lease_until=NULL,error=NULL WHERE id=j.id;
    ELSIF p_operation='failure' THEN
        UPDATE public.close_lead_sync_jobs SET failures=failures+1,
            status=CASE WHEN coalesce((p_data->>'retryable')::boolean,FALSE) AND failures<4 THEN 'retrying' ELSE 'failed' END,
            retry_at=CASE WHEN coalesce((p_data->>'retryable')::boolean,FALSE) AND failures<4 THEN clock_timestamp()+make_interval(secs=>least(300,greatest(5,coalesce((p_data->>'delay_seconds')::integer,30)))) ELSE NULL END,
            error=left(coalesce(p_data->>'error','Lead sync failed'),500),lease_token=NULL,lease_until=NULL,
            finished_at=CASE WHEN coalesce((p_data->>'retryable')::boolean,FALSE) AND failures<4 THEN NULL ELSE clock_timestamp() END WHERE id=j.id;
    ELSIF p_operation<>'heartbeat' OR p_operation IS NULL THEN RAISE EXCEPTION 'Invalid sync operation';
    END IF;
    UPDATE public.close_lead_sync_jobs SET updated_at=clock_timestamp(),
        lease_until=CASE WHEN status IN ('exporting','downloading','importing') THEN clock_timestamp()+interval '180 seconds' ELSE lease_until END
    WHERE id=j.id RETURNING * INTO j;
    RETURN to_jsonb(j);
END;
$$;

CREATE FUNCTION public.import_close_lead_batch(p_job_id UUID,p_token UUID,p_offset INTEGER,p_rows JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE j public.close_lead_sync_jobs%ROWTYPE; n INTEGER; hash TEXT;
BEGIN
    j := public.check_close_sync_lease(p_job_id,p_token);
    IF j.phase<>'importing' OR j.csv_sha256 IS NULL OR jsonb_typeof(p_rows) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'Invalid import batch'; END IF;
    n := jsonb_array_length(p_rows); hash := md5(p_rows::text);
    IF n<1 OR n>1000 OR p_offset IS NULL OR p_offset<0 THEN RAISE EXCEPTION 'Invalid import batch'; END IF;
    IF j.last_batch_offset=p_offset AND j.last_batch_size=n AND j.last_batch_hash=hash AND j.rows_processed=p_offset+n THEN RETURN to_jsonb(j); END IF;
    IF p_offset<>j.rows_processed OR p_offset+n>j.total_rows THEN RAISE EXCEPTION 'Import checkpoint changed'; END IF;
    IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_rows) x WHERE jsonb_typeof(x) IS DISTINCT FROM 'object' OR x->>'close_lead_id' IS NULL OR btrim(x->>'close_lead_id')='' OR jsonb_typeof(x->'source_data') IS DISTINCT FROM 'object' OR btrim(x->'source_data'->>'id') IS DISTINCT FROM x->>'close_lead_id') OR
       (SELECT count(DISTINCT x->>'close_lead_id') FROM jsonb_array_elements(p_rows) x)<>n THEN RAISE EXCEPTION 'Invalid or duplicate Close lead IDs'; END IF;
    INSERT INTO public.close_leads(close_lead_id,company_name,company_url,contact_name,first_name,last_name,contact_title,email,lead_status,industry,source_data,last_synced_at)
    SELECT x.close_lead_id,x.company_name,x.company_url,x.contact_name,x.first_name,x.last_name,x.contact_title,x.email,x.lead_status,x.industry,x.source_data,j.synced_at
    FROM jsonb_to_recordset(p_rows) AS x(close_lead_id TEXT,company_name TEXT,company_url TEXT,contact_name TEXT,first_name TEXT,last_name TEXT,contact_title TEXT,email TEXT,lead_status TEXT,industry TEXT,source_data JSONB)
    ON CONFLICT(close_lead_id) DO UPDATE SET company_name=excluded.company_name,company_url=excluded.company_url,
        contact_name=excluded.contact_name,first_name=excluded.first_name,last_name=excluded.last_name,contact_title=excluded.contact_title,
        email=excluded.email,lead_status=excluded.lead_status,industry=excluded.industry,source_data=excluded.source_data,last_synced_at=excluded.last_synced_at;
    UPDATE public.close_lead_sync_jobs SET rows_processed=p_offset+n,last_batch_offset=p_offset,last_batch_size=n,last_batch_hash=hash,
        updated_at=clock_timestamp(),lease_until=clock_timestamp()+interval '180 seconds',error=NULL WHERE id=j.id RETURNING * INTO j;
    RETURN to_jsonb(j);
END;
$$;
REVOKE ALL ON FUNCTION public.start_close_lead_sync(UUID,UUID,UUID), public.claim_close_lead_sync(), public.check_close_sync_lease(UUID,UUID), public.update_close_lead_sync(UUID,UUID,TEXT,JSONB), public.import_close_lead_batch(UUID,UUID,INTEGER,JSONB) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.start_close_lead_sync(UUID,UUID,UUID), public.claim_close_lead_sync(), public.update_close_lead_sync(UUID,UUID,TEXT,JSONB), public.import_close_lead_batch(UUID,UUID,INTEGER,JSONB) TO service_role;
REVOKE ALL ON FUNCTION public.check_close_sync_lease(UUID,UUID) FROM service_role;
COMMIT;
