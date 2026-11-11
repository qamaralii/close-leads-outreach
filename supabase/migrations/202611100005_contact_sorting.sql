BEGIN;

-- Close exports ISO 8601 strings. Parse offsets as instants, not lexical text.
-- Missing or malformed source values should sort last, not break the directory.
CREATE FUNCTION public.close_creation_timestamp(value TEXT)
RETURNS TIMESTAMPTZ LANGUAGE plpgsql STABLE STRICT SET search_path = '' AS $$
BEGIN
    IF btrim(value) !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$' THEN
        RETURN NULL;
    END IF;
    RETURN btrim(value)::timestamptz;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION public.close_creation_timestamp(TEXT) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.close_creation_timestamp(TEXT) TO authenticated, service_role;

-- PostgreSQL 15+: use the caller's permissions/RLS, never the view owner's.
CREATE VIEW public.close_contact_directory WITH (security_invoker = true) AS
SELECT close_lead_id, contact_name, company_name, email,
    nullif(lower(btrim(contact_name)), '') AS name_sort,
    nullif(lower(btrim(company_name)), '') AS company_sort,
    public.close_creation_timestamp(source_data->>'date_created') AS close_created_at
FROM public.close_leads;
REVOKE ALL ON public.close_contact_directory FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.close_contact_directory TO authenticated, service_role;

NOTIFY pgrst, 'reload schema';
COMMIT;
