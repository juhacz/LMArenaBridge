# api_server.py
# Backend nowej generacji LMArena Bridge

import asyncio
import json
import logging
import os
import sys
import subprocess
import time
import uuid
import re
import threading
import random
import mimetypes
from datetime import datetime
from contextlib import asynccontextmanager

import uvicorn
import requests
from packaging.version import parse as parse_version
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response

# --- Importy modułów wewnętrznych ---
from modules.file_uploader import upload_to_file_bed


# --- Podstawowa konfiguracja ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Globalny stan i konfiguracja ---
CONFIG = {}  # Przechowuje konfigurację załadowaną z config.jsonc
# browser_ws przechowuje połączenie WebSocket z pojedynczym skryptem Tampermonkey.
# Uwaga: obecna architektura zakłada, że tylko jedna karta przeglądarki jest aktywna.
# Aby obsłużyć wiele kart, trzeba by rozszerzyć to do zarządzania wieloma połączeniami.
browser_ws: WebSocket | None = None
# response_channels przechowuje kolejki odpowiedzi dla każdego żądania API.
# Klucz to request_id, wartość to asyncio.Queue.
response_channels: dict[str, asyncio.Queue] = {}
last_activity_time = None  # Rejestruje czas ostatniej aktywności
idle_monitor_thread = None  # Wątek monitorujący bezczynność
main_event_loop = None  # Główna pętla zdarzeń
# Nowe: śledzi, czy trwa odświeżanie związane z weryfikacją Cloudflare
IS_REFRESHING_FOR_VERIFICATION = False


# --- Mapowanie modeli ---
# MODEL_NAME_TO_ID_MAP przechowuje teraz bogatsze obiekty: { "model_name": {"id": "...", "type": "..."} }
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {}  # Nowe: przechowuje mapowania modeli do session/message ID
DEFAULT_MODEL_ID = None  # Domyślne ID modelu: None

def load_model_endpoint_map():
    """Wczytuje mapowanie modeli -> endpointów z model_endpoint_map.json."""
    global MODEL_ENDPOINT_MAP
    try:
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            content = f.read()
            # Pozwalamy na pusty plik
            if not content.strip():
                MODEL_ENDPOINT_MAP = {}
            else:
                MODEL_ENDPOINT_MAP = json.loads(content)
        logger.info(f"Pomyślnie wczytano {len(MODEL_ENDPOINT_MAP)} mapowań endpointów z 'model_endpoint_map.json'.")
    except FileNotFoundError:
        logger.warning("Plik 'model_endpoint_map.json' nie został znaleziony. Używana będzie pusta mapa.")
        MODEL_ENDPOINT_MAP = {}
    except json.JSONDecodeError as e:
        logger.error(f"Błąd podczas ładowania/parowania 'model_endpoint_map.json': {e}. Używana będzie pusta mapa.")
        MODEL_ENDPOINT_MAP = {}

def _parse_jsonc(jsonc_string: str) -> dict:
    """
    Solidne parsowanie JSONC, usuwanie komentarzy.
    """
    lines = jsonc_string.splitlines()
    no_comments_lines = []
    in_block_comment = False
    for line in lines:
        stripped_line = line.strip()
        if in_block_comment:
            if '*/' in stripped_line:
                in_block_comment = False
                line = stripped_line.split('*/', 1)[1]
            else:
                continue
        
        if '/*' in line and not in_block_comment:
            before_comment, _, after_comment = line.partition('/*')
            if '*/' in after_comment:
                _, _, after_block = after_comment.partition('*/')
                line = before_comment + after_block
            else:
                line = before_comment
                in_block_comment = True

        if line.strip().startswith('//'):
            continue
        
        no_comments_lines.append(line)

    return json.loads("\n".join(no_comments_lines))

def load_config():
    """Wczytuje konfigurację z config.jsonc i obsługuje komentarze JSONC."""
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
        CONFIG = _parse_jsonc(content)
        logger.info("Pomyślnie wczytano konfigurację z 'config.jsonc'.")
        # Logowanie kluczowych ustawień
        logger.info(f"  - Tryb Tavern: {'✅ Włączony' if CONFIG.get('tavern_mode_enabled') else '❌ Wyłączony'}")
        logger.info(f"  - Tryb Bypass: {'✅ Włączony' if CONFIG.get('bypass_enabled') else '❌ Wyłączony'}")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Błąd podczas ładowania/parowania 'config.jsonc': {e}. Używana będzie domyślna konfiguracja.")
        CONFIG = {}

def load_model_map():
    """Wczytuje mapowanie modeli z models.json, obsługuje format 'id:type'."""
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            raw_map = json.load(f)
            
        processed_map = {}
        for name, value in raw_map.items():
            if isinstance(value, str) and ':' in value:
                parts = value.split(':', 1)
                model_id = parts[0] if parts[0].lower() != 'null' else None
                model_type = parts[1]
                processed_map[name] = {"id": model_id, "type": model_type}
            else:
                # Obsługa formatu domyślnego / starszego
                processed_map[name] = {"id": value, "type": "text"}

        MODEL_NAME_TO_ID_MAP = processed_map
        logger.info(f"Pomyślnie wczytano i sparsowano {len(MODEL_NAME_TO_ID_MAP)} modeli z 'models.json'.")

    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Błąd podczas ładowania 'models.json': {e}. Lista modeli będzie pusta.")
        MODEL_NAME_TO_ID_MAP = {}

# --- Obsługa ogłoszeń ---
def check_and_display_announcement():
    """Sprawdza i wyświetla jednorazowe ogłoszenie."""
    announcement_file = "announcement-lmarena.json"
    if os.path.exists(announcement_file):
        try:
            logger.info("="*60)
            logger.info("📢 Wykryto aktualizację z ogłoszeniem, treść:")
            with open(announcement_file, 'r', encoding='utf-8') as f:
                announcement = json.load(f)
                title = announcement.get("title", "Ogłoszenie")
                content = announcement.get("content", [])
                
                logger.info(f"   --- {title} ---")
                for line in content:
                    logger.info(f"   {line}")
                logger.info("="*60)

        except json.JSONDecodeError:
            logger.error(f"Nie można sparsować pliku ogłoszenia '{announcement_file}'. Zawartość może nie być poprawnym JSON.")
        except Exception as e:
            logger.error(f"Błąd podczas odczytu pliku ogłoszenia: {e}")
        finally:
            try:
                os.remove(announcement_file)
                logger.info(f"Plik ogłoszenia '{announcement_file}' został usunięty.")
            except OSError as e:
                logger.error(f"Nie udało się usunąć pliku ogłoszenia '{announcement_file}': {e}")

