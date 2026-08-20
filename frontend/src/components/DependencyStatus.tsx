import { useEffect, useState } from "react";
import {
  DependencyItem,
  fetchDependencies,
  installDependency,
} from "../lib/api";

const KIND_LABELS: Record<DependencyItem["kind"], string> = {
  binary: "Chương trình dòng lệnh",
  python_package: "Thư viện Python",
  model: "Model AI",
};

function StatusDot({ installed }: { installed: boolean }) {
  return (
    <span
      className={`inline-block h-2.5 w-2.5 flex-shrink-0 rounded-full ${
        installed ? "bg-emerald-500" : "bg-red-500"
      }`}
      aria-hidden
    />
  );
}

function DependencyRow({
  item,
  onInstalled,
}: {
  item: DependencyItem;
  onInstalled: () => void;
}) {
  const [installing, setInstalling] = useState(false);
  const [log, setLog] = useState<string[]>([]);
  const [showLog, setShowLog] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);

  async function handleInstall() {
    setInstalling(true);
    setInstallError(null);
    setShowLog(true);
    setLog([]);
    try {
      await installDependency(item.name, (line) =>
        setLog((prev) => [...prev, line]),
      );
      onInstalled();
    } catch (err) {
      setInstallError(err instanceof Error ? err.message : "Cài đặt thất bại.");
    } finally {
      setInstalling(false);
    }
  }

  return (
    <li className="rounded border border-ink-700 bg-ink-800/40 p-3">
      <div className="flex items-start gap-3">
        <div className="mt-1">
          <StatusDot installed={item.installed} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-mono text-sm font-medium text-ink-100">
              {item.name}
            </span>
            <span className="text-xs text-ink-500">
              {KIND_LABELS[item.kind]}
            </span>
          </div>
          <p className="text-sm text-ink-400">{item.description}</p>

          {item.installed ? (
            <p className="mt-1 truncate text-xs text-emerald-400 ">
              Đã cài{item.version ? ` — ${item.version}` : ""}
            </p>
          ) : (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <p className="text-xs text-red-300">
                Chưa cài. {item.install_hint}
              </p>
              {item.auto_installable && (
                <button
                  type="button"
                  onClick={handleInstall}
                  disabled={installing}
                  className="rounded border border-emerald-600 px-2 py-0.5 text-xs font-medium text-emerald-300 transition-colors hover:border-emerald-500 disabled:opacity-50"
                >
                  {installing ? "Đang cài…" : "Cài đặt"}
                </button>
              )}
            </div>
          )}

          {installError && (
            <p className="mt-1 text-xs text-red-300">{installError}</p>
          )}

          {showLog && log.length > 0 && (
            <pre className="mt-2 max-h-40 overflow-auto rounded bg-black/40 p-2 text-[11px] leading-relaxed text-ink-400">
              {log.join("\n")}
            </pre>
          )}
        </div>
      </div>
    </li>
  );
}

export default function DependencyStatus() {
  const [items, setItems] = useState<DependencyItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchDependencies();
      setItems(data);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Không thể tải trạng thái hệ thống.",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const missingCount = items?.filter((i) => !i.installed).length ?? 0;

  return (
    <div className="grid gap-3 rounded border border-ink-700 bg-ink-800/40 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium text-ink-100">
            Trạng thái cài đặt hệ thống
          </h2>
          {items && (
            <p className="text-xs text-ink-500">
              {missingCount === 0
                ? "Tất cả dependency bắt buộc đã sẵn sàng."
                : `${missingCount} dependency đang thiếu.`}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="rounded border border-ink-700 px-3 py-1.5 text-xs font-medium text-ink-300 transition-colors hover:border-ink-600 disabled:opacity-50"
        >
          {loading ? "Đang kiểm tra…" : "Kiểm tra lại"}
        </button>
      </div>

      {error && <p className="text-sm text-red-300">{error}</p>}

      {items && (
        <ul className="grid gap-2">
          {items.map((item) => (
            <DependencyRow key={item.name} item={item} onInstalled={load} />
          ))}
        </ul>
      )}

      {!items && loading && (
        <p className="text-sm text-ink-400">Đang kiểm tra…</p>
      )}
    </div>
  );
}
