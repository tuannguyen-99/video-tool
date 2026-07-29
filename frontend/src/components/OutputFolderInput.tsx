import { IconFolder } from "./icons";

interface Props {
  value: string;
  onChange: (v: string) => void;
}

export default function OutputFolderInput({ value, onChange }: Props) {
  return (
    <div>
      <label className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-ink-500">
        <IconFolder className="text-ink-500" />
        Thư mục lưu kết quả trên server
        <span className="normal-case text-ink-700">(tuỳ chọn)</span>
      </label>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Để trống để tải video về qua trình duyệt"
        className="mt-2 w-full rounded border border-ink-700 bg-ink-800 px-3 py-2.5 font-mono text-sm text-ink-200 placeholder:text-ink-700 focus:border-reel-amber focus:outline-none"
      />
      <p className="mt-1 text-xs text-ink-500">
        Trình duyệt không thể mở hộp thoại chọn thư mục hệ thống thật. Để trống, video xong sẽ có nút
        "Tải xuống" dùng hộp thoại lưu file có sẵn của trình duyệt. Chỉ điền vào đây nếu backend chạy
        trên cùng máy và bạn muốn video lưu thẳng vào một đường dẫn cụ thể.
      </p>
    </div>
  );
}
