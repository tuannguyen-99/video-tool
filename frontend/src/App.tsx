import { useEffect, useRef, useState } from "react";
import Header from "./components/Header";
import UrlInput from "./components/UrlInput";
import FileDrop from "./components/FileDrop";
import SubmitBar from "./components/SubmitBar";
import JobReel from "./components/JobReel";
import { IconCookie, IconMusic } from "./components/icons";
import {
  cancelAllJobs,
  createBatch,
  CookieRef,
  deleteCookieRef,
  deleteVoiceRef,
  listCookieRefs,
  listVoiceRefs,
  VoiceRef,
} from "./lib/api";
import { connectJobSocket } from "./lib/ws";
import { JobState, Platform } from "./types";
import DependencyStatus from "./components/DependencyStatus";

const RESOLUTION_STORAGE_KEY = "douyin-vietsub:resolution";
const PLATFORM_STORAGE_KEY = "douyin-vietsub:platform";
const SPLIT_STORAGE_KEY = "douyin-vietsub:splitLongVideo";
const SPLIT_MINUTES_STORAGE_KEY = "douyin-vietsub:splitVideoMinutes";
const MATCH_VOICE_GENDER_STORAGE_KEY = "douyin-vietsub:matchVoiceGender";
const REMOVE_HARDSUB_STORAGE_KEY = "douyin-vietsub:removeHardsub";
const WATERMARK_TEXT_STORAGE_KEY = "douyin-vietsub:watermarkText";
const TRANSLATE_ENGINE_STORAGE_KEY = "douyin-vietsub:translateEngine";

// Hai model dịch chạy local (xem pipeline/translate.py). Giá trị gửi lên
// backend phải khớp đúng SUPPORTED_ENGINES bên đó.
type TranslateEngine = "nllb" | "hachimi";

const TRANSLATE_ENGINE_OPTIONS: {
  value: TranslateEngine;
  label: string;
  hint: string;
}[] = [
  {
    value: "nllb",
    label: "NLLB-200",
    hint: "Đa ngôn ngữ, ổn định cho video nói thường. Nặng hơn, chậm hơn trên CPU.",
  },
  {
    value: "hachimi",
    label: "HachimiMT-60",
    hint: "Chỉ Trung → Việt, train riêng cho truyện mạng (tiên hiệp, đô thị). Nhẹ hơn ~10 lần nên nhanh hơn nhiều, xưng hô Hán-Việt tự nhiên hơn.",
  },
];

// The three source tabs: fetch by URL from Douyin/Bilibili, or skip
// fetching entirely and use a video file already on the user's machine.
// "local" isn't a real `Platform` for URL-based jobs (see types.ts /
// backend Platform enum) — it just controls which section of the form is
// shown here.
type SourceTab = Platform | "local";

const SOURCE_TAB_OPTIONS: { value: SourceTab; label: string }[] = [
  { value: "douyin", label: "Douyin" },
  { value: "bilibili", label: "Bilibili" },
  { value: "local", label: "Video có sẵn" },
];

interface VoiceRefFieldProps {
  label: string;
  savedRefs: VoiceRef[];
  selectedId: string;
  onSelectId: (id: string) => void;
  file: File | null;
  onFileChange: (f: File | null) => void;
  saveLabel: string;
  onSaveLabelChange: (v: string) => void;
  onDelete: (id: string) => void;
}

