import { JobState } from "../types";

export function connectJobSocket(onMessage: (job: JobState) => void): () => void {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${window.location.host}/api/ws`;
  const socket = new WebSocket(url);

  socket.onmessage = (event) => {
    try {
      const job = JSON.parse(event.data) as JobState;
      onMessage(job);
    } catch {
      // ignore malformed frames
    }
  };

  return () => socket.close();
}
