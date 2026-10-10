"""
Speech-to-text. Two selectable engines via the STT_ENGINE env var:

  funasr (DEFAULT) — FunASR's SenseVoice model (Alibaba/Tongyi Lab).
      Purpose-built for Chinese/Asian-language ASR: measured character
      error rate roughly 2.7x lower than Whisper on Chinese benchmarks
      (~7.8% vs faster-whisper large-v3-turbo's ~21.7% CER), while also
      being smaller (~254MB quantized vs ~1.5GB for the Whisper model this
      app used before) and faster on CPU. Made the default because every
      video this app transcribes is Chinese source audio (Douyin/
      Bilibili) — a general 99-language model like Whisper trades away
      exactly the accuracy this app's one real input language needs most.
      Install: pip install funasr

  whisper — faster-whisper, what this app used exclusively before FunASR
      was added. Kept fully working and selectable as a fallback — e.g. if
      FunASR causes dependency conflicts or produces worse output on some
      video — without needing to uninstall anything or touch this file:
      set STT_ENGINE=whisper and restart.
      Install: pip install faster-whisper

Both loaded lazily and cached as module-level singletons (loading is the
expensive part), and only the selected engine's package needs to actually
be installed/working — the other's import is never touched unless you
switch STT_ENGINE to it.
"""
from __future__ import annotations

import asyncio
import os
import re
import wave
from dataclasses import dataclass
from typing import Callable, List, Optional

# Kept as a module constant (not just inline) so deps_check.py can
# check/report on the exact same default Whisper model — instead of
# duplicating this string and risking the two drifting apart. Still
# relevant even with funasr as the default engine, since STT_ENGINE=whisper
# remains a fully supported fallback.
DEFAULT_WHISPER_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
DEFAULT_FUNASR_MODEL = "iic/SenseVoiceSmall"


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _stt_engine() -> str:
    engine = os.environ.get("STT_ENGINE", "funasr").strip().lower()
    if engine not in ("funasr", "whisper"):
        raise ValueError(
            f"STT_ENGINE={engine!r} không hợp lệ — chỉ hỗ trợ 'funasr' hoặc 'whisper'"
        )
    return engine


async def transcribe(
    audio_path: str,
    source_lang: str = "zh",
    on_progress: Optional[Callable[[float], None]] = None,
) -> List[Segment]:
    """`on_progress`, nếu có, nhận một float 0-100 khi tiến độ nhận dạng
    thay đổi — cả hai engine (funasr, whisper) đều hỗ trợ. Chạy trong
    worker thread qua asyncio.to_thread() nên callback bị gọi từ thread đó
    — người gọi (job_manager.py) cần loop.call_soon_threadsafe() nếu
    callback đụng tới state của asyncio, giống hệt cách
    translate.translate_segments() đã làm."""
    engine = _stt_engine()
    if engine == "whisper":
        return await asyncio.to_thread(
            _transcribe_whisper_sync, audio_path, source_lang, on_progress
        )
    return await asyncio.to_thread(
        _transcribe_funasr_sync, audio_path, source_lang, on_progress
    )


# ---------------------------------------------------------------------------
# FunASR (SenseVoice) — default engine
# ---------------------------------------------------------------------------

_funasr_vad_model = None
_funasr_asr_model = None

# SenseVoice's `language` param wants one of these short codes, or "auto" to
# detect per-utterance — anything else (e.g. faster-whisper-style "zh-CN")
# isn't recognized. Falls back to "auto" rather than passing through an
# unsupported code that SenseVoice would just reject outright.
_FUNASR_SUPPORTED_LANGS = {"zh", "en", "yue", "ja", "ko"}

# ============================================================================
# SEGMENTATION TUNING — edit these constants directly and restart the
# backend to take effect. No env vars needed.
# ============================================================================