# --- Sprawdzanie aktualizacji ---
GITHUB_REPO = "Lianues/LMArenaBridge"

def download_and_extract_update(version):
    """Pobiera i rozpakowuje najnowszą wersję do folderu tymczasowego."""
    update_dir = "update_temp"
    if not os.path.exists(update_dir):
        os.makedirs(update_dir)

    try:
        zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
        logger.info(f"Pobieram nową wersję z {zip_url}...")
        response = requests.get(zip_url, timeout=60)
        response.raise_for_status()

        # Potrzebne importy zipfile i io
        import zipfile
        import io
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            z.extractall(update_dir)
        
        logger.info(f"Nowa wersja została pobrana i rozpakowana do '{update_dir}'.")
        return True
    except requests.RequestException as e:
        logger.error(f"Błąd pobierania aktualizacji: {e}")
    except zipfile.BadZipFile:
        logger.error("Pobrany plik nie jest poprawnym archiwum zip.")
    except Exception as e:
        logger.error(f"Nieznany błąd podczas rozpakowywania aktualizacji: {e}")
    
    return False

def check_for_updates():
    """Sprawdza GitHub pod kątem nowej wersji."""
    if not CONFIG.get("enable_auto_update", True):
        logger.info("Automatyczne aktualizacje są wyłączone — pomijam sprawdzenie.")
        return

    current_version = CONFIG.get("version", "0.0.0")
    logger.info(f"Aktualna wersja: {current_version}. Sprawdzam aktualizacje na GitHub...")

    try:
        config_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/config.jsonc"
        response = requests.get(config_url, timeout=10)
        response.raise_for_status()

        jsonc_content = response.text
        remote_config = _parse_jsonc(jsonc_content)
        
        remote_version_str = remote_config.get("version")
        if not remote_version_str:
            logger.warning("W zdalnym pliku konfiguracyjnym nie znaleziono numeru wersji — pomijam sprawdzenie.")
            return

        if parse_version(remote_version_str) > parse_version(current_version):
            logger.info("="*60)
            logger.info(f"🎉 Znaleziono nową wersję! 🎉")
            logger.info(f"  - Obecna: {current_version}")
            logger.info(f"  - Najnowsza: {remote_version_str}")
            if download_and_extract_update(remote_version_str):
                logger.info("Przygotowuję aplikację do aktualizacji. Serwer wyłączy się i uruchomi skrypt aktualizujący za 5 sekund.")
                time.sleep(5)
                update_script_path = os.path.join("modules", "update_script.py")
                # Uruchamiamy niezależny proces Popen
                subprocess.Popen([sys.executable, update_script_path])
                # Eleganckie zakończenie bieżącego procesu
                os._exit(0)
            else:
                logger.error(f"Automatyczna aktualizacja nie powiodła się. Pobierz ręcznie: https://github.com/{GITHUB_REPO}/releases/latest")
            logger.info("="*60)
        else:
            logger.info("Program jest aktualny.")

    except requests.RequestException as e:
        logger.error(f"Błąd podczas sprawdzania aktualizacji: {e}")
    except json.JSONDecodeError:
        logger.error("Błąd parsowania zdalnego pliku konfiguracyjnego.")
    except Exception as e:
        logger.error(f"Nieznany błąd podczas sprawdzania aktualizacji: {e}")

# --- Aktualizacja listy modeli ---
def extract_models_from_html(html_content):
    """
    Wyodrębnia pełne obiekty JSON modeli z zawartości HTML, używając dopasowania nawiasów
    aby zapewnić kompletność obiektu.
    """
    models = []
    model_names = set()
    
    # Szukamy potencjalnych pozycji początku obiektu JSON modelu
    for start_match in re.finditer(r'\{\\"id\\":\\"[a-f0-9-]+\\"', html_content):
        start_index = start_match.start()
        
        # Dopasowywanie nawiasów od pozycji startowej
        open_braces = 0
        end_index = -1
        
        # Optymalizacja: ustaw limit wyszukiwania, by uniknąć nieskończonych pętli
        search_limit = start_index + 10000  # zakładamy, że definicja modelu nie przekroczy 10000 znaków
        
        for i in range(start_index, min(len(html_content), search_limit)):
            if html_content[i] == '{':
                open_braces += 1
            elif html_content[i] == '}':
                open_braces -= 1
                if open_braces == 0:
                    end_index = i + 1
                    break
        
        if end_index != -1:
            # Wyciągamy kompletny, escape'owany JSON
            json_string_escaped = html_content[start_index:end_index]
            
            # Usuwamy escape'y
            json_string = json_string_escaped.replace('\\"', '"').replace('\\\\', '\\')
            
            try:
                model_data = json.loads(json_string)
                model_name = model_data.get('publicName')
                
                # Unikalność według publicName
                if model_name and model_name not in model_names:
                    models.append(model_data)
                    model_names.add(model_name)
            except json.JSONDecodeError as e:
                logger.warning(f"Błąd parsowania wyodrębnionego obiektu JSON: {e} - fragment: {json_string[:150]}...")
                continue

    if models:
        logger.info(f"Pomyślnie wyodrębniono i sparsowano {len(models)} modeli.")
        return models
    else:
        logger.error("Błąd: nie znaleziono żadnych kompletnych obiektów JSON reprezentujących modele w odpowiedzi HTML.")
        return None

def save_available_models(new_models_list, models_path="available_models.json"):
    """
    Zapisuje wyodrębnioną listę obiektów modeli do pliku JSON.
    """
    logger.info(f"Wykryto {len(new_models_list)} modeli, aktualizuję '{models_path}'...")
    
    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            # Zapisujemy bezpośrednio listę obiektów modeli
            json.dump(new_models_list, f, indent=4, ensure_ascii=False)
        logger.info(f"✅ Plik '{models_path}' został zaktualizowany i zawiera {len(new_models_list)} modeli.")
    except IOError as e:
        logger.error(f"Błąd podczas zapisu pliku '{models_path}': {e}")

# --- Logika automatycznego restartu ---
def restart_server():
    """Powiadamia klienta o odświeżeniu, a następnie restartuje serwer."""
    logger.warning("="*60)
    logger.warning("Wykryto długi czas bezczynności serwera — przygotowuję restart...")
    logger.warning("="*60)
    
    # 1. (asynchronicznie) powiadom przeglądarkę o odświeżeniu
    async def notify_browser_refresh():
        if browser_ws:
            try:
                # Wysyłamy polecenie 'reconnect' aby poinformować frontend, że to planowany restart
                await browser_ws.send_text(json.dumps({"command": "reconnect"}, ensure_ascii=False))
                logger.info("Wysłano do przeglądarki polecenie 'reconnect'.")
            except Exception as e:
                logger.error(f"Błąd wysyłania polecenia 'reconnect': {e}")
    
    # Uruchamiamy asynchroniczną funkcję w głównej pętli zdarzeń
    if browser_ws and browser_ws.client_state.name == 'CONNECTED' and main_event_loop:
        asyncio.run_coroutine_threadsafe(notify_browser_refresh(), main_event_loop)
    
    # 2. Krótkie opóźnienie, aby upewnić się, że wiadomość dotarła
    time.sleep(3)
    
    # 3. Wykonanie restartu
    logger.info("Restartuję serwer...")
    os.execv(sys.executable, ['python'] + sys.argv)

