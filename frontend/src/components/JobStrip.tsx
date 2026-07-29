import { JobState, PIPELINE_STAGES, STAGE_LABEL, StageName } from "../types";
import {
  IconAlert,
  IconCheck,
  IconDownload,
  IconExport,
  IconTranslate,
  IconVoice,
  IconWave,
} from "./icons";

const STAGE_ICON: Record<
  StageName,
  (props: { className?: string }) => JSX.Element
> = {
  queued: IconAlert,
  download: IconDownload,
  speech_to_text: IconWave,
  translate: IconTranslate,
  text_to_speech: IconVoice,
  export: IconExport,
  done: IconCheck,
};

function stageState(
  job: JobState,
  stage: StageName,
): "done" | "active" | "failed" | "idle" {
  console.log("job", job, "state", stage);

  const order = PIPELINE_STAGES;
  const currentIdx = order.indexOf(job.stage);
  const stageIdx = order.indexOf(stage);

  if (job.status === "failed" && job.failed_stage === stage) return "failed";
  if (job.status === "success") return "done";
  if (stageIdx < currentIdx) return "done";
  if (stageIdx === currentIdx && job.status === "running") return "active";
  return "idle";
}

export default function JobStrip({ job }: { job: JobState }) {
  const border =
    job.status === "success"
      ? "border-state-success/50"
      : job.status === "failed"
        ? "border-state-error/50"
        : "border-ink-700";

  return (
    <div className={`rounded border ${border} bg-ink-800/60 p-4`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate font-mono text-xs text-ink-500">{job.url}</p>
          {job.status === "success" && job.output_filename && (
            <p className="mt-1 flex items-center gap-2 text-sm text-state-success">
              <IconCheck className="h-4 w-4" />
              {job.output_filename}
              <a
                href={`/api/jobs/${job.id}/download`}
                download={job.output_filename}
                className="ml-1 rounded border border-state-success/40 px-2 py-0.5 font-mono text-xs text-state-success hover:bg-state-success/10"
              >
                Tải xuống
              </a>
            </p>
          )}
          {job.status === "failed" && (
            <p className="mt-1 flex items-center gap-1.5 text-sm text-state-error">
              <IconAlert className="h-4 w-4" />
              Thất bại ở bước: {STAGE_LABEL[job.failed_stage ?? "queued"]}
              {job.error && (
                <span className="text-ink-500"> — {job.error}</span>
              )}
            </p>
          )}
          {job.status === "running" && (
            <p className="mt-1 text-sm text-reel-amber">
              {STAGE_LABEL[job.stage]}…
            </p>
          )}
          {job.status === "pending" && (
            <p className="mt-1 text-sm text-ink-500">
              Đang chờ trong hàng đợi…
            </p>
          )}
        </div>
      </div>

      <div className="mt-4 flex items-center gap-1">
        {PIPELINE_STAGES.map((stage, i) => {
          const state = stageState(job, stage);
          const Icon = STAGE_ICON[stage];
          const cell =
            state === "done"
              ? "bg-state-success/15 text-state-success border-state-success/40"
              : state === "active"
                ? "bg-reel-amber/15 text-reel-amber border-reel-amber/50 animate-pulse"
                : state === "failed"
                  ? "bg-state-error/15 text-state-error border-state-error/50"
                  : "bg-ink-900 text-ink-700 border-ink-700";
          return (
            <div key={stage} className="flex flex-1 items-center gap-1">
              <div
                title={STAGE_LABEL[stage]}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded border py-1.5 ${cell}`}
              >
                <Icon className="h-3.5 w-3.5" />
                <span className="hidden font-mono text-[10px] uppercase tracking-wide sm:inline">
                  {String(i + 1).padStart(2, "0")}
                </span>
              </div>
              {i < PIPELINE_STAGES.length - 1 && (
                <span className="h-px w-2 bg-ink-700" />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