# Passed to fsmn-vad as vad_kwargs.max_single_segment_time — caps how long
# a single detected speech region can run before VAD force-splits it.
# Lower = finer/more segments straight from VAD itself; higher = fewer,
# longer regions (then relies more on FUNASR_HARD_SPLIT_MS and
# sentence-punctuation splitting to break it up further).
FUNASR_VAD_MAX_SEGMENT_MS = 20000

# Consecutive VAD speech regions separated by a gap shorter than this get
# merged into one chunk (up to FUNASR_MERGE_TARGET_MS long) rather than
# each brief pause producing its own tiny fragment. Lower = more (shorter)
# fragments; higher = fewer (longer) merged chunks.
FUNASR_MERGE_MAX_GAP_MS = 1000

# Target max length (ms) when merging nearby VAD regions together —
# mirrors the spirit of FunASR's own merge_vad/merge_length_s tutorial
# setting. Lower = more, shorter ASR-call chunks (slower overall, since
# each chunk is a separate model call, but each individual chunk's
# sentence-splitting has less text to divide time across); higher = fewer,
# longer chunks.
FUNASR_MERGE_TARGET_MS = 15000

# Safety net independent of the VAD model's own max_single_segment_time: if
# a single raw region still comes back longer than this (e.g. an older
# funasr/fsmn-vad build ignoring that setting, or continuous noise VAD
# never finds a gap in), force-split it into equal chunks rather than
# handing one giant blob to ASR.
FUNASR_HARD_SPLIT_MS = 25000

# Characters treated as sentence-enders when splitting a region's
# (SenseVoice-native-punctuated) text into per-sentence segments — THE MAIN
# KNOB if segments are still too long/coarse: SenseVoice outputs
# punctuation natively, but if a stretch of speech has few/no
# sentence-ending marks (e.g. run-on narration using mostly commas),
# nothing here will split it further.
#
# "，" (full-width comma) is included by default: some source videos
# narrate in one long comma-joined run with only a single "。" at the very
# end (a whole ~15-20s VAD region coming back as one sentence) — that
# produces one giant subtitle block that ffmpeg then wraps into 7-8 lines
# on screen, exactly the "chữ vietsub quá dài" case. Splitting on commas
# too trades that off for shorter-but-choppier segments, which reads far
# better as burned-in subtitles. Remove "，" here (back to "。！？!?") if a
# given source's commas are meaningful mid-clause and the choppiness reads
# worse than the long blocks did.
FUNASR_SENTENCE_END_CHARS = "。！？!?，"


def _funasr_hub_kwargs() -> dict:
    # FunASR downloads model weights from ModelScope by default, which is
    # fast inside China but can be slow/unreliable elsewhere. Set
    # FUNASR_HUB=hf to download from Hugging Face instead.
    hub = os.environ.get("FUNASR_HUB")
    return {"hub": hub} if hub else {}


def _get_funasr_vad_model():
    """Standalone VAD model — used to find speech regions ourselves rather
    than relying on an ASR+VAD combined pipeline's internal sentence
    segmentation (`sentence_info`), which turned out to be inconsistent
    across funasr versions/configs for SenseVoice (came back empty for
    long audio in practice, silently collapsing a whole video into one
    giant segment — see the length guard this replaced). This split keeps
    segmentation entirely under our control and independent of that
    internal behavior."""
    global _funasr_vad_model
    if _funasr_vad_model is None:
        from funasr import AutoModel

        _funasr_vad_model = AutoModel(
            model=os.environ.get("FUNASR_VAD_MODEL", "fsmn-vad"),
            device=os.environ.get("FUNASR_DEVICE", "cpu"),
            disable_update=True,
            vad_kwargs={"max_single_segment_time": FUNASR_VAD_MAX_SEGMENT_MS},
            **_funasr_hub_kwargs(),
        )
    return _funasr_vad_model