def idle_monitor():
    """Uruchamiane w tle — monitoruje bezczynność serwera."""
    global last_activity_time
    
    # Czekamy, aż last_activity_time zostanie ustawione po starcie
    while last_activity_time is None:
        time.sleep(1)
        
    logger.info("Wątek monitorujący bezczynność uruchomiony.")
    
    while True:
        if CONFIG.get("enable_idle_restart", False):
            timeout = CONFIG.get("idle_restart_timeout_seconds", 300)
            
            # Jeśli timeout == -1, wyłączamy restart
            if timeout == -1:
                time.sleep(10)  # pauza, aby uniknąć gorącej pętli
                continue

            idle_time = (datetime.now() - last_activity_time).total_seconds()
            
            if idle_time > timeout:
                logger.info(f"Serwer był bezczynny przez {idle_time:.0f}s, przekroczono próg {timeout}s.")
                restart_server()
                break  # kończymy, proces zostanie zastąpiony
                
        # Sprawdzamy co 10 sekund
        time.sleep(10)

# --- FastAPI lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Funkcja wykonywana przy starcie serwera."""
    global idle_monitor_thread, last_activity_time, main_event_loop
    main_event_loop = asyncio.get_running_loop()  # Pobieramy główną pętlę zdarzeń
    load_config()  # Najpierw wczytujemy konfigurację
    
    # --- Wypisanie aktualnego trybu działania ---
    mode = CONFIG.get("id_updater_last_mode", "direct_chat")
    target = CONFIG.get("id_updater_battle_target", "A")
    logger.info("="*60)
    logger.info(f"  Aktualny tryb operacyjny: {mode.upper()}")
    if mode == 'battle':
        logger.info(f"  - Tryb Battle, cel: Asystent {target}")
    logger.info("  (Tryb można zmienić uruchamiając id_updater.py)")
    logger.info("="*60)

    check_for_updates()  # Sprawdź aktualizacje
    load_model_map()  # Wczytaj mapę modeli
    load_model_endpoint_map()  # Wczytaj mapowanie endpointów modeli
    logger.info("Serwer uruchomiony. Oczekiwanie na połączenie skryptu Tampermonkey...")

    # Wyświetl ogłoszenie na koniec, żeby było bardziej widoczne
    check_and_display_announcement()

    # Ustawiamy czas ostatniej aktywności po wczytaniu modeli
    last_activity_time = datetime.now()
    
    # Uruchamiamy wątek monitorujący bezczynność, jeśli skonfigurowano
    if CONFIG.get("enable_idle_restart", False):
        idle_monitor_thread = threading.Thread(target=idle_monitor, daemon=True)
        idle_monitor_thread.start()
        

    yield
    logger.info("Serwer się zamyka.")

app = FastAPI(lifespan=lifespan)

# --- Konfiguracja middleware CORS ---
# Dopuszczamy wszystkie źródła, metody i nagłówki — bezpieczne dla narzędzi lokalnych.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Funkcje pomocnicze ---
def save_config():
    """Zapisuje obecny obiekt CONFIG z powrotem do config.jsonc, starając się zachować komentarze."""
    try:
        # Wczytujemy oryginalny plik, żeby zachować komentarze
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Bezpieczne zastępowanie wartości za pomocą wyrażeń regularnych
        def replacer(key, value, content):
            # Regex znajdzie klucz i dopasuje jego wartość do przecinka lub zamknięcia obiektu
            pattern = re.compile(rf'("{key}"\s*:\s*").*?("?)(,?\s*)$', re.MULTILINE)
            replacement = rf'\g<1>{value}\g<2>\g<3>'
            if not pattern.search(content):  # Jeśli klucz nie istnieje, dodajemy go na końcu (prostsze podejście)
                 content = re.sub(r'}\s*$', f'  ,"{key}": "{value}"\n}}', content)
            else:
                 content = pattern.sub(replacement, content)
            return content

        content_str = "".join(lines)
        content_str = replacer("session_id", CONFIG["session_id"], content_str)
        content_str = replacer("message_id", CONFIG["message_id"], content_str)
        
        with open('config.jsonc', 'w', encoding='utf-8') as f:
            f.write(content_str)
        logger.info("✅ Pomyślnie zaktualizowano informacje o sesji w config.jsonc.")
    except Exception as e:
        logger.error(f"❌ Błąd zapisu do config.jsonc: {e}", exc_info=True)


async def _process_openai_message(message: dict) -> dict:
    """
    Przetwarza wiadomość w formacie OpenAI, rozdzielając tekst i załączniki.
    - Rozbija multimodalne części na czysty tekst i listę załączników.
    - Logika 'file bed' została przeniesiona do preprocesora chat_completions; tutaj tylko budujemy załączniki.
    - Zapewnia, że pusta treść roli 'user' zostanie zastąpiona spacją, aby uniknąć błędów po stronie LMArena.
    """
    content = message.get("content")
    role = message.get("role")
    attachments = []
    text_content = ""

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                # URL może być base64 lub HTTP (już zastąpiony przez preprocesor)
                image_url_data = part.get("image_url", {})
                url = image_url_data.get("url")
                original_filename = image_url_data.get("detail")

                try:
                    # Dla base64 trzeba wyciągnąć content_type
                    if url.startswith("data:"):
                        content_type = url.split(';')[0].split(':')[1]
                    else:
                        # Dla HTTP próbujemy zgadnąć typ MIME
                        content_type = mimetypes.guess_type(url)[0] or 'application/octet-stream'

                    # Na podstawie content_type wybieramy prefiks i rozszerzenie
                    if not original_filename:
                        # Prefiks na podstawie typu głównego
                        main_type = content_type.split('/')[0] if '/' in content_type else 'file'
                        prefix = main_type if main_type in ['image', 'audio', 'video', 'application', 'text'] else 'file'
                        
                        # Używamy mimetypes do uzyskania rozszerzenia
                        ext = mimetypes.guess_extension(content_type)
                        if ext:
                            ext = ext.lstrip('.')
                        else:
                            # Jeśli nie uda się rozpoznać, używamy 'bin'
                            ext = 'bin'
                        
                        file_name = f"{prefix}_{uuid.uuid4()}.{ext}"
                    else:
                        file_name = original_filename
                    
                    attachments.append({
                        "name": file_name,
                        "contentType": content_type,
                        "url": url
                    })

                except (AttributeError, IndexError, ValueError) as e:
                    logger.warning(f"Błąd podczas przetwarzania URL załącznika: {url[:100]}... Błąd: {e}")

        text_content = "\n\n".join(text_parts)
    elif isinstance(content, str):
        text_content = content

    if role == "user" and not text_content.strip():
        text_content = " "

    return {
        "role": role,
        "content": text_content,
        "attachments": attachments
    }

