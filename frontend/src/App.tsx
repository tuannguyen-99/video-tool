import { useEffect, useRef, useState } from "react";
import Header from "./components/Header";
import UrlInput from "./components/UrlInput";
import FileDrop from "./components/FileDrop";
import OutputFolderInput from "./components/OutputFolderInput";
import SubmitBar from "./components/SubmitBar";
import JobReel from "./components/JobReel";
import { IconCookie, IconMusic } from "./components/icons";
import { cancelAllJobs, createBatch } from "./lib/api";
import { connectJobSocket } from "./lib/ws";
import { JobState } from "./types";

const OUTPUT_DIR_STORAGE_KEY = "douyin-vietsub:outputDir";
const RESOLUTION_STORAGE_KEY = "douyin-vietsub:resolution";

export default function App() {
  const [urls, setUrls] = useState("");
  const [cookieFile, setCookieFile] = useState<File | null>(null);
  const [musicFile, setMusicFile] = useState<File | null>(null);
  const [outputDir, setOutputDir] = useState(() => {
    try {
      return localStorage.getItem(OUTPUT_DIR_STORAGE_KEY) ?? "";
    } catch {
      return "";
    }
  });
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

  function handleResolutionChange(v: "720p" | "1080p") {
    setResolution(v);
    try {
      localStorage.setItem(RESOLUTION_STORAGE_KEY, v);
    } catch {
      // localStorage unavailable — not critical, just skip persisting
    }
  }

  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  function handleOutputDirChange(v: string) {
    setOutputDir(v);
    try {
      localStorage.setItem(OUTPUT_DIR_STORAGE_KEY, v);
    } catch {
      // localStorage unavailable (private mode, etc.) — not critical, just skip persisting
    }
  }

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
  const canSubmit = urlCount > 0 && !!cookieFile;

  async function handleSubmit() {
    setErrorMessage(null);
    if (!cookieFile) {
      setErrorMessage("Vui lòng chọn file cookie để xác thực với Douyin.");
      return;
    }
    setSubmitting(true);
    try {
      const urlList = urls
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await createBatch({
        urls: urlList,
        outputDir: outputDir.trim(),
        cookieFile,
        musicFile,
        burnSubtitles,
        mixMusicVolume: musicVolume,
        resolution,
      });

      setTrackedIds((prev) => new Set([...prev, ...res.job_ids]));
      setJobsById((prev) => {
        const next = { ...prev };
        for (const id of res.job_ids) {
          next[id] = next[id] ?? {
            id,
            url: urlList[res.job_ids.indexOf(id)] ?? "",
            status: "pending",
            stage: "queued",
            failed_stage: null,
            error: null,
            output_filename: null,
            progress: 0,
          };
        }
        return next;
      });
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

  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 px-8 py-8">
        <section className="grid gap-4 rounded border border-ink-700 bg-ink-800/40 p-5">
          <UrlInput value={urls} onChange={setUrls} />

          <div className="grid gap-4 sm:grid-cols-2">
            <FileDrop
              label="File cookie (f2)"
              icon={<IconCookie />}
              accept=".txt,.json,.yaml,.yml"
              file={cookieFile}
              onChange={setCookieFile}
            />
            <FileDrop
              label="Nhạc nền mp3"
              icon={<IconMusic />}
              accept="audio/mpeg,.mp3"
              file={musicFile}
              onChange={setMusicFile}
              optional
            />
          </div>

          <OutputFolderInput
            value={outputDir}
            onChange={handleOutputDirChange}
          />

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

        <JobReel jobs={jobs} />
      </main>
    </div>
  );
}