def _get_funasr_asr_model():
    """ASR-only model (no vad_model attached) — runs against audio chunks
    already trimmed to a VAD-detected speech region by
    _transcribe_funasr_sync, one call per region."""
    global _funasr_asr_model
    if _funasr_asr_model is None:
        from funasr import AutoModel

        _funasr_asr_model = AutoModel(
            model=os.environ.get("FUNASR_MODEL", DEFAULT_FUNASR_MODEL),
            device=os.environ.get("FUNASR_DEVICE", "cpu"),
            disable_update=True,
            **_funasr_hub_kwargs(),
        )
    return _funasr_asr_model


def _load_pcm_i16(audio_path: str):
    """Reads raw int16 PCM samples straight from the WAV file — audio_path
    is always the 16kHz mono PCM wav mux.extract_audio() produced. Used to
    slice out each VAD region's audio without any lossy float round-trip."""
    import numpy as np

    with wave.open(audio_path, "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16), sr


def _write_wav_chunk(samples_i16, sr: int, path: str) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples_i16.tobytes())


def _split_long_regions(regions_ms: List[List[int]]) -> List[List[int]]:
    """Force-splits any single region longer than FUNASR_HARD_SPLIT_MS into
    consecutive equal-ish chunks. A hard cut mid-speech isn't as clean as
    splitting at a real pause, but it's far better than one multi-minute
    blob desyncing subtitle timing/TTS placement and blowing past
    translation API length limits."""
    hard_split_ms = FUNASR_HARD_SPLIT_MS
    result: List[List[int]] = []
    for start, end in regions_ms:
        duration = end - start
        if duration <= hard_split_ms:
            result.append([start, end])
            continue
        num_chunks = -(-duration // hard_split_ms)  # ceil division
        chunk_len = duration / num_chunks
        pos = start
        for _ in range(num_chunks):
            next_pos = min(int(pos + chunk_len), end)
            result.append([int(pos), next_pos])
            pos = next_pos
    return result


def _merge_vad_regions(regions_ms: List[List[int]]) -> List[List[int]]:
    if not regions_ms:
        return []
    max_gap_ms = FUNASR_MERGE_MAX_GAP_MS
    target_ms = FUNASR_MERGE_TARGET_MS
    merged = [list(regions_ms[0])]
    for start, end in regions_ms[1:]:
        last = merged[-1]
        gap = start - last[1]
        prospective_duration = end - last[0]
        if gap <= max_gap_ms and prospective_duration <= target_ms:
            last[1] = end
        else:
            merged.append([start, end])
    return merged


def _has_real_content(text: str) -> bool:
    """True if `text` has at least one actual letter/digit (in any
    language/script — Python's str.isalnum() covers Chinese/Vietnamese/etc,
    not just ASCII). Guards against SenseVoice occasionally emitting a
    punctuation-only "sentence" (e.g. "...." or "。。。") for a VAD region
    that turned out to be background noise/music rather than real speech —
    such text isn't empty (so a plain `.strip()` check misses it), but has
    nothing to translate/voice, and sending it to the translation API can
    fail outright (some backends reject near-empty/punctuation-only input
    with a validation error rather than returning it unchanged)."""
    return any(ch.isalnum() for ch in text)


# SenseVoice's rich_transcription_postprocess() embeds emotion/event tags
# as emoji directly in the output text — 😊/😡/😔 for detected emotion, 🎼
# for background music, 👏 for applause, etc. These describe the audio,
# they are not words to be spoken or translated, but nothing downstream
# (translate.py, tts.py) knows that once they're inlined into plain text.
# Left in, a segment that's entirely (or mostly) one of these symbols
# gives the TTS engine nothing speakable to synthesize (surfaces as
# vieneu's "No valid speech tokens found in the output."), and a mixed
# segment like "🎼穿越后，" still carries the symbol all the way into the
# Vietnamese subtitle/audio for no reason. Stripped once here, right after
# postprocessing, so nothing downstream (translation, TTS, the .srt file)
# ever sees it.
_ANNOTATION_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # misc symbols/pictographs, emoticons, supplemental symbols, transport, etc.
    "\U00002600-\U000027BF"  # misc symbols & dingbats
    "\U0000FE0F"             # variation selector-16
    "\U0000200D"             # zero-width joiner (emoji sequences)
    "]+"
)


