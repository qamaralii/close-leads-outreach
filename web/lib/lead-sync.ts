export const syncJobFields = "id,status,phase,total_rows,rows_processed,failures,error,created_at,started_at,updated_at,finished_at,synced_at,retry_at";
export type SyncJob = {
  id: string; status: "queued" | "exporting" | "downloading" | "importing" | "retrying" | "completed" | "failed";
  phase: string; total_rows: number | null; rows_processed: number; failures: number; error: string | null;
  created_at: string; started_at: string | null; updated_at: string; finished_at: string | null; synced_at: string | null; retry_at: string | null;
};
export function syncRunning(job: SyncJob | null) { return !!job && job.status !== "completed" && job.status !== "failed"; }
export function syncLabel(job: SyncJob) {
  switch (job.status) {
    case "queued": return "Waiting for sync worker…";
    case "exporting": return "Preparing Close export…";
    case "downloading": return "Downloading and validating leads…";
    case "importing": return `${job.rows_processed.toLocaleString()} of ${job.total_rows?.toLocaleString() ?? "…"} leads refreshed`;
    case "retrying": return "Sync interrupted. Retrying shortly…";
    case "completed": return `${job.rows_processed.toLocaleString()} leads refreshed`;
    case "failed": return "Sync failed";
  }
}
