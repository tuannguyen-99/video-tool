import { useMemo, useState } from "react";
import {
  cancelJob,
  deleteAllFinishedJobs,
  deleteJob,
  downloadUrl,
} from "../lib/api";
import { JobState, Platform, StageName } from "../types";

const SPLIT_MINUTES_STORAGE_KEY = "douyin-vietsub:splitVideoMinutes";

const STAGE_LABELS: Record<StageName, string> = {
  queued: "Đang chờ trong hàng đợi",
  download: "Đang tải video",
  speech_to_text: "Đang nhận dạng giọng nói",
  translate: "Đang dịch phụ đề",
  text_to_speech: "Đang tạo giọng đọc tiếng Việt",
  export: "Đang ghép & xuất video",
  done: "Hoàn tất",
};

const PLATFORM_LABELS: Record<Platform, string> = {
  douyin: "Douyin",
  bilibili: "Bilibili",
  local: "Video tải sẵn",
};

const STATUS_STYLES: Record<JobState["status"], string> = {
  pending: "bg-ink-700 text-ink-300",
  running: "bg-amber-500/15 text-amber-300",
  success: "bg-emerald-500/15 text-emerald-300",
  failed: "bg-red-500/15 text-red-300",
  cancelled: "bg-ink-700 text-ink-400",
};

const STATUS_LABELS: Record<JobState["status"], string> = {
  pending: "Đang chờ",
  running: "Đang xử lý",
  success: "Thành công",
  failed: "Thất bại",
  cancelled: "Đã hủy",
};

const FINISHED_STATUSES: JobState["status"][] = [
  "success",
  "failed",
  "cancelled",
];

function StatusPill({ status }: { status: JobState["status"] }) {
  return (
    <span
      className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}
    >
      {STATUS_LABELS[status]}
    </span>
  );
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-700">
      <div
        className="h-full rounded-full bg-emerald-500 transition-all"
        style={{ width: `${value}%` }}
      />
    </div>
  );
}

interface JobRowProps {
  job: JobState;
  onDeleted: (jobId: string) => void;
  onError: (message: string) => void;
}