async def convert_openai_to_lmarena_payload(openai_data: dict, session_id: str, message_id: str, mode_override: str = None, battle_target_override: str = None) -> dict:
    """
    Konwertuje ciało żądania OpenAI na uproszczony ładunek wymagany przez skrypt Tampermonkey,
    stosuje tryb Tavern, tryb Bypass oraz tryb battle.
    Dodatkowo obsługuje nadpisanie trybu (mode) dla danego modelu.
    """
    # 1. Normalizacja ról i przetwarzanie wiadomości
    #    - Zmieniamy niestandardową rolę 'developer' na 'system' dla zgodności.
    #    - Rozdzielamy tekst i załączniki.
    messages = openai_data.get("messages", [])
    for msg in messages:
        if msg.get("role") == "developer":
            msg["role"] = "system"
            logger.info("Normalizacja roli wiadomości: zmieniono 'developer' na 'system'.")
            
    processed_messages = []
    for msg in messages:
        processed_msg = await _process_openai_message(msg.copy())
        processed_messages.append(processed_msg)

    # 2. Zastosowanie trybu Tavern (Tavern Mode)
    if CONFIG.get("tavern_mode_enabled"):
        system_prompts = [msg['content'] for msg in processed_messages if msg['role'] == 'system']
        other_messages = [msg for msg in processed_messages if msg['role'] != 'system']
        
        merged_system_prompt = "\n\n".join(system_prompts)
        final_messages = []
        
        if merged_system_prompt:
            # Wiadomości systemowe nie powinny zawierać załączników
            final_messages.append({"role": "system", "content": merged_system_prompt, "attachments": []})
        
        final_messages.extend(other_messages)
        processed_messages = final_messages

    # 3. Określenie docelowego ID modelu
    model_name = openai_data.get("model", "claude-3-5-sonnet-20241022")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})  # Ważne: zawsze zapewniamy słownik
    
    target_model_id = None
    if model_info:
        target_model_id = model_info.get("id")
    else:
        logger.warning(f"Model '{model_name}' nie znaleziony w 'models.json'. Żądanie zostanie wysłane bez specyficznego ID modelu.")

    if not target_model_id:
        logger.warning(f"Model '{model_name}' nie ma przypisanego ID w 'models.json'. Żądanie zostanie wysłane bez ID modelu.")

    # 4. Budowa szablonów wiadomości
    message_templates = []
    for msg in processed_messages:
        message_templates.append({
            "role": msg["role"],
            "content": msg.get("content", ""),
            "attachments": msg.get("attachments", [])
        })
    
    # 4.5. Specjalne: jeśli wiadomość użytkownika kończy się na --bypass i zawiera obraz, budujemy fałszywą odpowiedź asystenta
    if message_templates and message_templates[-1]["role"] == "user":
        last_msg = message_templates[-1]
        if last_msg["content"].strip().endswith("--bypass") and last_msg.get("attachments"):
            has_images = False
            for attachment in last_msg.get("attachments", []):
                if attachment.get("contentType", "").startswith("image/"):
                    has_images = True
                    break
            
            if has_images:
                logger.info("Wykryto znacznik --bypass oraz załącznik obrazu — konstruuję fałszywą wiadomość asystenta.")
                
                # Usuwamy znacznik --bypass z treści użytkownika
                last_msg["content"] = last_msg["content"].strip()[:-9].strip()
                
                # Tworzymy fałszywą wiadomość asystenta z obrazami użytkownika
                fake_assistant_msg = {
                    "role": "assistant",
                    "content": "",  # pusta treść
                    "attachments": last_msg.get("attachments", []).copy()  # kopiujemy obrazy
                }
                
                # Czyścimy załączniki oryginalnej wiadomości użytkownika
                last_msg["attachments"] = []
                
                # Wstawiamy fałszywą wiadomość asystenta przed użytkownikiem
                message_templates.insert(len(message_templates)-1, fake_assistant_msg)
                
                # Jeśli pierwsza wiadomość jest od asystenta, dodajemy fałszywą wiadomość użytkownika na początek
                if message_templates[0]["role"] == "assistant":
                    logger.info("Wykryto, że pierwsza wiadomość jest od asystenta — dodaję fałszywą wiadomość użytkownika.")
                    fake_user_msg = {
                        "role": "user",
                        "content": "Hi",
                        "attachments": []
                    }
                    message_templates.insert(0, fake_user_msg)

    # 5. Zastosowanie trybu Bypass (tylko dla modeli tekstowych)
    model_type = model_info.get("type", "text")
    if CONFIG.get("bypass_enabled") and model_type == "text":
        # Tryb bypass zawsze wstawia pustą wiadomość użytkownika z participantPosition 'a'
        logger.info("Tryb Bypass jest włączony — wstrzykuję pustą wiadomość użytkownika.")
        message_templates.append({"role": "user", "content": " ", "participantPosition": "a", "attachments": []})

    # 6. Ustawienie Participant Position
    # Najpierw sprawdzamy nadpisanie trybu, inaczej używamy globalnej konfiguracji
    mode = mode_override or CONFIG.get("id_updater_last_mode", "direct_chat")
    target_participant = battle_target_override or CONFIG.get("id_updater_battle_target", "A")
    target_participant = target_participant.lower()  # wymuszamy małe litery

    logger.info(f"Ustawiam Participant Positions według trybu '{mode}' (cel: {target_participant if mode == 'battle' else 'N/A'}).")

    for msg in message_templates:
        if msg['role'] == 'system':
            if mode == 'battle':
                # W trybie Battle: system i wybrany asystent są po tej samej stronie (A -> 'a', B -> 'b')
                msg['participantPosition'] = target_participant
            else:
                # DirectChat: system zawsze 'b'
                msg['participantPosition'] = 'b'
        elif mode == 'battle':
            # W trybie Battle, pozostałe wiadomości używają wybranego participant
            msg['participantPosition'] = target_participant
        else:  # DirectChat
            # DirectChat: pozostałe wiadomości używają domyślnie 'a'
            msg['participantPosition'] = 'a'

    return {
        "message_templates": message_templates,
        "target_model_id": target_model_id,
        "session_id": session_id,
        "message_id": message_id
    }

