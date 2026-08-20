"""
Persistent storage for voice-clone reference clips the user wants to reuse
across batches, instead of re-uploading the same 3-5s sample every time.

Separate from storage.py's per-batch upload folder (which is scoped to one
batch's jobs and gets treated as disposable) — these live in their own
directory and stick around until explicitly deleted via the API, tracked
in a small JSON index alongside the saved audio files.

Not safe across multiple worker processes — same assumption the rest of
this app already makes (JobManager is an in-memory singleton), so this is
fine for the single-process deployment this app is built for, but would
need a real datastore if that ever changes.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional, TypedDict

VoiceRefKind = Literal["male", "female", "single"]

# Overridable via env var in case the deployment wants these somewhere
# other than the backend operator's home directory (e.g. a mounted volume
# in a container).
_BASE_DIR = os.environ.get(
    "VOICE_REFS_DIR", os.path.expanduser("~/.douyin-vietsub/voice_refs")
)
_INDEX_PATH = os.path.join(_BASE_DIR, "index.json")


class VoiceRef(TypedDict):
    id: str
    kind: VoiceRefKind
    label: str
    filename: str  # relative to _BASE_DIR
    created_at: str


def _ensure_dir() -> None:
    os.makedirs(_BASE_DIR, exist_ok=True)


def _load_index() -> List[VoiceRef]:
    _ensure_dir()
    if not os.path.exists(_INDEX_PATH):
        return []
    try:
        with open(_INDEX_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        # Corrupt/unreadable index — treat as empty rather than taking the
        # whole app down; worst case the user just re-saves their voices.
        return []


def _save_index(items: List[VoiceRef]) -> None:
    _ensure_dir()
    # Write to a temp file then atomically replace, so a crash mid-write
    # can't leave index.json half-written/corrupted.
    tmp_path = _INDEX_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _INDEX_PATH)


def list_voice_refs(kind: Optional[VoiceRefKind] = None) -> List[VoiceRef]:
    """Newest first, optionally filtered to one kind (male/female/single)."""
    items = _load_index()
    if kind is not None:
        items = [it for it in items if it["kind"] == kind]
    return sorted(items, key=lambda it: it["created_at"], reverse=True)


def save_voice_ref(kind: VoiceRefKind, label: str, filename: str, data: bytes) -> VoiceRef:
    """Persists a voice-clone reference clip so it can be reused in later
    batches without re-uploading. `data` is the raw audio bytes (already
    read from the upload — callers must read the UploadFile themselves
    since it can only be consumed once)."""
    _ensure_dir()
    ref_id = uuid.uuid4().hex[:12]
    ext = os.path.splitext(filename)[1] or ".wav"
    stored_filename = f"{ref_id}{ext}"
    with open(os.path.join(_BASE_DIR, stored_filename), "wb") as fh:
        fh.write(data)

    record: VoiceRef = {
        "id": ref_id,
        "kind": kind,
        "label": label.strip() or filename,
        "filename": stored_filename,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    items = _load_index()
    items.append(record)
    _save_index(items)
    return record


def get_voice_ref_path(ref_id: str) -> Optional[str]:
    for it in _load_index():
        if it["id"] == ref_id:
            return os.path.join(_BASE_DIR, it["filename"])
    return None


def delete_voice_ref(ref_id: str) -> bool:
    items = _load_index()
    remaining = [it for it in items if it["id"] != ref_id]
    if len(remaining) == len(items):
        return False
    removed = next(it for it in items if it["id"] == ref_id)
    try:
        os.remove(os.path.join(_BASE_DIR, removed["filename"]))
    except OSError:
        pass  # file already gone somehow — index entry still gets dropped
    _save_index(remaining)
    return True
