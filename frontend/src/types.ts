export type StageName =
  | "queued"
  | "download"
  | "speech_to_text"
  | "translate"
  | "text_to_speech"
  | "export"
  | "done";

export type JobStatus =
  | "pending"
  | "running"
  | "success"
  | "failed"
  | "cancelled";

export interface JobState {
  id: string;
  url: string;
  status: JobStatus;
  stage: StageName;
  failed_stage: StageName | null;
  error: string | null;
  output_filename: string | null;
  progress: number;
}

export const STAGE_LABEL: Record<StageName, string> = {
  queued: "Đang chờ",
  download: "Tải video",
  speech_to_text: "Nhận diện giọng nói",
  translate: "Dịch thuật",
  text_to_speech: "Tạo giọng đọc",
  export: "Xuất video",
  done: "Hoàn tất",
};

export const PIPELINE_STAGES: StageName[] = [
  "download",
  "speech_to_text",
  "translate",
  "text_to_speech",
  "export",
];