# --- Pomocnicze formatowanie zgodne z OpenAI (bezpieczne JSON) ---
def format_openai_chunk(content: str, model: str, request_id: str) -> str:
    """Formatuje pojedynczy fragment strumieniowy zgodnie z formatem OpenAI SSE."""
    chunk = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def format_openai_finish_chunk(model: str, request_id: str, reason: str = 'stop') -> str:
    """Formatuje końcowy fragment strumieniowy zgodny z OpenAI."""
    chunk = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"

def format_openai_error_chunk(error_message: str, model: str, request_id: str) -> str:
    """Formatuje fragment błędu zgodny z OpenAI SSE."""
    content = f"\n\n[LMArena Bridge Error]: {error_message}"
    return format_openai_chunk(content, model, request_id)

def format_openai_non_stream_response(content: str, model: str, request_id: str, reason: str = 'stop') -> dict:
    """Buduje zgodne z OpenAI kompletne (nie-strumieniowe) ciało odpowiedzi."""
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": reason,
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(content) // 4,
            "total_tokens": len(content) // 4,
        },
    }

async def _process_lmarena_stream(request_id: str):
    """
    Główny generator wewnętrzny: przetwarza surowy strumień z przeglądarki i emituje zdarzenia.
    Typy zdarzeń: ('content', str), ('finish', str), ('error', str)
    """
    global IS_REFRESHING_FOR_VERIFICATION
    queue = response_channels.get(request_id)
    if not queue:
        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: Nie znaleziono kanału odpowiedzi.")
        yield 'error', 'Błąd wewnętrzny serwera: nie znaleziono kanału odpowiedzi.'
        return

    buffer = ""
    timeout = CONFIG.get("stream_response_timeout_seconds",360)
    text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
    # Nowe: wzorzec do dopasowania i wyciągnięcia URLi obrazów
    image_pattern = re.compile(r'[ab]2:(\[.*?\])')
    finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    cloudflare_patterns = [r'<title>Just a moment...</title>', r'Enable JavaScript and cookies to continue']
    
    has_yielded_content = False  # Flaga, czy wygenerowano już treść

    try:
        while True:
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Oczekiwanie na dane z przeglądarki przekroczyło limit ({timeout}s).")
                yield 'error', f'Przekroczono limit oczekiwania na odpowiedź po {timeout} sekundach.'
                return

            # --- Obsługa weryfikacji Cloudflare ---
            def handle_cloudflare_verification():
                global IS_REFRESHING_FOR_VERIFICATION
                if not IS_REFRESHING_FOR_VERIFICATION:
                    logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Wykryto weryfikację CAPTCHA — wysyłam polecenie odświeżenia.")
                    IS_REFRESHING_FOR_VERIFICATION = True
                    if browser_ws:
                        asyncio.create_task(browser_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False)))
                    return "Wykryto weryfikację CAPTCHA. Wysłano polecenie odświeżenia przeglądarki — spróbuj ponownie za kilka sekund."
                else:
                    logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Weryfikacja CAPTCHA już trwa — oczekuję na zakończenie.")
                    return "Trwa oczekiwanie na ukończenie weryfikacji CAPTCHA..."

            # 1. Sprawdzamy, czy otrzymaliśmy bezpośredni błąd z WebSocket
            if isinstance(raw_data, dict) and 'error' in raw_data:
                error_msg = raw_data.get('error', 'Nieznany błąd przeglądarki')
                if isinstance(error_msg, str):
                    if '413' in error_msg or 'too large' in error_msg.lower():
                        friendly_error_msg = "Przesyłanie nie powiodło się: załącznik przekracza limit rozmiaru serwera LMArena (zwykle około 5MB). Spróbuj zmniejszyć plik lub przesłać mniejszy plik."
                        logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Wykryto błąd przekroczenia rozmiaru (413).")
                        yield 'error', friendly_error_msg
                        return
                    if any(re.search(p, error_msg, re.IGNORECASE) for p in cloudflare_patterns):
                        yield 'error', handle_cloudflare_verification()
                        return
                yield 'error', error_msg
                return

            # 2. Sprawdzamy sygnał [DONE]
            if raw_data == "[DONE]":
                # Reset stanu przeniesiono do websocket_endpoint, aby być pewnym, że przy ponownym połączeniu stan zostanie zresetowany
                if has_yielded_content and IS_REFRESHING_FOR_VERIFICATION:
                     logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Żądanie zakończone pomyślnie; stan weryfikacji zostanie zresetowany przy następnym połączeniu.")
                break

            # 3. Doklejamy do bufora i analizujemy
            buffer += "".join(str(item) for item in raw_data) if isinstance(raw_data, list) else raw_data

            if any(re.search(p, buffer, re.IGNORECASE) for p in cloudflare_patterns):
                yield 'error', handle_cloudflare_verification()
                return
            
            if (error_match := error_pattern.search(buffer)):
                try:
                    error_json = json.loads(error_match.group(1))
                    yield 'error', error_json.get("error", "Nieznany błąd z LMArena")
                    return
                except json.JSONDecodeError:
                    pass

            # Najpierw obsługujemy treść tekstową
            while (match := text_pattern.search(buffer)):
                try:
                    text_content = json.loads(f'"{match.group(1)}"')
                    if text_content:
                        has_yielded_content = True
                        yield 'content', text_content
                except (ValueError, json.JSONDecodeError):
                    pass
                buffer = buffer[match.end():]

            # Nowe: obsługa zawartości obrazów
            while (match := image_pattern.search(buffer)):
                try:
                    image_data_list = json.loads(match.group(1))
                    if isinstance(image_data_list, list) and image_data_list:
                        image_info = image_data_list[0]
                        if image_info.get("type") == "image" and "image" in image_info:
                            # Opakowujemy URL w Markdown i emitujemy jako blok treści
                            markdown_image = f"![Image]({image_info['image']})"
                            yield 'content', markdown_image
                except (json.JSONDecodeError, IndexError) as e:
                    logger.warning(f"Błąd parsowania URL obrazu: {e}, bufor: {buffer[:150]}")
                buffer = buffer[match.end():]

            if (finish_match := finish_pattern.search(buffer)):
                try:
                    finish_data = json.loads(finish_match.group(1))
                    yield 'finish', finish_data.get("finishReason", "stop")
                except (json.JSONDecodeError, IndexError):
                    pass
                buffer = buffer[finish_match.end():]

    except asyncio.CancelledError:
        logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Zadanie anulowane.")
    finally:
        if request_id in response_channels:
            del response_channels[request_id]
            logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Kanał odpowiedzi został posprzątany.")

