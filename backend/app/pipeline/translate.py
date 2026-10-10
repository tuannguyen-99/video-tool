"""
Chinese -> Vietnamese translation, chạy hoàn toàn local/offline.

Hai engine local, chọn qua TRANSLATE_ENGINE (hoặc tham số `engine=` truyền
theo từng job từ UI):

  nllb (mặc định) — Meta NLLB-200 distilled-600M, đa ngôn ngữ, chạy qua
      CTranslate2. Cần bước convert một lần bằng ct2-transformers-converter
      (xem download_nllb_model() trong deps_check.py). Dịch được nhiều cặp
      ngôn ngữ, chất lượng khá đều, nhưng nặng hơn và văn phong trung tính.

  hachimi — ngocdang83/HachimiMT-60-zh-vi, model Marian ~57M tham số,
      CHỈ dịch zh -> vi, train riêng cho truyện mạng (tiên hiệp, đô thị).
      Nhỏ hơn NLLB khoảng 10 lần nên nhanh hơn nhiều trên CPU, và văn
      phong Hán-Việt (xưng hô, tên riêng) hợp truyện hơn hẳn NLLB. Repo
      trên HF đã có sẵn bản export CTranslate2 int8 trong thư mục
      'ct2-int8_float32/' — KHÔNG cần convert, không cần torch, chỉ cần
      tải snapshot về là chạy (xem download_hachimi_model() trong
      deps_check.py).

Cả hai đều không gọi mạng lúc dịch, không rate limit, không quota, không
API key. Các engine cloud cũ (Google Translate qua deep_translator,
MyMemory) đã bị bỏ hoàn toàn: không còn retry/backoff/fallback. Một lỗi ở
đây là lỗi thật (sai đường dẫn model, model hỏng, hết RAM), không phải
block tạm thời, nên code fail fast thay vì retry mù.

Cài chung cho cả hai engine:
    pip install ctranslate2 transformers sentencepiece

Biến môi trường (tất cả đều optional):
  TRANSLATE_ENGINE      "nllb" (mặc định) hoặc "hachimi" — chỉ là default
                        cho toàn server; mỗi job có thể override qua
                        tham số engine= của translate_segments().

  NLLB_MODEL_DIR        thư mục model đã convert (mặc định: hằng số bên dưới)
  NLLB_TOKENIZER        repo id tokenizer HF (mặc định repo gốc 600M)
  NLLB_DEVICE           "cpu" (mặc định) hoặc "cuda"
  NLLB_COMPUTE_TYPE     mặc định "default" (để CTranslate2 tự chọn)

  HACHIMI_MODEL_ID      repo id HF (mặc định ngocdang83/HachimiMT-60-zh-vi)
  HACHIMI_MODEL_DIR     trỏ thẳng tới snapshot đã tải sẵn, bỏ qua HF cache
  HACHIMI_CT2_SUBDIR    thư mục con chứa bản CT2 (mặc định ct2-int8_float32)
  HACHIMI_DEVICE        "cpu" (mặc định) hoặc "cuda"
  HACHIMI_COMPUTE_TYPE  mặc định "int8_float32" (đúng bản export trong repo)

  Chung cho cả hai:
  TRANSLATE_BEAM_SIZE      mặc định 4
  TRANSLATE_BATCH_SIZE     số đoạn dịch chung một lượt, mặc định 16
  TRANSLATE_INTER_THREADS  mặc định 1
  TRANSLATE_INTRA_THREADS  mặc định 0 (CTranslate2 tự chọn theo số core)
"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import Callable, Dict, List, Optional, Tuple

from .transcribe import Segment


class TranslationError(Exception):
    pass


# Tên engine hợp lệ — dùng chung cho validate ở đây, cho deps_check.py và
# cho whitelist của API ở main.py, để ba chỗ không bị lệch nhau.
ENGINE_NLLB = "nllb"
ENGINE_HACHIMI = "hachimi"
SUPPORTED_ENGINES = (ENGINE_NLLB, ENGINE_HACHIMI)
DEFAULT_ENGINE = ENGINE_NLLB


def default_engine() -> str:
    """Engine mặc định của server, đọc fresh từ env mỗi lần (không cache
    lúc import) để đổi env rồi là có hiệu lực ngay, không cần restart."""
    engine = os.environ.get("TRANSLATE_ENGINE", DEFAULT_ENGINE).strip().lower()
    if engine not in SUPPORTED_ENGINES:
        raise TranslationError(
            f"TRANSLATE_ENGINE={engine!r} không hợp lệ — chỉ hỗ trợ "
            f"{' hoặc '.join(repr(e) for e in SUPPORTED_ENGINES)}."
        )
    return engine


def _resolve_engine(engine: Optional[str]) -> str:
    if engine is None:
        return default_engine()
    normalized = engine.strip().lower()
    if normalized not in SUPPORTED_ENGINES:
        raise TranslationError(
            f"Engine dịch {engine!r} không hợp lệ — chỉ hỗ trợ "
            f"{' hoặc '.join(repr(e) for e in SUPPORTED_ENGINES)}."
        )
    return normalized


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise TranslationError(f"{name}={raw!r} không phải số nguyên hợp lệ")


def _beam_size() -> int:
    return max(1, _env_int("TRANSLATE_BEAM_SIZE", 4))


def _batch_size() -> int:
    return max(1, _env_int("TRANSLATE_BATCH_SIZE", 16))


def _ct2_threads() -> Tuple[int, int]:
    return (
        _env_int("TRANSLATE_INTER_THREADS", 1),
        _env_int("TRANSLATE_INTRA_THREADS", 0),
    )


def _require_ct2() -> None:
    try:
        import ctranslate2  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as e:
        raise TranslationError(
            "Thiếu thư viện cho engine dịch local. Cài bằng: "
            "pip install ctranslate2 transformers sentencepiece"
        ) from e


# ---------------------------------------------------------------------------
# NLLB-200 (đa ngôn ngữ, cần convert một lần)
# ---------------------------------------------------------------------------

# Vị trí mặc định mà nút "Cài đặt" ở trang system-dependencies convert
# NLLB-200 vào (xem download_nllb_model() trong deps_check.py) khi
# NLLB_MODEL_DIR không được set. Giữ làm module constant (không inline)
# để deps_check.py import đúng hằng số này thay vì lặp lại chuỗi và bị
# lệch nhau — cùng lý do như DEFAULT_WHISPER_MODEL trong transcribe.py.
DEFAULT_NLLB_MODEL_DIR = os.path.expanduser(
    "~/.cache/vietsub-tool/nllb-200-distilled-600M-ct2"
)

# Mã FLORES-200 mà NLLB cần, map từ mã ngắn kiểu Google mà app này truyền
# qua lại (source_lang="zh-CN"/"zh", target_lang="vi").
_NLLB_LANG_CODES = {
    "zh": "zho_Hans",
    "zh-cn": "zho_Hans",
    "zh-hans": "zho_Hans",
    "zh-tw": "zho_Hant",
    "zh-hant": "zho_Hant",
    "vi": "vie_Latn",
    "vi-vn": "vie_Latn",
    "en": "eng_Latn",
    "en-us": "eng_Latn",
    "en-gb": "eng_Latn",
}

_nllb_translator = None
_nllb_tokenizer = None


def _to_nllb_code(code: str) -> str:
    normalized = code.strip().lower()
    if normalized in _NLLB_LANG_CODES:
        return _NLLB_LANG_CODES[normalized]
    raise TranslationError(
        f"Không có mapping FLORES-200 cho mã ngôn ngữ {code!r} trong "
        f"_NLLB_LANG_CODES (translate.py) — thêm mã đó vào rồi thử lại."
    )


def _get_nllb_model():
    """Load model CTranslate2 NLLB-200 + tokenizer HF một lần rồi cache
    cả hai thành module-level singleton — giống hệt _get_funasr_model()/
    _get_whisper_model() trong transcribe.py, cùng lý do: load mới là
    phần đắt, còn mỗi lần dịch thì không."""
    global _nllb_translator, _nllb_tokenizer
    if _nllb_translator is None:
        _require_ct2()
        import ctranslate2
        import transformers

        model_dir = os.environ.get("NLLB_MODEL_DIR", DEFAULT_NLLB_MODEL_DIR)
        if not os.path.isdir(model_dir):
            raise TranslationError(
                f"Không tìm thấy model NLLB đã convert ở {model_dir!r}. Vào trang "
                f"trạng thái hệ thống và bấm 'Cài đặt' cho 'nllb_model', hoặc set "
                f"NLLB_MODEL_DIR trỏ tới thư mục đã convert sẵn."
            )

        inter_threads, intra_threads = _ct2_threads()
        try:
            _nllb_translator = ctranslate2.Translator(
                model_dir,
                device=os.environ.get("NLLB_DEVICE", "cpu"),
                compute_type=os.environ.get("NLLB_COMPUTE_TYPE", "default"),
                inter_threads=inter_threads,
                intra_threads=intra_threads,
            )
        except Exception as e:
            raise TranslationError(
                f"Không load được model NLLB ở {model_dir!r}: {e}"
            ) from e

        tokenizer_repo = os.environ.get(
            "NLLB_TOKENIZER", "facebook/nllb-200-distilled-600M"
        )
        try:
            _nllb_tokenizer = transformers.AutoTokenizer.from_pretrained(tokenizer_repo)
        except Exception as e:
            # Lần đầu load tokenizer cần mạng để tải từ HF (sau đó nằm
            # trong cache HF và chạy offline được). Nói rõ ra để không bị
            # nhầm với lỗi model.
            _nllb_translator = None
            raise TranslationError(
                f"Không load được tokenizer {tokenizer_repo!r}: {e} — lần đầu cần "
                f"mạng để tải về cache HuggingFace, sau đó chạy offline bình thường."
            ) from e
    return _nllb_translator, _nllb_tokenizer


def _nllb_translate_chunk(texts: List[str], source: str, target: str) -> List[str]:
    translator, tokenizer = _get_nllb_model()
    src_code = _to_nllb_code(source)
    tgt_code = _to_nllb_code(target)

    # NLLB cần src_lang được set trước khi encode để tokenizer chèn đúng
    # token mã ngôn ngữ nguồn ở đầu.
    tokenizer.src_lang = src_code
    source_tokens = [
        tokenizer.convert_ids_to_tokens(tokenizer.encode(text)) for text in texts
    ]
    try:
        # target_prefix mồi decoder bằng mã ngôn ngữ đích — đó là cách
        # steer NLLB sang một ngôn ngữ cụ thể (model không có tham số
        # "target" riêng, chính mã FLORES-200 là chỉ thị).
        outputs = translator.translate_batch(
            source_tokens,
            target_prefix=[[tgt_code]] * len(texts),
            beam_size=_beam_size(),
        )
    except Exception as e:
        raise TranslationError(f"Model NLLB lỗi khi dịch: {e}") from e

    # Token đầu ra đầu tiên là mã ngôn ngữ đích vừa mồi làm prefix — bỏ nó
    # trước khi decode về text.
    return [
        tokenizer.decode(tokenizer.convert_tokens_to_ids(out.hypotheses[0][1:])).strip()
        for out in outputs
    ]


# ---------------------------------------------------------------------------
# HachimiMT-60-zh-vi (Marian ~57M, chuyên truyện mạng, chỉ zh -> vi)
# ---------------------------------------------------------------------------

DEFAULT_HACHIMI_MODEL_ID = "ngocdang83/HachimiMT-60-zh-vi"

# Thư mục con trong repo HF chứa sẵn bản export CTranslate2 int8 — đây là
# lý do engine này không cần bước convert (và không cần torch) như NLLB:
# chỉ cần snapshot_download về là chạy được ngay.
DEFAULT_HACHIMI_CT2_SUBDIR = "ct2-int8_float32"

# Chỉ tải đúng những file cần cho inference bằng CTranslate2 + tokenizer
# Marian, bỏ qua checkpoint PyTorch gốc mà app này không bao giờ dùng tới.
# deps_check.py import đúng tuple này để lúc tải và lúc check khớp nhau
# tuyệt đối.
HACHIMI_ALLOW_PATTERNS = (
    "config.json",
    "source.spm",
    "target.spm",
    "vocab.json",
    "tokenizer_config.json",
    f"{DEFAULT_HACHIMI_CT2_SUBDIR}/*",
)

# Model này là Marian một chiều zh -> vi, không có token ngôn ngữ nào để
# steer sang cặp khác — gọi nhầm cặp sẽ ra output rác chứ không báo lỗi,
# nên chặn ngay ở đây cho rõ ràng.
_HACHIMI_SOURCES = {"zh", "zh-cn", "zh-hans", "zh-tw", "zh-hant"}
_HACHIMI_TARGETS = {"vi", "vi-vn"}

_hachimi_translator = None
_hachimi_tokenizer = None


def hachimi_model_id() -> str:
    return os.environ.get("HACHIMI_MODEL_ID", DEFAULT_HACHIMI_MODEL_ID).strip()


def resolve_hachimi_dirs(local_files_only: bool = True) -> Tuple[str, str]:
    """Trả về (snapshot_dir, ct2_dir) của model Hachimi.

    Ưu tiên HACHIMI_MODEL_DIR nếu được set (trỏ thẳng tới thư mục snapshot
    đã tải sẵn); nếu không thì hỏi huggingface_hub xem snapshot đã nằm
    trong cache chưa. Dùng chung bởi cả translate.py lúc load model và
    deps_check.py lúc check/tải, để "đã cài" và "cái thực sự được load"
    không bao giờ lệch nhau."""
    explicit = os.environ.get("HACHIMI_MODEL_DIR")
    subdir = os.environ.get("HACHIMI_CT2_SUBDIR", DEFAULT_HACHIMI_CT2_SUBDIR)

    if explicit:
        snapshot_dir = os.path.expanduser(explicit.strip())
        if not os.path.isdir(snapshot_dir):
            raise TranslationError(
                f"HACHIMI_MODEL_DIR={snapshot_dir!r} không phải thư mục tồn tại."
            )
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as e:
            raise TranslationError(
                "Thiếu 'huggingface_hub' để tìm model Hachimi trong cache. "
                "Cài bằng: pip install huggingface_hub"
            ) from e
        snapshot_dir = snapshot_download(
            hachimi_model_id(),
            allow_patterns=list(HACHIMI_ALLOW_PATTERNS),
            local_files_only=local_files_only,
        )

    # Bản CT2 nằm trong thư mục con; nhưng nếu ai đó trỏ HACHIMI_MODEL_DIR
    # thẳng vào chính thư mục CT2 thì cũng chấp nhận luôn cho đỡ bẫy.
    ct2_dir = os.path.join(snapshot_dir, subdir)
    if not os.path.isdir(ct2_dir):
        if os.path.isfile(os.path.join(snapshot_dir, "model.bin")):
            ct2_dir = snapshot_dir
        else:
            raise TranslationError(
                f"Không tìm thấy bản CTranslate2 của Hachimi ở {ct2_dir!r}. Vào "
                f"trang trạng thái hệ thống và bấm 'Cài đặt' cho 'hachimi_model', "
                f"hoặc set HACHIMI_MODEL_DIR trỏ tới snapshot đã tải sẵn."
            )
    return snapshot_dir, ct2_dir


def _get_hachimi_model():
    """Load model Hachimi (CTranslate2) + tokenizer Marian một lần rồi
    cache — cùng pattern singleton như _get_nllb_model()."""
    global _hachimi_translator, _hachimi_tokenizer
    if _hachimi_translator is None:
        _require_ct2()
        import ctranslate2
        import transformers

        try:
            snapshot_dir, ct2_dir = resolve_hachimi_dirs(local_files_only=True)
        except TranslationError:
            raise
        except Exception as e:
            raise TranslationError(
                f"Chưa tải model Hachimi ({hachimi_model_id()}): {e} — vào trang "
                f"trạng thái hệ thống và bấm 'Cài đặt' cho 'hachimi_model'."
            ) from e

        inter_threads, intra_threads = _ct2_threads()
        try:
            _hachimi_translator = ctranslate2.Translator(
                ct2_dir,
                device=os.environ.get("HACHIMI_DEVICE", "cpu"),
                compute_type=os.environ.get("HACHIMI_COMPUTE_TYPE", "int8_float32"),
                inter_threads=inter_threads,
                intra_threads=intra_threads,
            )
        except Exception as e:
            raise TranslationError(
                f"Không load được model Hachimi ở {ct2_dir!r}: {e}"
            ) from e

        # Tokenizer Marian đọc source.spm/target.spm/vocab.json ngay trong
        # snapshot — không tải thêm gì từ mạng, nên engine này offline hoàn
        # toàn ngay từ lần chạy đầu (khác NLLB, vốn còn phải kéo tokenizer
        # từ repo gốc về cache).
        try:
            _hachimi_tokenizer = transformers.AutoTokenizer.from_pretrained(snapshot_dir)
        except Exception as e:
            _hachimi_translator = None
            raise TranslationError(
                f"Không load được tokenizer Hachimi ở {snapshot_dir!r}: {e} — "
                f"thiếu 'sentencepiece'? Cài bằng: pip install sentencepiece"
            ) from e
    return _hachimi_translator, _hachimi_tokenizer


def _hachimi_translate_chunk(texts: List[str], source: str, target: str) -> List[str]:
    if (
        source.strip().lower() not in _HACHIMI_SOURCES
        or target.strip().lower() not in _HACHIMI_TARGETS
    ):
        raise TranslationError(
            f"Engine 'hachimi' chỉ dịch được Trung -> Việt, không dùng được cho "
            f"{source!r} -> {target!r}. Chọn engine 'nllb' cho cặp ngôn ngữ này."
        )

    translator, tokenizer = _get_hachimi_model()
    source_tokens = [
        tokenizer.convert_ids_to_tokens(
            tokenizer.encode(text, truncation=True, max_length=512)
        )
        for text in texts
    ]
    try:
        # no_repeat_ngram_size + repetition_penalty theo đúng khuyến nghị
        # trên model card: model nhỏ (57M) dễ lặp cụm khi gặp câu ngắn
        # hoặc nhiều tên riêng, hai tham số này chặn phần lớn trường hợp đó.
        outputs = translator.translate_batch(
            source_tokens,
            beam_size=_beam_size(),
            max_decoding_length=512,
            no_repeat_ngram_size=2,
            repetition_penalty=1.2,
        )
    except Exception as e:
        raise TranslationError(f"Model Hachimi lỗi khi dịch: {e}") from e

    # Marian không có target_prefix như NLLB nên không phải bỏ token đầu;
    # chỉ cần skip_special_tokens để bỏ </s>, <pad>.
    return [
        tokenizer.decode(
            tokenizer.convert_tokens_to_ids(out.hypotheses[0]),
            skip_special_tokens=True,
        ).strip()
        for out in outputs
    ]


# ---------------------------------------------------------------------------
# Dispatch + phần dùng chung
# ---------------------------------------------------------------------------

_CHUNK_TRANSLATORS = {
    ENGINE_NLLB: _nllb_translate_chunk,
    ENGINE_HACHIMI: _hachimi_translate_chunk,
}

# Một lock cho mỗi engine, dùng cho cả lúc load model lần đầu lẫn mọi lần
# translate_batch: model chạy trên CPU nên chạy song song chỉ làm các lời
# gọi giành core của nhau, và tokenizer (nhất là tokenizer.src_lang của
# NLLB) là state mutable dùng chung nên đọc/ghi đồng thời sẽ sai.
# CTranslate2 đã tự dùng nhiều thread bên trong cho một batch.
_ENGINE_LOCKS: Dict[str, threading.Lock] = {
    engine: threading.Lock() for engine in SUPPORTED_ENGINES
}


def _is_translatable(text: str) -> bool:
    """True nếu `text` có ít nhất một chữ/số thật (bất kể ngôn ngữ nào —
    str.isalnum() bao cả tiếng Trung/tiếng Việt, không chỉ ASCII). Chặn
    input chỉ có dấu câu (ví dụ "....", "。。。") — đưa vào model chỉ tốn
    thời gian và dễ ra output rác. Đường FunASR trong transcribe.py đã
    lọc ở đầu nguồn (SenseVoice đôi khi nhả text chỉ có dấu câu cho một
    segment VAD hóa ra là tiếng ồn chứ không phải tiếng nói); đây là lớp
    thứ hai cho trường hợp text tương tự đến từ chỗ khác (STT engine
    khác, segment sửa tay...)."""
    return any(ch.isalnum() for ch in text)


def _translate_batch_sync(
    texts: List[str],
    source: str,
    target: str,
    engine: Optional[str] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> List[str]:
    """Dịch nhiều đoạn trong một lượt translate_batch. Đoạn rỗng hoặc chỉ
    có dấu câu được trả lại nguyên văn, không đưa vào model, nhưng vẫn
    giữ đúng vị trí trong list kết quả.

    `on_progress`, nếu có, được gọi với một float 0-100 sau mỗi batch dịch
    xong — tính theo số đoạn ĐÃ ĐƯA VÀO MODEL (`pending`), không phải theo
    tổng `texts`, vì các đoạn rỗng/chỉ dấu câu bị bỏ qua ở bước lọc bên
    dưới, không đi qua model nên không tính vào tiến độ. Được gọi TRỰC
    TIẾP, đồng bộ, ngay trong hàm này — hàm này bản thân nó thường chạy
    trong một worker thread qua asyncio.to_thread() (xem
    translate_segments() bên dưới), nên callback cũng bị gọi từ thread đó,
    không phải event loop chính. Nếu callback đụng tới state của asyncio
    (ví dụ cập nhật JobState rồi notify listener), người gọi
    (translate_segments()'s caller) phải tự bọc bằng
    loop.call_soon_threadsafe() — xem job_manager.py.
    """
    resolved_engine = _resolve_engine(engine)

    results: List[Optional[str]] = [None] * len(texts)
    pending: List[Tuple[int, str]] = []
    for i, text in enumerate(texts):
        if not text.strip():
            results[i] = ""
        elif not _is_translatable(text):
            results[i] = text
        else:
            pending.append((i, text))

    if not pending:
        if on_progress is not None:
            on_progress(100.0)
        return [r or "" for r in results]

    translate_chunk = _CHUNK_TRANSLATORS[resolved_engine]
    batch_size = _batch_size()
    total_pending = len(pending)
    done = 0

    with _ENGINE_LOCKS[resolved_engine]:
        for chunk_start in range(0, len(pending), batch_size):
            chunk = pending[chunk_start : chunk_start + batch_size]
            translated = translate_chunk([text for _, text in chunk], source, target)
            for (idx, original), text in zip(chunk, translated):
                if not text:
                    raise TranslationError(
                        f"Engine {resolved_engine!r} trả về kết quả rỗng cho đoạn "
                        f"{original!r}"
                    )
                results[idx] = text
            done += len(chunk)
            if on_progress is not None:
                on_progress(min(100.0, done / total_pending * 100.0))

    return [r or "" for r in results]


def _translate_sync(
    text: str, source: str, target: str, engine: Optional[str] = None
) -> str:
    return _translate_batch_sync([text], source, target, engine)[0]


async def translate_text(
    text: str,
    source_lang: str = "zh-CN",
    target_lang: str = "vi",
    engine: Optional[str] = None,
) -> str:
    """Dịch lẻ một chuỗi ngắn (ví dụ title/caption của video), dùng đúng
    engine như translate_segments(), chỉ là không bọc Segment."""
    return await asyncio.to_thread(
        _translate_sync, text, source_lang, target_lang, engine
    )


async def translate_segments(
    segments: List[Segment],
    source_lang: str = "zh-CN",
    target_lang: str = "vi",
    engine: Optional[str] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> List[Segment]:
    """Dịch cả list segment. Chạy theo batch trong một worker thread duy
    nhất thay vì gather nhiều task song song: model local dùng chung một
    Translator trên CPU nên chạy song song không nhanh hơn, còn dịch theo
    batch thì nhanh hơn thật.

    `engine` là lựa chọn theo từng job (từ UI); để None thì dùng mặc định
    của server (TRANSLATE_ENGINE).

    `on_progress`, nếu có, nhận một float 0-100 sau mỗi batch dịch xong —
    xem docstring của _translate_batch_sync về việc callback này bị gọi từ
    worker thread, không phải event loop chính."""
    if not segments:
        return []

    translations = await asyncio.to_thread(
        _translate_batch_sync,
        [seg.text for seg in segments],
        source_lang,
        target_lang,
        engine,
        on_progress,
    )
    return [
        Segment(start=seg.start, end=seg.end, text=translated)
        for seg, translated in zip(segments, translations)
    ]