function JobRow({ job, onDeleted, onError }: JobRowProps) {
  const [cancelling, setCancelling] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const canCancel = job.status === "pending" || job.status === "running";
  const canDelete = FINISHED_STATUSES.includes(job.status);
  const pct = Math.max(0, Math.min(1, job.progress)) * 100;

  const files =
    job.output_files && job.output_files.length > 0
      ? job.output_files
      : job.output_filename
        ? [{ filename: job.output_filename }]
        : [];

  async function handleCancel() {
    setCancelling(true);
    try {
      await cancelJob(job.id);
      // No optimistic update needed — the websocket pushes the real
      // "cancelled" status once the backend applies it.
    } catch (err) {
      onError(err instanceof Error ? err.message : "Không thể hủy tác vụ.");
    } finally {
      setCancelling(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    try {
      await deleteJob(job.id);
      onDeleted(job.id);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Không thể xóa tác vụ.");
      setDeleting(false);
    }
  }

  return (
    <li className="rounded border border-ink-700 bg-ink-800/40 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded bg-ink-700 px-2 py-0.5 text-xs font-medium text-ink-200">
            {PLATFORM_LABELS[job.platform] ?? job.platform}
          </span>
          <StatusPill status={job.status} />
          {job.split_long_video && (
            <span className="rounded bg-ink-700 px-2 py-0.5 text-xs text-ink-400">
              Cắt đoạn{" "}
              {localStorage.getItem(SPLIT_MINUTES_STORAGE_KEY)
                ? localStorage.getItem(SPLIT_MINUTES_STORAGE_KEY)
                : "10"}{" "}
              phút
            </span>
          )}
          {job.match_voice_gender && (
            <span className="rounded bg-ink-700 px-2 py-0.5 text-xs text-ink-400">
              {job.detected_voice_gender === "male"
                ? "Giọng: Nam"
                : job.detected_voice_gender === "female"
                  ? "Giọng: Nữ"
                  : "Giọng theo video gốc"}
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          {canCancel && (
            <button
              type="button"
              onClick={handleCancel}
              disabled={cancelling}
              className="text-xs font-medium text-red-300 transition-colors hover:text-red-200 disabled:opacity-50"
            >
              {cancelling ? "Đang hủy…" : "Hủy"}
            </button>
          )}
          {canDelete && (
            <button
              type="button"
              onClick={handleDelete}
              disabled={deleting}
              title="Xóa khỏi danh sách"
              className="text-xs font-medium text-ink-400 transition-colors hover:text-red-300 disabled:opacity-50"
            >
              {deleting ? "Đang xóa…" : "Xóa"}
            </button>
          )}
        </div>
      </div>

      {job.title_vi ? (
        <>
          <p
            className="mt-2 truncate text-sm font-medium text-ink-100"
            title={job.title_vi}
          >
            {job.title_vi}
          </p>
          <p className="truncate text-xs text-ink-500" title={job.url}>
            {job.url}
          </p>
        </>
      ) : (
        <p className="mt-2 truncate text-sm text-ink-300" title={job.url}>
          {job.url}
        </p>
      )}

      {(job.status === "pending" || job.status === "running") && (
        <div className="mt-2 grid gap-1.5">
          <p className="text-sm text-ink-400">
            {job.platform === "local" && job.stage === "download"
              ? "Đang xử lý file đã tải lên"
              : (STAGE_LABELS[job.stage] ?? job.stage)}
            {pct ? ` ${Math.floor(pct)}%` : " ..."}
          </p>
          <ProgressBar value={pct} />
        </div>
      )}

      {job.status === "failed" && (
        <p className="mt-1 text-sm text-red-300">
          Lỗi ở bước{" "}
          {job.failed_stage ? STAGE_LABELS[job.failed_stage] : "không xác định"}
          {job.error ? `: ${job.error}` : ""}
        </p>
      )}

      {job.status === "cancelled" && job.error && (
        <p className="mt-1 text-sm text-ink-400">{job.error}</p>
      )}

      {job.status === "success" && files.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {files.map((f, i) => (
            <a
              key={f.filename}
              href={downloadUrl(job.id, f.filename)}
              className="rounded border border-emerald-600/50 bg-emerald-500/10 px-3 py-1.5 text-sm font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20"
            >
              {files.length > 1 ? `Tải phần ${i + 1}` : "Tải video"}
            </a>
          ))}
        </div>
      )}
    </li>
  );
}

interface JobReelProps {
  jobs: JobState[];
  onDeleteJob: (jobId: string) => void;
  onDeleteAllFinished: (jobIds: string[]) => void;
}

export default function JobReel({
  jobs,
  onDeleteJob,
  onDeleteAllFinished,
}: JobReelProps) {
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [clearingAll, setClearingAll] = useState(false);

  const finishedIds = jobs
    .filter((j) => FINISHED_STATUSES.includes(j.status))
    .map((j) => j.id);

  async function handleClearAll() {
    setClearingAll(true);
    setErrorMessage(null);
    try {
      const res = await deleteAllFinishedJobs();
      onDeleteAllFinished(res.deleted_job_ids);
    } catch (err) {
      setErrorMessage(
        err instanceof Error
          ? err.message
          : "Không thể xóa các tác vụ đã hoàn tất.",
      );
    } finally {
      setClearingAll(false);
    }
  }

  if (jobs.length === 0) {
    return (
      <p className="rounded border border-dashed border-ink-700 p-6 text-center text-sm text-ink-400">
        Chưa có tác vụ nào. Dán URL và nhấn xử lý để bắt đầu.
      </p>
    );
  }

  return (
    <div className="grid gap-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-ink-400">{jobs.length} tác vụ</p>
        {finishedIds.length > 0 && (
          <button
            type="button"
            onClick={handleClearAll}
            disabled={clearingAll}
            className="text-xs font-medium text-ink-400 transition-colors hover:text-red-300 disabled:opacity-50"
          >
            {clearingAll
              ? "Đang xóa…"
              : `Xóa tất cả đã hoàn tất (${finishedIds.length})`}
          </button>
        )}
      </div>

      {errorMessage && <p className="text-sm text-red-300">{errorMessage}</p>}

      <ul className="grid gap-3">
        {jobs.map((job) => (
          <JobRow
            key={job.id}
            job={job}
            onDeleted={onDeleteJob}
            onError={setErrorMessage}
          />
        ))}
      </ul>
    </div>
  );
}