async def stream_generator(request_id: str, model: str):
    """Formatuje wewnętrzny strumień zdarzeń do odpowiedzi SSE w stylu OpenAI."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"STREAMER [ID: {request_id[:8]}]: Uruchomiono generator strumieniowy.")
    
    finish_reason_to_send = 'stop'  # Domyślny powód zakończenia

    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            yield format_openai_chunk(data, model, response_id)
        elif event_type == 'finish':
            # Zapamiętujemy powód zakończenia, ale nie kończymy natychmiast — czekamy na [DONE]
            finish_reason_to_send = data
            if data == 'content-filter':
                warning_msg = "\n\nOdpowiedź została przerwana — możliwe przekroczenie limitu kontekstu lub wewnętrzne filtrowanie modelu."
                yield format_openai_chunk(warning_msg, model, response_id)
        elif event_type == 'error':
            logger.error(f"STREAMER [ID: {request_id[:8]}]: W strumieniu wystąpił błąd: {data}")
            yield format_openai_error_chunk(str(data), model, response_id)
            yield format_openai_finish_chunk(model, response_id, reason='stop')
            return  # Przy błędzie kończymy

    # Wykonujemy to tylko gdy _process_lmarena_stream zakończy się naturalnie (otrzymano [DONE])
    yield format_openai_finish_chunk(model, response_id, reason=finish_reason_to_send)
    logger.info(f"STREAMER [ID: {request_id[:8]}]: Generator strumieniowy zakończył się poprawnie.")

async def non_stream_response(request_id: str, model: str):
    """Agreguje wewnętrzny strumień i zwraca jedną odpowiedź JSON zgodną z OpenAI."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: Rozpoczynam przetwarzanie odpowiedzi nie-strumieniowej.")
    
    full_content = []
    finish_reason = "stop"
    
    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            full_content.append(data)
        elif event_type == 'finish':
            finish_reason = data
            if data == 'content-filter':
                full_content.append("\n\nOdpowiedź została przerwana — możliwe przekroczenie limitu kontekstu lub wewnętrzne filtrowanie modelu.")
            # Nie przerywamy, czekamy na [DONE], by uniknąć warunków wyścigu
        elif event_type == 'error':
            logger.error(f"NON-STREAM [ID: {request_id[:8]}]: Wystąpił błąd podczas przetwarzania: {data}")
            
            # Ustalanie statusu błędu spójnie dla stream / non-stream
            status_code = 413 if "przekracza limit rozmiaru" in str(data) or "załącznik przekracza" in str(data) else 500

            error_response = {
                "error": {
                    "message": f"[LMArena Bridge Error]: {data}",
                    "type": "bridge_error",
                    "code": "attachment_too_large" if status_code == 413 else "processing_error"
                }
            }
            return Response(content=json.dumps(error_response, ensure_ascii=False), status_code=status_code, media_type="application/json")

    final_content = "".join(full_content)
    response_data = format_openai_non_stream_response(final_content, model, response_id, reason=finish_reason)
    
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: Agregacja odpowiedzi zakończona.")
    return Response(content=json.dumps(response_data, ensure_ascii=False), media_type="application/json")

# --- Punkt końcowy WebSocket ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Obsługuje połączenie WebSocket od skryptu Tampermonkey."""
    global browser_ws, IS_REFRESHING_FOR_VERIFICATION
    await websocket.accept()
    if browser_ws is not None:
        logger.warning("Wykryto nowe połączenie skryptu Tampermonkey — poprzednie połączenie zostanie zastąpione.")
    
    # Każde nowe połączenie oznacza zakończenie (lub brak) weryfikacji CAPTCHA
    if IS_REFRESHING_FOR_VERIFICATION:
        logger.info("✅ Nowe połączenie WebSocket nawiązane — stan weryfikacji CAPTCHA zresetowany.")
        IS_REFRESHING_FOR_VERIFICATION = False
        
    logger.info("✅ Skrypt Tampermonkey połączony z WebSocket.")
    browser_ws = websocket
    try:
        while True:
            # Odbieramy wiadomości od skryptu Tampermonkey
            message_str = await websocket.receive_text()
            message = json.loads(message_str)
            
            request_id = message.get("request_id")
            data = message.get("data")

            if not request_id or data is None:
                logger.warning(f"Otrzymano od przeglądarki nieprawidłową wiadomość: {message}")
                continue

            # Umieszczamy odebrane dane w odpowiedniej kolejce odpowiedzi
            if request_id in response_channels:
                await response_channels[request_id].put(data)
            else:
                logger.warning(f"⚠️ Otrzymano odpowiedź dla nieznanego lub zamkniętego żądania: {request_id}")

    except WebSocketDisconnect:
        logger.warning("❌ Klient Tampermonkey rozłączył się.")
    except Exception as e:
        logger.error(f"Nieznany błąd podczas obsługi WebSocket: {e}", exc_info=True)
    finally:
        browser_ws = None
        # Czyścimy wszystkie oczekujące kanały odpowiedzi, aby nie pozostawić wiszących żądań
        for queue in response_channels.values():
            await queue.put({"error": "Browser disconnected during operation"})
        response_channels.clear()
        logger.info("Połączenie WebSocket zostało posprzątane.")

# --- Zgodne z OpenAI endpointy API ---
@app.get("/v1/models")
async def get_models():
    """Zwraca listę modeli zgodną z OpenAI."""
    if not MODEL_NAME_TO_ID_MAP:
        return JSONResponse(
            status_code=404,
            content={"error": "Lista modeli jest pusta lub plik 'models.json' nie został znaleziony."}
        )
    
    return {
        "object": "list",
        "data": [
            {
                "id": model_name, 
                "object": "model",
                "created": int(time.time()),
                "owned_by": "LMArenaBridge"
            }
            for model_name in MODEL_NAME_TO_ID_MAP.keys()
        ],
    }

