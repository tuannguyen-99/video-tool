import { IconLink } from "./icons";

interface Props {
  value: string;
  onChange: (v: string) => void;
}

export default function UrlInput({ value, onChange }: Props) {
  const count = value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean).length;

  return (
    <div>
      <label className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-ink-500">
        <IconLink className="text-ink-500" />
        URL video Douyin
        <span className="ml-auto font-mono normal-case text-reel-amber">{count} link</span>
      </label>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={3}
        placeholder="https://www.douyin.com/video/..., https://v.douyin.com/..., ..."
        className="mt-2 w-full resize-none rounded border border-ink-700 bg-ink-800 px-3 py-2.5 font-mono text-sm text-ink-200 placeholder:text-ink-700 focus:border-reel-amber focus:outline-none"
      />
      <p className="mt-1 text-xs text-ink-500">Nhiều URL cách nhau bằng dấu phẩy ( , )</p>
    </div>
  );
}
