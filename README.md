# Douyin → Vietsub Studio

Công cụ local: tải video Douyin theo URL, chuyển giọng nói thành văn bản,
dịch Trung → Việt, tạo giọng đọc tiếng Việt (TTS), gắn vietsub, và ghép
kèm nhạc nền tuỳ chọn.

```
frontend/   Vite + React + TypeScript + TailwindCSS (UI)
backend/    FastAPI (Python) — điều phối pipeline: f2 → faster-whisper →
            deep-translator → TTS tiếng Việt → ffmpeg
```

## Pipeline mỗi video

1. **download** — `f2` tải video Douyin bằng cookie do bạn cung cấp
2. **speech_to_text** — `faster-whisper` phiên âm audio tiếng Trung có timestamp
3. **translate** — `deep-translator` dịch từng đoạn Trung → Việt
4. **text_to_speech** — sinh giọng đọc tiếng Việt cho từng đoạn, đặt đúng
   thời điểm gốc để giữ nhịp
5. **export** — ghép giọng đọc (+ trộn nhạc nền nếu có) vào video, gắn
   cứng file `.srt` vietsub, xuất ra thư mục bạn chọn

Trạng thái từng video (queued/running/success/failed + bước thất bại)
được đẩy real-time về giao diện qua WebSocket.

---

## 1. Yêu cầu hệ thống

Cài trước khi bắt đầu, trên cả Windows lẫn macOS:

| Thành phần       | Phiên bản gợi ý       | Ghi chú                                                           |
| ---------------- | --------------------- | ----------------------------------------------------------------- |
| Python           | 3.11 hoặc 3.12        | Tránh bản quá mới (3.13+) — một số gói TTS/PyAV chưa có wheel sẵn |
| Node.js          | 18+                   | Kèm npm                                                           |
| ffmpeg + ffprobe | bất kỳ bản gần đây    | Phải nằm trong PATH                                               |
| f2 CLI           | theo hướng dẫn của f2 | https://github.com/Johnserf-Seed/f2                               |

Kiểm tra nhanh sau khi cài (chạy trong terminal/PowerShell):

```bash
python3 --version   # macOS/Linux
python --version    # Windows
node --version
ffmpeg -version
```

### Cài đặt trên macOS

```bash
brew install python@3.12 node ffmpeg
brew install ffmpeg
```

macOS **không có sẵn lệnh `python`**, chỉ có `python3` — mọi lệnh Python bên dưới dùng `python3` là vì vậy. Sau khi kích hoạt virtualenv (`source .venv/bin/activate`), bên trong venv thì `python`/`pip` sẽ hoạt động bình thường.

### Cài đặt trên Windows

- Cài Python từ https://www.python.org/downloads/ (nhớ tick **"Add python.exe to PATH"** lúc cài).
- Cài Node.js từ https://nodejs.org.
- Cài ffmpeg: cách dễ nhất là `winget install ffmpeg` hoặc tải bản build tại https://www.gyan.dev/ffmpeg/builds/ rồi tự thêm thư mục `bin` vào biến môi trường PATH.
- Dùng **PowerShell**, không dùng cmd.exe, cho các lệnh bên dưới.

---

## 2. Chạy backend

### macOS / Linux

```bash
cd backend
rm -rf .venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade "av>=13" --only-binary=:all:
pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

```

### Windows (PowerShell)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Nếu PowerShell chặn script với lỗi "running scripts is disabled", chạy một lần:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Backend chạy tại `http://localhost:8000`.

### Nếu cài `f2` / `faster-whisper` báo lỗi build gói `av` (PyAV)

Lỗi thường gặp: pip cố build `av` từ source và báo `no member named
'nb_side_data'` hoặc `no member named 'side_data' in struct AVStream` — do
version `av` bị pin quá cũ so với FFmpeg đang cài trên máy, nên không có
wheel dựng sẵn phù hợp. Cách xử lý, chạy **trước**
`pip install -r requirements.txt`:

