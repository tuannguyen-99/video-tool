interface Props {
  burnSubtitles: boolean;
  onBurnSubtitlesChange: (v: boolean) => void;
  musicVolume: number;
  onMusicVolumeChange: (v: number) => void;
  disabled: boolean;
  submitting: boolean;
  onSubmit: () => void;
  errorMessage: string | null;
  /** True while any job from the current/previous batch is still pending or running */
  isProcessing: boolean;
  /** True while a cancel-all request is in flight */
  stopping: boolean;
  onStop: () => void;
}

export default function SubmitBar({
  burnSubtitles,
  onBurnSubtitlesChange,
  musicVolume,
  onMusicVolumeChange,
  disabled,
  submitting,
  onSubmit,
  errorMessage,
  isProcessing,
  stopping,
  onStop,
}: Props) {
  return (
    <div className="rounded border border-ink-700 bg-ink-800/60 p-4">
      <div className="flex flex-wrap items-center gap-6">
        <label className="flex items-center gap-2 text-sm text-ink-200">
          <input
            type="checkbox"
            checked={burnSubtitles}
            onChange={(e) => onBurnSubtitlesChange(e.target.checked)}
            className="h-4 w-4 accent-reel-amber"
          />
          Gắn cứng vietsub vào video
        </label>

        <label className="flex flex-1 min-w-[200px] items-center gap-3 text-sm text-ink-200">
          Âm lượng nhạc nền
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={musicVolume}
            onChange={(e) => onMusicVolumeChange(parseFloat(e.target.value))}
            className="flex-1 accent-reel-amber"
          />
          <span className="font-mono text-xs text-ink-500 w-10 text-right">
            {Math.round(musicVolume * 100)}%
          </span>
        </label>

        <div className="ml-auto flex items-center gap-3">
          {isProcessing && (
            <button
              type="button"
              disabled={stopping}
              onClick={onStop}
              className="rounded border border-state-error px-5 py-2.5 font-display text-sm font-semibold text-state-error transition hover:bg-state-error/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {stopping ? "Đang dừng…" : "Dừng"}
            </button>
          )}
          <button
            type="button"
            disabled={disabled || submitting || isProcessing}
            onClick={onSubmit}
            title={
              isProcessing
                ? "Đang xử lý tác vụ, vui lòng chờ hoặc bấm Dừng"
                : undefined
            }
            className="rounded bg-reel-amber px-5 py-2.5 font-display text-sm font-semibold text-ink-950 transition hover:bg-reel-amberDim disabled:cursor-not-allowed disabled:bg-ink-700 disabled:text-ink-500"
          >
            {submitting ? "Đang gửi…" : "Bắt đầu xử lý"}
          </button>
        </div>
      </div>
      {errorMessage && (
        <p className="mt-3 text-sm text-state-error">{errorMessage}</p>
      )}
    </div>
  );
}
