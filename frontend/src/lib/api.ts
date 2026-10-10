import { Platform } from "../types";

// Same-origin by default; override with VITE_API_BASE if the API is served
// from a different host/port than the frontend.
const API_BASE = (import.meta as any).env?.VITE_API_BASE ?? "";

export interface CreateBatchParams {
  urls: string[];
  platform: Platform;
  cookieFile: File | null;
  // Reuse a previously saved cookie file (see listCookieRefs) instead of
  // uploading a fresh one. Takes priority over cookieFile if both are set.
  cookieRefId?: string | null;
  // Non-empty = also persist the matching fresh cookieFile permanently
  // (tagged with `platform`) under this label, so it's reusable next time
  // via listCookieRefs() instead of needing to be re-uploaded.
  saveCookieLabel?: string;
  musicFile: File | null;
  burnSubtitles: boolean;
  mixMusicVolume: number;
  resolution: "720p" | "1080p";
  splitLongVideo: boolean;
  splitVideoMinutes: number;
  matchVoiceGender: boolean;
  // Erases hard-coded source-language subtitles already burned into the
  // source video's pixels (AI inpainting) before the new Vietnamese
  // subtitles get burned in. Slow — off by default, see App.tsx.
  removeHardsub: boolean;
  maleVoiceRefFile?: File | null;
  femaleVoiceRefFile?: File | null;
  // Single voice-clone reference for the whole video — used when
  // matchVoiceGender is off, as the non-gender-matched alternative.
  voiceRefFile?: File | null;
  // Reuse a previously saved voice (see listVoiceRefs) instead of
  // uploading a fresh clip. Takes priority over the matching *VoiceRefFile
  // above if both are set.
  maleVoiceRefId?: string | null;
  femaleVoiceRefId?: string | null;
  voiceRefId?: string | null;
  // Non-empty = also persist the matching fresh upload (*VoiceRefFile, not
  // *VoiceRefId) permanently under this label, so it's reusable next time
  // via listVoiceRefs() instead of needing to be re-uploaded.
  saveMaleVoiceRefLabel?: string;
  saveFemaleVoiceRefLabel?: string;
  saveVoiceRefLabel?: string;
  // Pre-downloaded video files already on the user's machine — an
  // alternative/addition to `urls`. These skip the download step
  // server-side entirely and don't need a cookie file.
  localVideoFiles?: File[];
  // Channel-name text burned into the top-left corner of the export.
  // Empty/omitted = no watermark.
  watermarkText?: string;
  translateEngine?: string;
}

export interface CreateBatchResponse {
  batch_id: string;
  job_ids: string[];
}

async function readErrorDetail(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") return data.detail;
    return JSON.stringify(data);
  } catch {
    return `Yêu cầu thất bại (${res.status})`;
  }
}

