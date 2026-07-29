import { JobState } from "../types";
import JobStrip from "./JobStrip";

export default function JobReel({ jobs }: { jobs: JobState[] }) {
  if (jobs.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center rounded border border-dashed border-ink-700 py-16 text-center">
        <p className="font-display text-sm text-ink-500">Chưa có tác vụ nào</p>
        <p className="mt-1 text-xs text-ink-700">Nhập URL và bấm "Bắt đầu xử lý" để bắt đầu.</p>
      </div>
    );
  }

  const successCount = jobs.filter((j) => j.status === "success").length;
  const failedCount = jobs.filter((j) => j.status === "failed").length;

  return (
    <div className="flex flex-1 flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-sm font-semibold text-ink-200">Danh sách xử lý</h2>
        <p className="font-mono text-xs text-ink-500">
          {jobs.length} video · <span className="text-state-success">{successCount} thành công</span> ·{" "}
          <span className="text-state-error">{failedCount} thất bại</span>
        </p>
      </div>
      <div className="flex flex-col gap-3">
        {jobs.map((job) => (
          <JobStrip key={job.id} job={job} />
        ))}
      </div>
    </div>
  );
}
