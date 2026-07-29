export interface CreateBatchPayload {
  urls: string[];
  outputDir: string;
  cookieFile: File;
  musicFile?: File | null;
  sourceLang?: string;
  targetLang?: string;
  burnSubtitles?: boolean;
  mixMusicVolume?: number;
}

export interface CreateBatchResponse {
  batch_id: string;
  job_ids: string[];
}

export async function createBatch(
  payload: CreateBatchPayload,
): Promise<CreateBatchResponse> {
  const form = new FormData();
  form.append("urls", payload.urls.join(","));
  form.append("output_dir", payload.outputDir);
  form.append("source_lang", payload.sourceLang ?? "zh-CN");
  form.append("target_lang", payload.targetLang ?? "vi");
  form.append("burn_subtitles", String(payload.burnSubtitles ?? true));
  form.append("mix_music_volume", String(payload.mixMusicVolume ?? 0.15));
  form.append("cookie_file", payload.cookieFile);
  if (payload.musicFile) form.append("music_file", payload.musicFile);

  const res = await fetch("/api/jobs", { method: "POST", body: form });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Không thể tạo tác vụ (${res.status}): ${text}`);
  }
  return res.json();
}

export interface CancelAllResponse {
  ok: boolean;
  cancelled_job_ids: string[];
}

export async function cancelAllJobs(): Promise<CancelAllResponse> {
  const res = await fetch("/api/jobs/cancel-all", { method: "POST" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Không thể hủy tác vụ (${res.status}): ${text}`);
  }
  return res.json();
}