def strip_annotation_emoji(text: str) -> str:
    """Removes SenseVoice's inlined emotion/event emoji and collapses the
    whitespace left behind. Public (not underscore-prefixed) since tts.py
    also uses this defensively right before synthesis, as a last check in
    case emoji reach it from some other path (e.g. hand-edited segments)."""
    return re.sub(r"\s+", " ", _ANNOTATION_EMOJI_RE.sub("", text)).strip()


# SenseVoice outputs punctuation natively (unlike Paraformer, which needs a
# separate punc_model) — so a single VAD-region ASR call can come back as
# several sentences run together, e.g. "第一句。第二句！第三句？". Without
# splitting on this, every region becomes one coarse ~15-25s subtitle line
# instead of per-sentence lines like Whisper naturally produces. Which
# characters count as sentence-enders is configurable via
# FUNASR_SENTENCE_END_CHARS (see _funasr_sentence_end_chars) — this is the
# main knob if segments are still too long/coarse.


def _split_into_sentences(text: str) -> List[str]:
    if not text:
        return []
    end_chars = re.escape(FUNASR_SENTENCE_END_CHARS)
    sentence_end_re = re.compile(f"([{end_chars}]+)")
    parts = sentence_end_re.split(text)
    sentences: List[str] = []
    buf = ""
    for part in parts:
        buf += part
        if sentence_end_re.fullmatch(part):
            stripped = buf.strip()
            if stripped:
                sentences.append(stripped)
            buf = ""
    tail = buf.strip()
    if tail:
        sentences.append(tail)
    return sentences


def _distribute_sentences_over_time(sentences: List[str], start_ms: int, end_ms: int) -> List[Segment]:
    """Splits a VAD region's [start_ms, end_ms] proportionally across its
    sentences by character count. This is an approximation — SenseVoice
    doesn't give per-character/word timestamps, only region-level ones —
    but it's the standard technique subtitle tools use when finer timing
    isn't available, and it's far more accurate than treating the whole
    region as one line: a short sentence gets a short slot, a long one
    gets a long slot, instead of every sentence in the region claiming the
    full region duration."""
    if not sentences:
        return []
    if len(sentences) == 1:
        return [Segment(start=start_ms / 1000.0, end=end_ms / 1000.0, text=sentences[0])]

    total_chars = sum(len(s) for s in sentences) or 1
    duration = end_ms - start_ms
    result: List[Segment] = []
    pos = float(start_ms)
    for i, sentence in enumerate(sentences):
        is_last = i == len(sentences) - 1
        seg_end = float(end_ms) if is_last else pos + duration * (len(sentence) / total_chars)
        result.append(Segment(start=pos / 1000.0, end=seg_end / 1000.0, text=sentence))
        pos = seg_end
    return result


