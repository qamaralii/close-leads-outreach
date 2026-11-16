BEGIN;
INSERT INTO storage.buckets(id,name,public,file_size_limit) VALUES ('outreach-attachments','outreach-attachments',false,25000000)
ON CONFLICT(id) DO UPDATE SET public=false,file_size_limit=25000000;
-- Browser uploads use narrowly scoped signed staging tokens. Final objects are
-- moved by the server and cannot be overwritten with those tokens.
CREATE POLICY outreach_attachment_download ON storage.objects FOR SELECT TO authenticated USING (
    bucket_id='outreach-attachments' AND EXISTS (
        SELECT 1 FROM public.outreach_attachments a WHERE a.status='ready' AND a.created_by=auth.uid()
        AND storage.objects.name='files/'||a.created_by||'/'||a.id AND public.is_outreach_member()
    )
);
COMMIT;