```bash
pip install --upgrade "av>=13" --only-binary=:all:
```

rồi chạy lại `pip install -r requirements.txt`. Nếu vẫn lỗi, khả năng cao là
Python đang dùng quá mới (3.13+) — cài lại venv bằng Python 3.11/3.12 sẽ có
sẵn wheel cho hầu hết các gói.

cd backend
rm -rf .venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

---

## 3. Chạy frontend

Mở một terminal/PowerShell **khác** (giữ backend đang chạy ở cửa sổ kia):

```bash
cd frontend
npm install
npm run dev
```

Lệnh này giống nhau trên cả Windows và macOS. Mở `http://localhost:5173` —
dev server tự proxy `/api` sang `http://localhost:8000`.

---

## 4. Ghi chú quan trọng trước khi dùng thật

- **Thư mục lưu kết quả**: trình duyệt không thể mở hộp thoại chọn thư mục
  hệ thống thật vì lý do bảo mật, nên ô này giờ là **tuỳ chọn**. Để trống,
  video sẽ được giữ ở `backend/storage/outputs/<job_id>/` và mỗi job xong
  sẽ có nút **"Tải xuống"** ngay trên giao diện — bấm vào để trình duyệt mở
  hộp thoại Save As quen thuộc, bạn chọn nơi lưu như tải file bình thường.
  Chỉ điền đường dẫn tuyệt đối vào ô này nếu bạn chạy backend trên cùng máy
  và muốn video ghi thẳng vào một thư mục cụ thể mà không cần bấm tải.
- **File cookie** và **nhạc nền mp3** được tải lên (upload) thật sự tới
  backend và lưu ở `backend/storage/uploads/`.
- **`f2`**: `backend/app/pipeline/downloader.py` gọi `f2` qua CLI với một
  file config YAML tạo tự động. Format config trong file đó là một tập
  con hợp lý dựa trên tài liệu f2 tại thời điểm viết — kiểm tra lại với
  phiên bản f2 bạn cài (`f2 dy --help`) và chỉnh nếu schema đổi.
- **VieNeu-TTS**: cài bằng `pip install vieneu` (đã có trong
  `requirements.txt`) — bản tối giản không cần GPU/PyTorch, chạy engine
  v3 Turbo qua ONNX Runtime trên CPU. Có GPU CUDA/Apple Silicon thì SDK tự
  chuyển sang engine PyTorch, không cần sửa code.
  `backend/app/pipeline/tts.py` dùng giọng mặc định của VieNeu; có thể
  chỉnh qua biến môi trường:
  - `VIENEU_VOICE=<tên giọng>` — dùng một giọng có sẵn (xem
    `tts.list_preset_voices()` trong SDK để lấy danh sách tên)
  - `VIENEU_REF_AUDIO=<đường dẫn wav>` (+ `VIENEU_REF_TEXT` tuỳ chọn) —
    nhân bản giọng tức thì từ một đoạn audio mẫu 3–5 giây, thay vì dùng
    giọng có sẵn
- **faster-whisper**: mặc định dùng model `medium` (đổi bằng biến môi
  trường `WHISPER_MODEL`), dịch tốt hơn cho tiếng Trung so với `small`.

---

## 5. Cấu trúc backend

```
backend/app/
  main.py            FastAPI app: POST /api/jobs, WebSocket /api/ws
  job_manager.py      điều phối pipeline theo từng job, phát trạng thái
  models.py            schema Pydantic
  storage.py            lưu file upload (cookie/nhạc) + thư mục làm việc mỗi job
  pipeline/
    downloader.py       gọi f2
    transcribe.py        faster-whisper
    translate.py          deep-translator
    tts.py                  TTS tiếng Việt (VieNeu / fallback)
    srt_utils.py            sinh file .srt
    mux.py                    ffmpeg: tách audio, ghép track TTS theo timestamp,
                              trộn nhạc nền, gắn cứng sub, xuất video cuối
```