def _transcribe_funasr_sync(
    audio_path: str,
    source_lang: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> List[Segment]:
    import tempfile

    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    lang = source_lang.split("-")[0].lower()
    if lang not in _FUNASR_SUPPORTED_LANGS:
        lang = "auto"

    samples, sr = _load_pcm_i16(audio_path)

    vad_model = _get_funasr_vad_model()
    vad_result = vad_model.generate(input=audio_path)
    # Standalone fsmn-vad returns speech regions as [[start_ms, end_ms], ...]
    # under the "value" key.
    regions_ms = vad_result[0].get("value") if vad_result else None
    if not regions_ms:
        if on_progress is not None:
            on_progress(100.0)
        return []  # no speech detected at all (silence/pure music) — not an error

    regions_ms = _split_long_regions(regions_ms)
    regions_ms = _merge_vad_regions(regions_ms)
    asr_model = _get_funasr_asr_model()

    # Progress weighted by SPEECH DURATION covered so far, not by "which
    # region out of N" — regions vary a lot in length (anywhere up to
    # FUNASR_HARD_SPLIT_MS), so a count-based proxy would jump unevenly
    # (e.g. many short regions finishing fast, then stalling on one long
    # one). ASR inference time roughly tracks audio duration processed,
    # making this a reasonably accurate proxy — same reasoning as ffmpeg's
    # actual-timestamp-based progress in mux.py, just without an exact
    # ground truth like ffmpeg's -progress gives.
    total_speech_ms = sum(end_ms - start_ms for start_ms, end_ms in regions_ms) or 1
    processed_ms = 0

    segments: List[Segment] = []
    for start_ms, end_ms in regions_ms:
        # Counted (and on_progress called) unconditionally for every
        # region up front, before any of the "continue"s below — so
        # progress always reaches exactly 100% once the loop finishes,
        # regardless of how many regions turned out to have no usable
        # speech in them.
        processed_ms += end_ms - start_ms
        if on_progress is not None:
            on_progress(min(100.0, processed_ms / total_speech_ms * 100.0))

        start_sample = max(int(start_ms / 1000.0 * sr), 0)
        end_sample = min(int(end_ms / 1000.0 * sr), samples.size)
        if end_sample <= start_sample:
            continue

        # Written to a temp wav and passed by path rather than as a raw
        # array — every documented FunASR usage pattern passes a file path
        # to generate(), so this avoids relying on unconfirmed ndarray+fs
        # kwarg support for the ASR-only model.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_wav_chunk(samples[start_sample:end_sample], sr, tmp_path)
            result = asr_model.generate(
                input=tmp_path,
                language=lang,
                use_itn=True,  # inverse text normalization: "一百二" -> "120" etc
            )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if not result:
            continue
        text = strip_annotation_emoji(rich_transcription_postprocess(result[0].get("text", "")))
        if not text or not _has_real_content(text):
            continue

        sentences = [s for s in _split_into_sentences(text) if _has_real_content(s)]
        if not sentences:
            continue
        segments.extend(_distribute_sentences_over_time(sentences, start_ms, end_ms))

    return segments


# ---------------------------------------------------------------------------
# faster-whisper — fallback engine (STT_ENGINE=whisper)
# ---------------------------------------------------------------------------

_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        # WhisperModel accepts either a short size name (tiny/base/small/
        # medium/large-v3/...) or, as here, a full HuggingFace repo id of a
        # CT2-converted model — it downloads/caches it via huggingface_hub
        # the same way either way. First run for a given WHISPER_MODEL will
        # download it (~1.5GB at int8 for this one).
        #
        # Override via WHISPER_MODEL, e.g. back to "medium" for a smaller/
        # faster download, or another CT2 repo id.
        model_size = os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL)
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        device = os.environ.get("WHISPER_DEVICE", "auto")
        _whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _whisper_model


def _transcribe_whisper_sync(
    audio_path: str,
    source_lang: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> List[Segment]:
    model = _get_whisper_model()
    # faster-whisper wants a bare language code like "zh", not "zh-CN"
    lang = source_lang.split("-")[0]
    segments_iter, info = model.transcribe(audio_path, language=lang, vad_filter=True)
    # info.duration is known immediately (faster-whisper computes it from
    # the audio/VAD pass up front), BEFORE any actual decoding happens —
    # decoding only proceeds as segments_iter is consumed below, since
    # it's a lazy generator. That laziness is exactly what makes real
    # progress possible here: each `for seg in segments_iter` iteration is
    # one more chunk actually decoded, and seg.end (seconds into the
    # audio) divided by info.duration gives real, accurate progress — the
    # same quality as ffmpeg's out_time_us in mux.py, not a count-based
    # proxy like translate.py's batch counting has to fall back to.
    duration_s = info.duration or 0.0

    segments: List[Segment] = []
    for seg in segments_iter:
        if on_progress is not None and duration_s > 0:
            on_progress(min(100.0, seg.end / duration_s * 100.0))
        if seg.text.strip():
            segments.append(Segment(start=seg.start, end=seg.end, text=seg.text.strip()))

    if on_progress is not None:
        on_progress(100.0)
    return segments