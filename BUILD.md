```bash
cd frontend && npm run build
Copy dist vào project BE
cd backend
# Serve React build — để cuối cùng vì catch-all
app.mount("/", StaticFiles(directory="dist", html=True), name="static")
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
pyinstaller --onefile --add-data "dist:dist" main.py
```