@app.post("/internal/request_model_update")
async def request_model_update():
    """
    Odbiera żądanie od model_updater.py i wysyła polecenie przez WebSocket,
    aby skrypt Tampermonkey przesłał źródło strony.
    """
    if not browser_ws:
        logger.warning("MODEL UPDATE: Otrzymano żądanie aktualizacji, ale brak połączenia z przeglądarką.")
        raise HTTPException(status_code=503, detail="Klient przeglądarki nie jest połączony.")
    
    try:
        logger.info("MODEL UPDATE: Otrzymano żądanie aktualizacji — wysyłam polecenie przez WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "send_page_source"}))
        logger.info("MODEL UPDATE: Polecenie 'send_page_source' zostało wysłane.")
        return JSONResponse({"status": "success", "message": "Polecenie wysłania źródła strony zostało wysłane."})
    except Exception as e:
        logger.error(f"MODEL UPDATE: Błąd podczas wysyłania polecenia: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Nie udało się wysłać polecenia przez WebSocket.")

@app.post("/internal/update_available_models")
async def update_available_models_endpoint(request: Request):
    """
    Odbiera HTML strony od skryptu Tampermonkey, wyciąga modele i aktualizuje available_models.json.
    """
    html_content = await request.body()
    if not html_content:
        logger.warning("Żądanie aktualizacji modeli nie zawierało treści HTML.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Nie otrzymano treści HTML."}
        )
    
    logger.info("Otrzymano HTML od skryptu Tampermonkey — rozpoczynam ekstrakcję dostępnych modeli...")
    new_models_list = extract_models_from_html(html_content.decode('utf-8'))
    
    if new_models_list:
        save_available_models(new_models_list)
        return JSONResponse({"status": "success", "message": "Plik available_models.json został zaktualizowany."})
    else:
        logger.error("Nie udało się wyodrębnić danych modeli z dostarczonego HTML.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Nie można wyodrębnić danych modeli z HTML."}
        )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Obsługa żądań chat/completions.
    Konwertuje format OpenAI -> LMArena, wysyła przez WebSocket do skryptu Tampermonkey,
    a następnie zwraca wynik (stream lub non-stream).
    """
    global last_activity_time
    last_activity_time = datetime.now()  # Aktualizujemy czas aktywności
    logger.info(f"Otrzymano żądanie API — czas aktywności zaktualizowany: {last_activity_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        openai_req = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Nieprawidłowe ciało żądania JSON")

    model_name = openai_req.get("model")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})  # Ważne: jeśli brak modelu, otrzymujemy pusty słownik
    model_type = model_info.get("type", "text")  # Domyślnie text

    # --- Nowe: Logika rozpoznawania typu modelu ---
    if model_type == 'image':
        logger.info(f"Wykryto, że model '{model_name}' jest typu 'image' — będzie obsłużony przez główny endpoint chat.")
        # Dla modeli obrazkowych ponownie używamy głównej logiki, ponieważ _process_lmarena_stream obsługuje obrazy.
        pass  # Kontynuujemy wspólną obsługę chat
    # --- Koniec logiki generowania obrazów ---

    # Jeśli model nie jest obrazkowy, wykonujemy standardową logikę tekstową
    load_config()  # Wczytujemy aktualną konfigurację na żywo, by mieć aktualne sessionId itd.
    # --- Weryfikacja API Key ---
    api_key = CONFIG.get("api_key")
    if api_key:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(
                status_code=401,
                detail="Brakujący klucz API. Podaj nagłówek Authorization w formacie 'Bearer YOUR_KEY'."
            )
        
        provided_key = auth_header.split(' ')[1]
        if provided_key != api_key:
            raise HTTPException(
                status_code=401,
                detail="Podany klucz API jest nieprawidłowy."
            )

    # --- Wzmacniana kontrola połączenia rozwiązująca warunki wyścigu po weryfikacji CAPTCHA ---
    if IS_REFRESHING_FOR_VERIFICATION and not browser_ws:
        raise HTTPException(
            status_code=503,
            detail="Oczekiwanie na odświeżenie przeglądarki w celu ukończenia weryfikacji CAPTCHA — spróbuj ponownie za kilka sekund."
        )

    if not browser_ws:
        raise HTTPException(
            status_code=503,
            detail="Klient Tampermonkey nie jest podłączony. Upewnij się, że strona LMArena jest otwarta i skrypt aktywny."
        )

    # --- Mapowanie model -> session/message ID ---
    session_id, message_id = None, None
    mode_override, battle_target_override = None, None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = None

        if isinstance(mapping_entry, list) and mapping_entry:
            selected_mapping = random.choice(mapping_entry)
            logger.info(f"Dla modelu '{model_name}' wybrano losowo jedno z mapowań ID.")
        elif isinstance(mapping_entry, dict):
            selected_mapping = mapping_entry
            logger.info(f"Dla modelu '{model_name}' znaleziono pojedyncze mapowanie endpointu (stary format).")
        
        if selected_mapping:
            session_id = selected_mapping.get("session_id")
            message_id = selected_mapping.get("message_id")
            # Pobieramy także informacje o trybie
            mode_override = selected_mapping.get("mode")  # może być None
            battle_target_override = selected_mapping.get("battle_target")  # może być None
            log_msg = f"Zostanie użyte Session ID: ...{session_id[-6:] if session_id else 'N/A'}"
            if mode_override:
                log_msg += f" (tryb: {mode_override}"
                if mode_override == 'battle':
                    log_msg += f", cel: {battle_target_override or 'A'}"
                log_msg += ")"
            logger.info(log_msg)

    # Jeśli nadal brak session_id, stosujemy logikę globalnego fallbacku
    if not session_id:
        if CONFIG.get("use_default_ids_if_mapping_not_found", True):
            session_id = CONFIG.get("session_id")
            message_id = CONFIG.get("message_id")
            # Przy użyciu globalnych ID nie nadpisujemy trybu — użyjemy konfiguracji globalnej
            mode_override, battle_target_override = None, None
            logger.info(f"Model '{model_name}' nie miał mapowania — użyto domyślnego Session ID: ...{session_id[-6:] if session_id else 'N/A'}")
        else:
            logger.error(f"Model '{model_name}' nie ma wpisu w 'model_endpoint_map.json' i wyłączono fallback do domyślnych ID.")
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model_name}' nie ma skonfigurowanego indywidualnego ID sesji. Dodaj mapowanie w 'model_endpoint_map.json' lub włącz 'use_default_ids_if_mapping_not_found' w 'config.jsonc'."
            )

    # --- Walidacja ostatecznych ID sesji ---
    if not session_id or not message_id or "YOUR_" in session_id or "YOUR_" in message_id:
        raise HTTPException(
            status_code=400,
            detail="Ostateczne session_id lub message_id są nieprawidłowe. Sprawdź konfigurację w 'model_endpoint_map.json' i 'config.jsonc' lub uruchom `id_updater.py`."
        )

    if not model_name or model_name not in MODEL_NAME_TO_ID_MAP:
        logger.warning(f"Żądany model '{model_name}' nie występuje w models.json — zostanie użyty domyślny model.")

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()
    logger.info(f"API CALL [ID: {request_id[:8]}]: Utworzono kanał odpowiedzi.")

    try:
        # --- Preprocessing załączników (w tym upload do file bed) ---
        # Przetwarzamy wszystkie załączniki przed komunikacją z przeglądarką; jeśli wystąpi błąd, od razu zwracamy błąd.
        messages_to_process = openai_req.get("messages", [])
        for message in messages_to_process:
            content = message.get("content")
            if isinstance(content, list):
                for i, part in enumerate(content):
                    if part.get("type") == "image_url" and CONFIG.get("file_bed_enabled"):
                        image_url_data = part.get("image_url", {})
                        base64_url = image_url_data.get("url")
                        original_filename = image_url_data.get("detail")
                        
                        if not (base64_url and base64_url.startswith("data:")):
                            raise ValueError(f"Nieprawidłowy format danych obrazka: {base64_url[:100] if base64_url else 'None'}")

                        upload_url = CONFIG.get("file_bed_upload_url")
                        if not upload_url:
                            raise ValueError("Włączono file bed, ale 'file_bed_upload_url' nie jest skonfigurowane.")
                        
                        # Naprawiamy ewentualne escape'owane ukośniki
                        upload_url = upload_url.replace('\\/', '/')

                        api_key = CONFIG.get("file_bed_api_key")
                        
                        # Jeśli brak nazwy pliku, generujemy ją na podstawie MIME z base64
                        if not original_filename:
                            try:
                                content_type = base64_url.split(';')[0].split(':')[1]
                                
                                # Prefiks na podstawie typu MIME
                                main_type = content_type.split('/')[0] if '/' in content_type else 'file'
                                prefix = main_type if main_type in ['image', 'audio', 'video', 'application', 'text'] else 'file'
                                
                                # Próbujemy uzyskać rozszerzenie z mimetypes
                                ext = mimetypes.guess_extension(content_type)
                                if ext:
                                    ext = ext.lstrip('.')
                                else:
                                    ext = 'bin'
                                
                                file_name = f"{prefix}_{uuid.uuid4()}.{ext}"
                            except Exception as e:
                                logger.warning(f"Błąd parsowania MIME — używam domyślnej nazwy pliku: {e}")
                                file_name = f"file_{uuid.uuid4()}.bin"
                        else:
                            file_name = original_filename
                        
                        logger.info(f"Preprocessing file bed: wysyłam '{file_name}' (MIME: {base64_url.split(';')[0].split(':')[1] if base64_url.startswith('data:') else 'unknown'})...")
                        
                        uploaded_filename, error_message = await upload_to_file_bed(file_name, base64_url, upload_url, api_key)

                        if error_message:
                            raise IOError(f"Błąd uploadu do file bed: {error_message}")
                        
                        # Zgodnie z konwencją konfiguracji budujemy finalny URL do pliku
                        url_prefix = upload_url.rsplit('/', 1)[0]
                        final_url = f"{url_prefix}/uploads/{uploaded_filename}"
                        
                        part["image_url"]["url"] = final_url
                        logger.info(f"URL załącznika został zastąpiony wartością: {final_url}")

        # 1. Konwersja żądania (w tej chwili nie powinno być już załączników wymagających uploadu)
        lmarena_payload = await convert_openai_to_lmarena_payload(
            openai_req,
            session_id,
            message_id,
            mode_override=mode_override,
            battle_target_override=battle_target_override
        )
        
        # Dodatkowo informujemy skrypt Tampermonkey, jeśli żądanie dotyczy obrazu
        if model_type == 'image':
            lmarena_payload['is_image_request'] = True
        
        # 2. Przygotowanie wiadomości do wysłania do przeglądarki
        message_to_browser = {
            "request_id": request_id,
            "payload": lmarena_payload
        }
        
        # 3. Wysyłka przez WebSocket
        logger.info(f"API CALL [ID: {request_id[:8]}]: Wysyłam ładunek do skryptu Tampermonkey przez WebSocket.")
        await browser_ws.send_text(json.dumps(message_to_browser))

        # 4. Zwracamy strumieniowo lub jako non-stream w zależności od parametru stream
        is_stream = openai_req.get("stream", False)

        if is_stream:
            # Zwracamy odpowiedź strumieniową
            return StreamingResponse(
                stream_generator(request_id, model_name or "default_model"),
                media_type="text/event-stream"
            )
        else:
            # Zwracamy odpowiedź nie-strumieniową
            return await non_stream_response(request_id, model_name or "default_model")
    except (ValueError, IOError) as e:
        # Błędy związane z przetwarzaniem załączników
        logger.error(f"API CALL [ID: {request_id[:8]}]: Błąd podczas preprocesu załączników: {e}")
        if request_id in response_channels:
            del response_channels[request_id]
        # Zwracamy poprawnie sformatowany błąd JSON
        return JSONResponse(
            status_code=500,
            content={"error": {"message": f"[LMArena Bridge Error] Błąd przetwarzania załączników: {e}", "type": "attachment_error"}}
        )
    except Exception as e:
        # Inne nieoczekiwane błędy
        if request_id in response_channels:
            del response_channels[request_id]
        logger.error(f"API CALL [ID: {request_id[:8]}]: Krytyczny błąd podczas obsługi żądania: {e}", exc_info=True)
        # Zwracamy także poprawnie sformatowany błąd JSON
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_server_error"}}
        )

# --- Endpoints wewnętrzne ---
@app.post("/internal/start_id_capture")
async def start_id_capture():
    """
    Odbiera żądanie od id_updater.py i wysyła przez WebSocket polecenie
    aktywacji trybu przechwytywania ID w skrypcie Tampermonkey.
    """
    if not browser_ws:
        logger.warning("ID CAPTURE: Otrzymano żądanie aktywacji, ale brak połączenia z przeglądarką.")
        raise HTTPException(status_code=503, detail="Klient przeglądarki nie jest połączony.")
    
    try:
        logger.info("ID CAPTURE: Otrzymano prośbę o aktywację — wysyłam polecenie przez WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "activate_id_capture"}))
        logger.info("ID CAPTURE: Polecenie aktywacji zostało wysłane.")
        return JSONResponse({"status": "success", "message": "Polecenie aktywacji zostało wysłane."})
    except Exception as e:
        logger.error(f"ID CAPTURE: Błąd podczas wysyłania polecenia aktywacji: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Nie udało się wysłać polecenia przez WebSocket.")


# --- Punkt wejścia programu ---
if __name__ == "__main__":
    # Sugerowane: wczytywać port z config.jsonc; tutaj tymczasowo zakodowany
    api_port = 5102
    logger.info(f"🚀 Serwer API LMArena Bridge v2.0 uruchamia się...")
    logger.info(f"   - Adres nasłuchu: http://127.0.0.1:{api_port}")
    logger.info(f"   - Punkt WebSocket: ws://127.0.0.1:{api_port}/ws")
    
    uvicorn.run(app, host="0.0.0.0", port=api_port)