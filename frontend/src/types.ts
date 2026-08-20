export type Platform = "douyin" | "bilibili";

export type JobStatus =
  | "pending"
  | "running"
  | "success"
  | "failed"
  | "cancelled";

export type StageName =
  | "queued"
  | "download"
  | "speech_to_text"
  | "translate"
  | "text_to_speech"
  | "export"
  | "done";

export interface OutputFile {
  filename: string;
  // Server filesystem path — present in payloads from the backend but not
  // needed/used by the frontend (downloads go through the API, not this
  // path directly).
  path?: string;
}

export interface JobState {
  id: string;
  url: string;
  platform: Platform;
  status: JobStatus;
  stage: StageName;
  failed_stage: StageName | null;
  error: string | null;
  // Whether this job requested splitting into 10-minute parts.
  split_long_video: boolean;
  // Whether TTS voice is auto-matched (male/female) to the source
  // speaker's detected voice instead of always using one fixed voice.
  match_voice_gender: boolean;
  // Set once match_voice_gender is on and detection produced a result
  // during the text_to_speech stage; null if the option is off or
  // detection was inconclusive (falls back to the default voice).
  detected_voice_gender: "male" | "female" | null;
  // Populated once the job succeeds. Normally one entry; more than one if
  // split_long_video kicked in.
  output_files: OutputFile[];
  // Legacy single-file fields, mirrored from output_files[0] by the backend.
  output_filename: string | null;
  output_path?: string | null;
  progress: number;
  title_vi: string;
}

export interface PIPELINE_STAGES {}
