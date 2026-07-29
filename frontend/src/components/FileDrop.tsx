import { ReactNode, useRef } from "react";

interface Props {
  label: string;
  icon: ReactNode;
  accept?: string;
  file: File | null;
  onChange: (file: File | null) => void;
  optional?: boolean;
}

export default function FileDrop({ label, icon, accept, file, onChange, optional }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div>
      <label className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-ink-500">
        <span className="text-ink-500">{icon}</span>
        {label}
        {optional && <span className="normal-case text-ink-700">(tuỳ chọn)</span>}
      </label>
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        className="mt-2 flex w-full items-center justify-between rounded border border-dashed border-ink-700 bg-ink-800 px-3 py-2.5 text-left text-sm text-ink-200 transition hover:border-reel-amber/60"
      >
        <span className={file ? "truncate text-ink-200" : "text-ink-700"}>
          {file ? file.name : "Chọn tệp…"}
        </span>
        {file && (
          <span
            role="button"
            onClick={(e) => {
              e.stopPropagation();
              onChange(null);
              if (inputRef.current) inputRef.current.value = "";
            }}
            className="ml-3 font-mono text-xs text-ink-500 hover:text-state-error"
          >
            xoá
          </span>
        )}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(e) => onChange(e.target.files?.[0] ?? null)}
      />
    </div>
  );
}