export async function createBatch(
  params: CreateBatchParams,
): Promise<CreateBatchResponse> {
  const formData = new FormData();
  formData.append("urls", params.urls.join(","));
  formData.append("platform", params.platform);
  formData.append("burn_subtitles", String(params.burnSubtitles));
  formData.append("mix_music_volume", String(params.mixMusicVolume));
  formData.append("resolution", params.resolution);
  formData.append("split_long_video", String(params.splitLongVideo));
  formData.append("split_video_minutes", String(params.splitVideoMinutes));
  formData.append("match_voice_gender", String(params.matchVoiceGender));
  formData.append("remove_hardsub", String(params.removeHardsub));
  if (params.cookieFile) {
    formData.append("cookie_file", params.cookieFile);
  }
  if (params.cookieRefId) {
    formData.append("cookie_ref_id", params.cookieRefId);
  }
  if (params.saveCookieLabel?.trim()) {
    formData.append("save_cookie_label", params.saveCookieLabel.trim());
  }
  if (params.musicFile) {
    formData.append("music_file", params.musicFile);
  }
  if (params.maleVoiceRefFile) {
    formData.append("male_voice_ref_file", params.maleVoiceRefFile);
  }
  if (params.femaleVoiceRefFile) {
    formData.append("female_voice_ref_file", params.femaleVoiceRefFile);
  }
  if (params.voiceRefFile) {
    formData.append("voice_ref_file", params.voiceRefFile);
  }
  if (params.maleVoiceRefId) {
    formData.append("male_voice_ref_id", params.maleVoiceRefId);
  }
  if (params.femaleVoiceRefId) {
    formData.append("female_voice_ref_id", params.femaleVoiceRefId);
  }
  if (params.voiceRefId) {
    formData.append("voice_ref_id", params.voiceRefId);
  }
  if (params.saveMaleVoiceRefLabel?.trim()) {
    formData.append(
      "save_male_voice_ref_label",
      params.saveMaleVoiceRefLabel.trim(),
    );
  }
  if (params.saveFemaleVoiceRefLabel?.trim()) {
    formData.append(
      "save_female_voice_ref_label",
      params.saveFemaleVoiceRefLabel.trim(),
    );
  }
  if (params.saveVoiceRefLabel?.trim()) {
    formData.append("save_voice_ref_label", params.saveVoiceRefLabel.trim());
  }
  for (const file of params.localVideoFiles ?? []) {
    formData.append("video_files", file);
  }
  if (params.watermarkText && params.watermarkText.trim()) {
    formData.append("watermark_text", params.watermarkText.trim());
  }

  const res = await fetch(`${API_BASE}/api/jobs`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
  return res.json();
}

export interface DependencyItem {
  name: string;
  kind: "binary" | "python_package" | "model";
  description: string;
  installed: boolean;
  detail: string | null;
  version: string | null;
  install_hint: string;
  auto_installable: boolean;
}

export async function fetchDependencies(): Promise<DependencyItem[]> {
  const res = await fetch(`${API_BASE}/api/system/dependencies`);
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
  const data = await res.json();
  return data.items as DependencyItem[];
}

/** Installs a pip-installable dependency (only ones where `auto_installable`
 * is true — e.g. f2, yt-dlp, faster_whisper, deep_translator, vieneu) and
 * streams the `pip install` output back line by line via `onLog`, so the UI
 * can show live progress instead of hanging silently. The backend only
 * accepts `name` values from its own server-side whitelist (never an
 * arbitrary pip spec), so this can't be used to run installs it doesn't
 * already know about — e.g. ffmpeg/ffprobe, which need a system package
 * manager and always return an error here. */
export async function installDependency(
  name: string,
  onLog: (line: string) => void,
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/system/dependencies/${encodeURIComponent(name)}/install`,
    { method: "POST" },
  );
  if (!res.ok || !res.body) {
    throw new Error(await readErrorDetail(res));
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) onLog(line);
  }
  if (buffer) onLog(buffer);
}

export async function cancelJob(jobId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/jobs/${jobId}/cancel`, {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
}

/** Re-queues a failed job, resuming from the stage it failed at and
 * reusing whatever intermediate results (download/transcript/translation/
 * TTS track) that stage's earlier attempt already produced. Only valid
 * for jobs whose status is "failed" — the websocket will push the job back
 * through "pending" -> "running" -> ... as it re-runs. */
export async function retryJob(jobId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/jobs/${jobId}/retry`, {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
}

export async function cancelAllJobs(): Promise<{
  ok: boolean;
  cancelled_job_ids: string[];
}> {
  const res = await fetch(`${API_BASE}/api/jobs/cancel-all`, {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
  return res.json();
}

export async function deleteJob(jobId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/jobs/${jobId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
}

export async function deleteAllFinishedJobs(): Promise<{
  ok: boolean;
  deleted_job_ids: string[];
}> {
  const res = await fetch(`${API_BASE}/api/jobs`, { method: "DELETE" });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
  return res.json();
}

/** Builds the download URL for a job's output file. Pass `filename` (one of
 * job.output_files[].filename) when a job was split into multiple parts;
 * omit it to get the first/only part. */
export function downloadUrl(jobId: string, filename?: string): string {
  const query = filename ? `?file=${encodeURIComponent(filename)}` : "";
  return `${API_BASE}/api/jobs/${jobId}/download${query}`;
}

export interface CookieRef {
  id: string;
  platform: "douyin" | "bilibili";
  label: string;
  created_at: string;
}

/** Lists previously saved cookie files (see CreateBatchParams.saveCookieLabel),
 * newest first. Pass `platform` to filter to one of "douyin" | "bilibili". */
export async function listCookieRefs(
  platform?: CookieRef["platform"],
): Promise<CookieRef[]> {
  const query = platform ? `?platform=${encodeURIComponent(platform)}` : "";
  const res = await fetch(`${API_BASE}/api/cookie-refs${query}`);
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
  const data = await res.json();
  return data.items as CookieRef[];
}

export async function deleteCookieRef(refId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/cookie-refs/${encodeURIComponent(refId)}`,
    { method: "DELETE" },
  );
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
}

export interface VoiceRef {
  id: string;
  kind: "male" | "female" | "single";
  label: string;
  created_at: string;
}

/** Lists previously saved voice-clone reference clips (see
 * CreateBatchParams.save*VoiceRefLabel), newest first. Pass `kind` to
 * filter to one of "male" | "female" | "single". */
export async function listVoiceRefs(
  kind?: VoiceRef["kind"],
): Promise<VoiceRef[]> {
  const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  const res = await fetch(`${API_BASE}/api/voice-refs${query}`);
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
  const data = await res.json();
  return data.items as VoiceRef[];
}

export async function deleteVoiceRef(refId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/voice-refs/${encodeURIComponent(refId)}`,
    { method: "DELETE" },
  );
  if (!res.ok) {
    throw new Error(await readErrorDetail(res));
  }
}
