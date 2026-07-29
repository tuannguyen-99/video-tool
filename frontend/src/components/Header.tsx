import { IconClapper } from "./icons";

export default function Header() {
  return (
    <header className="flex items-center justify-between border-b border-ink-700 px-8 py-5">
      <div className="flex items-center gap-3">
        <span className="flex h-9 w-9 items-center justify-center rounded bg-reel-amber/15 text-reel-amber">
          <IconClapper />
        </span>
        <div>
          <h1 className="font-display text-lg font-semibold text-ink-200">
            Douyin <span className="text-reel-amber">→</span> Vietsub Studio
          </h1>
          <p className="font-mono text-[11px] uppercase tracking-widest text-ink-500">
            download · speech-to-text · dịch · tts · export
          </p>
        </div>
      </div>
    </header>
  );
}
