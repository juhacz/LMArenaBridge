# file_bed_server/main.py
import base64
import os
import uuid
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import logging
from apscheduler.schedulers.background import BackgroundScheduler

# --- Podstawowa konfiguracja ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Konfiguracja ścieżek ---
# Umieszczamy katalog uploadów obok tego pliku main.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
API_KEY = "your_secret_api_key"  # Prosty klucz uwierzytelniający
CLEANUP_INTERVAL_MINUTES = 1  # Częstotliwość zadania czyszczącego (minuty)
FILE_MAX_AGE_MINUTES = 10  # Maksymalny wiek pliku do przechowania (minuty)

# --- Funkcja czyszcząca stare pliki ---
def cleanup_old_files():
    """Przechodzi przez katalog uploadów i usuwa pliki starsze niż zadany czas."""
    now = time.time()
    cutoff = now - (FILE_MAX_AGE_MINUTES * 60)
    
    logger.info(f"Uruchamiam zadanie czyszczenia — usuwam pliki starsze niż {datetime.fromtimestamp(cutoff).strftime('%Y-%m-%d %H:%M:%S')}...")
    
    deleted_count = 0
    try:
        for filename in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(file_path):
                try:
                    file_mtime = os.path.getmtime(file_path)
                    if file_mtime < cutoff:
                        os.remove(file_path)
                        logger.info(f"Usunięto przeterminowany plik: {filename}")
                        deleted_count += 1
                except OSError as e:
                    logger.error(f"Błąd podczas usuwania pliku '{file_path}': {e}")
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas czyszczenia starych plików: {e}", exc_info=True)

    if deleted_count > 0:
        logger.info(f"Zadanie czyszczenia zakończone — usunięto {deleted_count} plik(ów).")
    else:
        logger.info("Zadanie czyszczenia zakończone — brak plików do usunięcia.")


# --- FastAPI lifecycle ---
scheduler = BackgroundScheduler(timezone="UTC")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Uruchamiane przy starcie serwera — startuje zadanie w tle; przy zamknięciu zatrzymuje je."""
    # Uruchamiamy scheduler i dodajemy zadanie czyszczenia
    scheduler.add_job(cleanup_old_files, 'interval', minutes=CLEANUP_INTERVAL_MINUTES)
    scheduler.start()
    logger.info(f"Zadanie czyszczenia plików uruchomione — będzie uruchamiane co {CLEANUP_INTERVAL_MINUTES} minut.")
    yield
    # Wyłączamy scheduler przy zamknięciu
    scheduler.shutdown()
    logger.info("Zadanie czyszczenia plików zostało zatrzymane.")


app = FastAPI(lifespan=lifespan)

# --- Upewnij się, że katalog upload istnieje ---
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)
    logger.info(f"Katalog uploadów '{UPLOAD_DIR}' został utworzony.")

# --- Montujemy statyczne pliki, aby udostępniać uploady ---
app.mount(f"/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- Model Pydantic ---
class UploadRequest(BaseModel):
    file_name: str
    file_data: str  # odbiera kompletny Base64 data URI
    api_key: str | None = None

# --- Endpointy API ---
@app.post("/upload")
async def upload_file(request: UploadRequest, http_request: Request):
    """
    Przyjmuje plik zakodowany w base64, zapisuje go i zwraca dostępny URL.
    """
    # Prosta weryfikacja klucza API
    if API_KEY and request.api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Nieprawidłowy klucz API")

    try:
        # 1. Rozdzielenie nagłówka i danych base64
        header, encoded_data = request.file_data.split(',', 1)
        
        # 2. Dekodowanie base64
        file_data = base64.b64decode(encoded_data)
        
        # 3. Generowanie unikalnej nazwy pliku, aby uniknąć konfliktów
        file_extension = os.path.splitext(request.file_name)[1]
        if not file_extension:
            # Próba wywnioskowania rozszerzenia z typu MIME w nagłówku
            import mimetypes
            mime_type = header.split(';')[0].split(':')[1]
            guessed_extension = mimetypes.guess_extension(mime_type)
            file_extension = guessed_extension if guessed_extension else '.bin'

        unique_filename = f"{uuid.uuid4()}{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, unique_filename)

        # 4. Zapis pliku
        with open(file_path, "wb") as f:
            f.write(file_data)
        
        # 5. Zwracamy sukces i nazwę pliku
        logger.info(f"Plik '{request.file_name}' został zapisany jako '{unique_filename}'.")
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "filename": unique_filename}
        )

    except (ValueError, IndexError) as e:
        logger.error(f"Błąd parsowania base64: {e}")
        raise HTTPException(status_code=400, detail=f"Nieprawidłowy format base64 data URI: {e}")
    except Exception as e:
        logger.error(f"Nieznany błąd podczas obsługi uploadu: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Błąd wewnętrzny serwera: {e}")

@app.get("/")
def read_root():
    return {"message": "Serwer przechowywania plików LMArena Bridge działa."}

# --- Punkt wejścia programu ---
if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Serwer file bed uruchamia się...")
    logger.info("   - Adres nasłuchu: http://127.0.0.1:5180")
    logger.info(f"   - Endpoint upload: http://127.0.0.1:5180/upload")
    logger.info(f"   - Ścieżka do plików: /uploads")
    uvicorn.run(app, host="0.0.0.0", port=5180)