function VoiceRefField({
  label,
  savedRefs,
  selectedId,
  onSelectId,
  file,
  onFileChange,
  saveLabel,
  onSaveLabelChange,
  onDelete,
}: VoiceRefFieldProps) {
  const selected = savedRefs.find((r) => r.id === selectedId);

  return (
    <div className="grid gap-1.5">
      <label className="text-sm font-medium text-ink-200">{label}</label>

      {savedRefs.length > 0 && (
        <select
          value={selectedId}
          onChange={(e) => onSelectId(e.target.value)}
          className="rounded border border-ink-600 bg-ink-800 px-3 py-2 text-sm text-ink-100"
        >
          <option value="">— Tải file mới —</option>
          {savedRefs.map((r) => (
            <option key={r.id} value={r.id}>
              {r.label}
            </option>
          ))}
        </select>
      )}

      {selected ? (
        <div className="flex items-center justify-between gap-2 text-xs text-ink-400">
          <span>Đang dùng giọng đã lưu: {selected.label}</span>
          <button
            type="button"
            onClick={() => onDelete(selected.id)}
            className="shrink-0 font-medium text-ink-400 transition-colors hover:text-red-300"
          >
            Xóa giọng này
          </button>
        </div>
      ) : (
        <>
          <FileDrop
            label="Mẫu giọng (clone)"
            icon={<IconMusic />}
            accept="audio/*,.wav,.mp3"
            file={file}
            onChange={onFileChange}
            optional
          />
          {file && (
            <input
              type="text"
              placeholder="Lưu giọng này với tên (để dùng lần sau) — bỏ trống nếu chỉ dùng 1 lần"
              value={saveLabel}
              onChange={(e) => onSaveLabelChange(e.target.value)}
              maxLength={60}
              className="rounded border border-ink-600 bg-ink-800 px-3 py-2 text-xs text-ink-100 placeholder:text-ink-500"
            />
          )}
        </>
      )}
    </div>
  );
}

