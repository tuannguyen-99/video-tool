import { JobState } from "../types";

const API_BASE = (import.meta as any).env?.VITE_API_BASE ?? "";

function wsUrl(): string {
  if (API_BASE) {
    return API_BASE.replace(/^http/, "ws") + "/api/ws";
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/ws`;
}

/** Connects to the job status websocket and calls `onJob` for every job
 * update pushed by the backend (initial snapshot on connect, then one
 * message per change). Reconnects automatically with backoff if the
 * connection drops. Returns a cleanup function that closes the socket and
 * cancels any pending reconnect. */
export function connectJobSocket(onJob: (job: JobState) => void): () => void {
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let closedByCaller = false;
  let backoffMs = 1000;

  function connect() {
    socket = new WebSocket(wsUrl());

    socket.onmessage = (event) => {
      try {
        const job = JSON.parse(event.data) as JobState;
        onJob(job);
      } catch {
        // ignore malformed frames
      }
    };

    socket.onopen = () => {
      backoffMs = 1000;
    };

    socket.onclose = () => {
      if (closedByCaller) return;
      reconnectTimer = setTimeout(connect, backoffMs);
      backoffMs = Math.min(backoffMs * 2, 15000);
    };

    socket.onerror = () => {
      socket?.close();
    };
  }

  connect();

  return () => {
    closedByCaller = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    socket?.close();
  };
}