export default function App() {
  const [sourceTab, setSourceTab] = useState<SourceTab>(() => {
    try {
      const saved = localStorage.getItem(PLATFORM_STORAGE_KEY);
      return saved === "douyin" || saved === "bilibili" || saved === "local"
        ? saved
        : "douyin";
    } catch {
      return "douyin";
    }
  });
  // Only meaningful for the two URL-based tabs — the value the backend's
  // `platform` field expects when submitting URL jobs. Local-upload jobs
  // don't send a platform at all.
  const platform: Platform = sourceTab === "bilibili" ? "bilibili" : "douyin";

  function handleSourceTabChange(v: SourceTab) {
    setSourceTab(v);
    // A saved cookie is tied to one platform (Douyin cookies don't
    // authenticate Bilibili and vice versa) — clear the selection so
    // switching tabs doesn't silently carry over the wrong one.
    setCookieRefId("");
    try {
      localStorage.setItem(PLATFORM_STORAGE_KEY, v);
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  const [urls, setUrls] = useState("");
  const [cookieFile, setCookieFile] = useState<File | null>(null);
  const [musicFile, setMusicFile] = useState<File | null>(null);
  const [localVideoFiles, setLocalVideoFiles] = useState<File[]>([]);

  // Saved cookie files (see /api/cookie-refs), fetched once and refreshed
  // after any batch submit that might have saved a new one. Either a
  // saved one (cookieRefId) or a fresh upload (cookieFile) is used — see
  // the cookie resolution block in main.py's create_jobs.
  const [savedCookieRefs, setSavedCookieRefs] = useState<CookieRef[]>([]);
  async function refreshCookieRefs() {
    try {
      setSavedCookieRefs(await listCookieRefs());
    } catch {
      // Non-critical — the picker just falls back to "upload new" only.
    }
  }
  useEffect(() => {
    refreshCookieRefs();
  }, []);
  const [cookieRefId, setCookieRefId] = useState("");
  const [cookieSaveLabel, setCookieSaveLabel] = useState("");
  async function handleDeleteCookieRef(id: string) {
    try {
      await deleteCookieRef(id);
      await refreshCookieRefs();
      if (cookieRefId === id) setCookieRefId("");
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "Không thể xóa cookie đã lưu.",
      );
    }
  }

  // Saved voice-clone references (see /api/voice-refs), fetched once and
  // refreshed after any batch submit that might have saved a new one.
  const [savedVoiceRefs, setSavedVoiceRefs] = useState<VoiceRef[]>([]);
  async function refreshVoiceRefs() {
    try {
      setSavedVoiceRefs(await listVoiceRefs());
    } catch {
      // Non-critical — the picker just falls back to "upload new" only.
    }
  }
  useEffect(() => {
    refreshVoiceRefs();
  }, []);
  async function handleDeleteVoiceRef(id: string) {
    try {
      await deleteVoiceRef(id);
      await refreshVoiceRefs();
      if (maleVoiceRefId === id) setMaleVoiceRefId("");
      if (femaleVoiceRefId === id) setFemaleVoiceRefId("");
      if (voiceRefId === id) setVoiceRefId("");
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "Không thể xóa giọng đã lưu.",
      );
    }
  }

  // Each slot: either an existing saved voice (id) or a fresh upload
  // (file), optionally saved permanently under saveLabel — see
  // VoiceRefField above and _resolve_ref in main.py.
  const [maleVoiceRefFile, setMaleVoiceRefFile] = useState<File | null>(null);
  const [maleVoiceRefId, setMaleVoiceRefId] = useState("");
  const [maleVoiceRefSaveLabel, setMaleVoiceRefSaveLabel] = useState("");
  const [femaleVoiceRefFile, setFemaleVoiceRefFile] = useState<File | null>(
    null,
  );
  const [femaleVoiceRefId, setFemaleVoiceRefId] = useState("");
  const [femaleVoiceRefSaveLabel, setFemaleVoiceRefSaveLabel] = useState("");
  const [voiceRefFile, setVoiceRefFile] = useState<File | null>(null);
  const [voiceRefId, setVoiceRefId] = useState("");
  const [voiceRefSaveLabel, setVoiceRefSaveLabel] = useState("");
  const [burnSubtitles, setBurnSubtitles] = useState(true);
  const [musicVolume, setMusicVolume] = useState(0.15);
  const [resolution, setResolution] = useState<"720p" | "1080p">(() => {
    try {
      const saved = localStorage.getItem(RESOLUTION_STORAGE_KEY);
      return saved === "720p" || saved === "1080p" ? saved : "1080p";
    } catch {
      return "1080p";
    }
  });
  const [splitLongVideo, setSplitLongVideo] = useState(() => {
    try {
      return localStorage.getItem(SPLIT_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [splitVideoMinutes, setSplitVideoMinutes] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(SPLIT_MINUTES_STORAGE_KEY));
      return saved > 0 ? saved : 10;
    } catch {
      return 10;
    }
  });
  const [matchVoiceGender, setMatchVoiceGender] = useState(() => {
    try {
      return localStorage.getItem(MATCH_VOICE_GENDER_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  // Default OFF: AI inpainting per-frame is far slower than every other
  // stage in the pipeline, so this should only run for source videos that
  // actually have a burned-in subtitle to remove — see the tooltip text
  // below for the tradeoff, matches subtitle_remover.py's own reasoning.
  const [removeHardsub, setRemoveHardsub] = useState(() => {
    try {
      return localStorage.getItem(REMOVE_HARDSUB_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [translateEngine, setTranslateEngine] = useState<TranslateEngine>(
    () => {
      try {
        const saved = localStorage.getItem(TRANSLATE_ENGINE_STORAGE_KEY);
        return saved === "nllb" || saved === "hachimi" ? saved : "nllb";
      } catch {
        return "nllb";
      }
    },
  );
  const [watermarkText, setWatermarkText] = useState(() => {
    try {
      return localStorage.getItem(WATERMARK_TEXT_STORAGE_KEY) ?? "";
    } catch {
      return "";
    }
  });

  function handleResolutionChange(v: "720p" | "1080p") {
    setResolution(v);
    try {
      localStorage.setItem(RESOLUTION_STORAGE_KEY, v);
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  function handleSplitLongVideoChange(v: boolean) {
    setSplitLongVideo(v);
    try {
      localStorage.setItem(SPLIT_STORAGE_KEY, v ? "1" : "0");
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  function handleSplitVideoMinutesChange(v: number) {
    // Same range the backend validates (0.5–180 min), so a bad value gets
    // caught here instead of only surfacing as a 422 on submit.
    const clamped = Number.isFinite(v) ? Math.min(Math.max(v, 0.5), 180) : 10;
    setSplitVideoMinutes(clamped);
    try {
      localStorage.setItem(SPLIT_MINUTES_STORAGE_KEY, String(clamped));
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  function handleMatchVoiceGenderChange(v: boolean) {
    setMatchVoiceGender(v);
    try {
      localStorage.setItem(MATCH_VOICE_GENDER_STORAGE_KEY, v ? "1" : "0");
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  function handleRemoveHardsubChange(v: boolean) {
    setRemoveHardsub(v);
    try {
      localStorage.setItem(REMOVE_HARDSUB_STORAGE_KEY, v ? "1" : "0");
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  function handleTranslateEngineChange(v: TranslateEngine) {
    setTranslateEngine(v);
    try {
      localStorage.setItem(TRANSLATE_ENGINE_STORAGE_KEY, v);
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  function handleWatermarkTextChange(v: string) {
    // Same 60-char cap the backend validates.
    const capped = v.slice(0, 60);
    setWatermarkText(capped);
    try {
      localStorage.setItem(WATERMARK_TEXT_STORAGE_KEY, capped);
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  function handleAddLocalVideoFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    setLocalVideoFiles((prev) => [...prev, ...Array.from(fileList)]);
  }

  function handleRemoveLocalVideoFile(index: number) {
    setLocalVideoFiles((prev) => prev.filter((_, i) => i !== index));
  }

  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [showDependencyStatus, setShowDependencyStatus] = useState(false);

  const [trackedIds, setTrackedIds] = useState<Set<string>>(new Set());
  const [jobsById, setJobsById] = useState<Record<string, JobState>>({});
  const trackedIdsRef = useRef(trackedIds);
  trackedIdsRef.current = trackedIds;

  useEffect(() => {
    const disconnect = connectJobSocket((job) => {
      if (!trackedIdsRef.current.has(job.id)) return;
      setJobsById((prev) => ({ ...prev, [job.id]: job }));
    });
    return disconnect;
  }, []);

  const urlCount = urls
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean).length;
  const isLocalTab = sourceTab === "local";
  const hasUrls = !isLocalTab && urlCount > 0;
  const hasLocalFiles = localVideoFiles.length > 0;
  const canSubmit = isLocalTab
    ? hasLocalFiles
    : hasUrls && (!!cookieFile || !!cookieRefId);

  async function handleSubmit() {
    setErrorMessage(null);
    if (isLocalTab) {
      if (!hasLocalFiles) {
        setErrorMessage("Vui lòng chọn ít nhất 1 file video đã tải sẵn.");
        return;
      }
    } else {
      if (!hasUrls) {
        setErrorMessage("Vui lòng nhập ít nhất 1 URL.");
        return;
      }
      if (!cookieFile && !cookieRefId) {
        const platformLabel = platform === "douyin" ? "Douyin" : "Bilibili";
        setErrorMessage(
          `Vui lòng chọn file cookie để xác thực với ${platformLabel}.`,
        );
        return;
      }
    }
    setSubmitting(true);
    try {
      const urlList = isLocalTab
        ? []
        : urls
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
      const res = await createBatch({
        urls: urlList,
        platform,
        cookieFile: isLocalTab || cookieRefId ? null : cookieFile,
        cookieRefId: isLocalTab ? null : cookieRefId || null,
        saveCookieLabel: isLocalTab ? undefined : cookieSaveLabel,
        musicFile,
        burnSubtitles,
        mixMusicVolume: musicVolume,
        resolution,
        splitLongVideo,
        splitVideoMinutes,
        matchVoiceGender,
        removeHardsub,
        maleVoiceRefFile:
          matchVoiceGender && !maleVoiceRefId ? maleVoiceRefFile : null,
        femaleVoiceRefFile:
          matchVoiceGender && !femaleVoiceRefId ? femaleVoiceRefFile : null,
        voiceRefFile: !matchVoiceGender && !voiceRefId ? voiceRefFile : null,
        maleVoiceRefId: matchVoiceGender ? maleVoiceRefId || null : null,
        femaleVoiceRefId: matchVoiceGender ? femaleVoiceRefId || null : null,
        voiceRefId: !matchVoiceGender ? voiceRefId || null : null,
        saveMaleVoiceRefLabel: matchVoiceGender
          ? maleVoiceRefSaveLabel
          : undefined,
        saveFemaleVoiceRefLabel: matchVoiceGender
          ? femaleVoiceRefSaveLabel
          : undefined,
        saveVoiceRefLabel: !matchVoiceGender ? voiceRefSaveLabel : undefined,
        localVideoFiles: isLocalTab ? localVideoFiles : [],
        watermarkText,
        translateEngine,
      });

      // Job order from the backend is URL jobs first, then local-upload
      // jobs — matches the order createBatch()/api.ts appends them in.
      const displayLabels = isLocalTab
        ? localVideoFiles.map((f) => f.name)
        : urlList;

      setTrackedIds((prev) => new Set([...prev, ...res.job_ids]));
      setJobsById((prev) => {
        const next = { ...prev };
        for (const id of res.job_ids) {
          const idx = res.job_ids.indexOf(id);
          next[id] = next[id] ?? {
            id,
            url: displayLabels[idx] ?? "",
            platform: idx < urlList.length ? platform : "local",
            status: "pending",
            stage: "queued",
            failed_stage: null,
            error: null,
            split_long_video: splitLongVideo,
            match_voice_gender: matchVoiceGender,
            detected_voice_gender: null,
            title: null,
            title_vi: null,
            output_files: [],
            output_filename: null,
            output_path: null,
            progress: 0,
          };
        }
        return next;
      });
      // Clear the local file picker after a successful submit — the cookie
      // and music fields intentionally stay (people usually reuse the same
      // cookie/music across several batches).
      setLocalVideoFiles([]);
      // A fresh upload with a save-label may have just been persisted
      // server-side — refresh so it shows up in the picker immediately,
      // and clear the now-submitted upload state (selected saved-voice IDs
      // intentionally stay, since people usually reuse the same voice).
      setMaleVoiceRefFile(null);
      setMaleVoiceRefSaveLabel("");
      setFemaleVoiceRefFile(null);
      setFemaleVoiceRefSaveLabel("");
      setVoiceRefFile(null);
      setVoiceRefSaveLabel("");
      refreshVoiceRefs();
      // Same reasoning as the voice refs: clear the save-label input, but
      // leave cookieFile/cookieRefId as-is since people usually reuse the
      // same cookie across several batches.
      setCookieSaveLabel("");
      refreshCookieRefs();
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "Đã có lỗi không xác định.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const jobs = Array.from(trackedIds)
    .map((id) => jobsById[id])
    .filter(Boolean)
    .reverse();

  const isProcessing = jobs.some(
    (j) => j.status === "pending" || j.status === "running",
  );

  async function handleStop() {
    setStopping(true);
    setErrorMessage(null);
    try {
      await cancelAllJobs();
      // No need to optimistically patch jobsById here — the websocket will
      // push each job's status: "cancelled" update as the backend applies it.
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "Không thể dừng tác vụ.",
      );
    } finally {
      setStopping(false);
    }
  }

  const cookieLabel =
    platform === "douyin"
      ? "File cookie (f2)"
      : "File cookie (Netscape cookies.txt)";
  const cookieRefsForPlatform = savedCookieRefs.filter(
    (r) => r.platform === platform,
  );
  const selectedCookieRef = cookieRefsForPlatform.find(
    (r) => r.id === cookieRefId,
  );

  function handleDeleteJob(jobId: string) {
    setTrackedIds((prev) => {
      const next = new Set(prev);
      next.delete(jobId);
      return next;
    });
    setJobsById((prev) => {
      const next = { ...prev };
      delete next[jobId];
      return next;
    });
  }

  function handleDeleteAllFinished(jobIds: string[]) {
    if (jobIds.length === 0) return;
    setTrackedIds((prev) => {
      const next = new Set(prev);
      for (const id of jobIds) next.delete(id);
      return next;
    });
    setJobsById((prev) => {
      const next = { ...prev };
      for (const id of jobIds) delete next[id];
      return next;
    });
  }

  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 px-8 py-8">
        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => setShowDependencyStatus((v) => !v)}
            className="text-xs font-medium text-ink-400 transition-colors hover:text-ink-200"
          >
            {showDependencyStatus
              ? "Ẩn trạng thái cài đặt hệ thống"
              : "Xem trạng thái cài đặt hệ thống"}
          </button>
        </div>

        {showDependencyStatus && <DependencyStatus />}

        <section className="grid gap-4 rounded border border-ink-700 bg-ink-800/40 p-5">
          <div className="grid gap-1.5">
            <label className="text-sm font-medium text-ink-200">
              Nguồn video
            </label>
            <div className="flex gap-2">
              {SOURCE_TAB_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => handleSourceTabChange(opt.value)}
                  aria-pressed={sourceTab === opt.value}
                  className={`rounded border px-4 py-2 text-sm font-medium transition-colors ${
                    sourceTab === opt.value
                      ? "border-emerald-500 bg-emerald-500/10 text-emerald-300"
                      : "border-ink-700 bg-ink-800/40 text-ink-300 hover:border-ink-600"
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {isLocalTab ? (
            <div className="grid gap-2 rounded border border-ink-700/60 bg-ink-900/30 p-4">
              <div className="flex items-center justify-between gap-2">
                <label className="text-sm font-medium text-ink-200">
                  Video đã tải sẵn trên máy
                </label>
                <label className="cursor-pointer rounded border border-ink-600 bg-ink-800 px-3 py-1.5 text-xs font-medium text-ink-200 transition-colors hover:border-ink-500">
                  + Thêm video
                  <input
                    type="file"
                    accept="video/*,.mp4,.mov,.mkv"
                    multiple
                    className="hidden"
                    onChange={(e) => {
                      handleAddLocalVideoFiles(e.target.files);
                      e.target.value = "";
                    }}
                  />
                </label>
              </div>

              {localVideoFiles.length > 0 ? (
                <ul className="grid gap-1.5">
                  {localVideoFiles.map((file, i) => (
                    <li
                      key={`${file.name}-${i}`}
                      className="flex items-center justify-between gap-2 rounded bg-ink-800/60 px-3 py-1.5 text-sm text-ink-200"
                    >
                      <span className="truncate" title={file.name}>
                        {file.name}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleRemoveLocalVideoFile(i)}
                        className="shrink-0 text-xs font-medium text-ink-400 transition-colors hover:text-red-300"
                      >
                        Xóa
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-ink-500">
                  Chưa chọn video nào. Có thể chọn nhiều file cùng lúc.
                </p>
              )}
            </div>
          ) : (
            <>
              <UrlInput value={urls} onChange={setUrls} />

              <div className="grid gap-1.5">
                <label className="text-sm font-medium text-ink-200">
                  {cookieLabel}
                </label>

                {cookieRefsForPlatform.length > 0 && (
                  <select
                    value={cookieRefId}
                    onChange={(e) => setCookieRefId(e.target.value)}
                    className="rounded border border-ink-600 bg-ink-800 px-3 py-2 text-sm text-ink-100"
                  >
                    <option value="">— Tải file mới —</option>
                    {cookieRefsForPlatform.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.label}
                      </option>
                    ))}
                  </select>
                )}

                {selectedCookieRef ? (
                  <div className="flex items-center justify-between gap-2 text-xs text-ink-400">
                    <span>
                      Đang dùng cookie đã lưu: {selectedCookieRef.label}
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        handleDeleteCookieRef(selectedCookieRef.id)
                      }
                      className="shrink-0 font-medium text-ink-400 transition-colors hover:text-red-300"
                    >
                      Xóa cookie này
                    </button>
                  </div>
                ) : (
                  <>
                    <FileDrop
                      label="Tải file cookie mới"
                      icon={<IconCookie />}
                      accept=".txt,.json,.yaml,.yml"
                      file={cookieFile}
                      onChange={setCookieFile}
                    />
                    {cookieFile && (
                      <input
                        type="text"
                        placeholder="Lưu cookie này với tên (để dùng lần sau) — bỏ trống nếu chỉ dùng 1 lần"
                        value={cookieSaveLabel}
                        onChange={(e) => setCookieSaveLabel(e.target.value)}
                        maxLength={60}
                        className="rounded border border-ink-600 bg-ink-800 px-3 py-2 text-xs text-ink-100 placeholder:text-ink-500"
                      />
                    )}
                  </>
                )}
              </div>
            </>
          )}

          <FileDrop
            label="Nhạc nền mp3"
            icon={<IconMusic />}
            accept="audio/mpeg,.mp3"
            file={musicFile}
            onChange={setMusicFile}
            optional
          />

          <div className="grid gap-1.5">
            <label
              htmlFor="watermark-text"
              className="text-sm font-medium text-ink-200"
            >
              Tên kênh in trên video
              <span className="ml-1 text-ink-500">
                (tuỳ chọn, hiện ở góc trên-trái, mờ, không che hình)
              </span>
            </label>
            <input
              id="watermark-text"
              type="text"
              placeholder="Ví dụ: Yoshino Vietsub"
              value={watermarkText}
              onChange={(e) => handleWatermarkTextChange(e.target.value)}
              maxLength={60}
              className="rounded border border-ink-600 bg-ink-800 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-500"
            />
          </div>

          <div className="grid gap-1.5">
            <label className="text-sm font-medium text-ink-200">
              Độ phân giải đầu ra
            </label>
            <div className="flex gap-2">
              {(["720p", "1080p"] as const).map((res) => (
                <button
                  key={res}
                  type="button"
                  onClick={() => handleResolutionChange(res)}
                  aria-pressed={resolution === res}
                  className={`rounded border px-4 py-2 text-sm font-medium transition-colors ${
                    resolution === res
                      ? "border-emerald-500 bg-emerald-500/10 text-emerald-300"
                      : "border-ink-700 bg-ink-800/40 text-ink-300 hover:border-ink-600"
                  }`}
                >
                  {res}
                </button>
              ))}
            </div>
          </div>

          <div className="grid gap-1.5">
            <label className="text-sm font-medium text-ink-200">
              Model dịch
              <span className="ml-1 text-ink-500">
                (tuỳ chọn, cả hai đều chạy offline trên máy)
              </span>
            </label>
            <div className="flex flex-wrap gap-2">
              {TRANSLATE_ENGINE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => handleTranslateEngineChange(opt.value)}
                  aria-pressed={translateEngine === opt.value}
                  className={`rounded border px-4 py-2 text-sm font-medium transition-colors ${
                    translateEngine === opt.value
                      ? "border-emerald-500 bg-emerald-500/10 text-emerald-300"
                      : "border-ink-700 bg-ink-800/40 text-ink-300 hover:border-ink-600"
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <p className="text-xs text-ink-500">
              {
                TRANSLATE_ENGINE_OPTIONS.find(
                  (o) => o.value === translateEngine,
                )?.hint
              }
            </p>
            <p className="text-xs text-ink-500">
              Model phải được tải trước ở mục “Xem trạng thái cài đặt hệ thống”
              ({translateEngine === "hachimi" ? "hachimi_model" : "nllb_model"}
              ).
            </p>
          </div>

          <div className="grid gap-2">
            <label className="flex items-center gap-2 text-sm text-ink-200">
              <input
                type="checkbox"
                checked={splitLongVideo}
                onChange={(e) => handleSplitLongVideoChange(e.target.checked)}
                className="h-4 w-4 rounded border-ink-600 bg-ink-800 accent-emerald-500"
              />
              Tự động cắt thành từng đoạn nếu video dài hơn số phút bên dưới
              <span className="text-ink-500">(tuỳ chọn)</span>
            </label>

            {splitLongVideo && (
              <div className="ml-6 flex items-center gap-2">
                <label
                  htmlFor="split-video-minutes"
                  className="text-sm text-ink-300"
                >
                  Cắt mỗi đoạn dài
                </label>
                <input
                  id="split-video-minutes"
                  type="number"
                  min={0.5}
                  max={180}
                  step={0.5}
                  value={splitVideoMinutes}
                  onChange={(e) =>
                    handleSplitVideoMinutesChange(e.target.valueAsNumber)
                  }
                  className="w-20 rounded border border-ink-600 bg-ink-800 px-2 py-1 text-sm text-ink-100"
                />
                <span className="text-sm text-ink-300">phút</span>
              </div>
            )}
          </div>

          <label className="flex items-center gap-2 text-sm text-ink-200">
            <input
              type="checkbox"
              checked={removeHardsub}
              onChange={(e) => handleRemoveHardsubChange(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-800 accent-emerald-500"
            />
            Xoá sub tiếng Trung đã gắn cứng trong video gốc (AI)
            <span className="text-ink-500">
              (tuỳ chọn, mặc định tắt — chỉ bật nếu video nguồn có sẵn sub cứng,
              làm chậm đáng kể do phải xử lý AI từng khung hình)
            </span>
          </label>

          <label className="flex items-center gap-2 text-sm text-ink-200">
            <input
              type="checkbox"
              checked={matchVoiceGender}
              onChange={(e) => handleMatchVoiceGenderChange(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-800 accent-emerald-500"
            />
            Tự động chọn giọng đọc nam/nữ theo giọng gốc trong video
            <span className="text-ink-500">
              (tuỳ chọn, mặc định dùng 1 giọng cố định)
            </span>
          </label>

          {matchVoiceGender ? (
            <div className="grid gap-3 rounded border border-ink-700/60 bg-ink-900/30 p-4">
              <p className="text-xs text-ink-400">
                Tuỳ chọn: chọn giọng đã lưu, hoặc tải lên mẫu giọng mới (3-5
                giây, rõ, ít tạp âm) để nhân bản (clone). Bỏ trống ô nào thì
                giới tính đó dùng giọng mặc định.
              </p>
              <div className="grid gap-4 sm:grid-cols-2">
                <VoiceRefField
                  label="Giọng Nam"
                  savedRefs={savedVoiceRefs.filter((r) => r.kind === "male")}
                  selectedId={maleVoiceRefId}
                  onSelectId={setMaleVoiceRefId}
                  file={maleVoiceRefFile}
                  onFileChange={setMaleVoiceRefFile}
                  saveLabel={maleVoiceRefSaveLabel}
                  onSaveLabelChange={setMaleVoiceRefSaveLabel}
                  onDelete={handleDeleteVoiceRef}
                />
                <VoiceRefField
                  label="Giọng Nữ"
                  savedRefs={savedVoiceRefs.filter((r) => r.kind === "female")}
                  selectedId={femaleVoiceRefId}
                  onSelectId={setFemaleVoiceRefId}
                  file={femaleVoiceRefFile}
                  onFileChange={setFemaleVoiceRefFile}
                  saveLabel={femaleVoiceRefSaveLabel}
                  onSaveLabelChange={setFemaleVoiceRefSaveLabel}
                  onDelete={handleDeleteVoiceRef}
                />
              </div>
            </div>
          ) : (
            <div className="grid gap-3 rounded border border-ink-700/60 bg-ink-900/30 p-4">
              <p className="text-xs text-ink-400">
                Tuỳ chọn: chọn giọng đã lưu, hoặc tải lên 1 mẫu giọng mới (3-5
                giây, rõ, ít tạp âm) để nhân bản (clone) dùng chung cho toàn bộ
                video. Bỏ trống thì dùng giọng mặc định.
              </p>
              <VoiceRefField
                label="Giọng dùng chung"
                savedRefs={savedVoiceRefs.filter((r) => r.kind === "single")}
                selectedId={voiceRefId}
                onSelectId={setVoiceRefId}
                file={voiceRefFile}
                onFileChange={setVoiceRefFile}
                saveLabel={voiceRefSaveLabel}
                onSaveLabelChange={setVoiceRefSaveLabel}
                onDelete={handleDeleteVoiceRef}
              />
            </div>
          )}

          <SubmitBar
            burnSubtitles={burnSubtitles}
            onBurnSubtitlesChange={setBurnSubtitles}
            musicVolume={musicVolume}
            onMusicVolumeChange={setMusicVolume}
            disabled={!canSubmit}
            submitting={submitting}
            onSubmit={handleSubmit}
            errorMessage={errorMessage}
            isProcessing={isProcessing}
            stopping={stopping}
            onStop={handleStop}
          />
        </section>

        <JobReel
          jobs={jobs}
          onDeleteJob={handleDeleteJob}
          onDeleteAllFinished={handleDeleteAllFinished}
        />
      </main>
    </div>
  );
}
