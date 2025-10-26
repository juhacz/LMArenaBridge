# id_updater.py
#
# To jednorazowy, zaktualizowany serwer HTTP, który na podstawie wybranego przez użytkownika trybu
# (DirectChat lub Battle) odbiera informacje o sesji z skryptu Tampermonkey i aktualizuje plik config.jsonc.

import http.server
import socketserver
import json
import re
import threading
import os
import requests

# --- Konfiguracja ---
HOST = "127.0.0.1"
PORT = 5103
CONFIG_PATH = 'config.jsonc'

def read_config():
    """Wczytuje i parsuje plik config.jsonc, usuwając komentarze przed parsowaniem."""
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ Błąd: plik konfiguracyjny '{CONFIG_PATH}' nie istnieje.")
        return None
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Bardziej odporne usuwanie komentarzy — przetwarzamy linię po linii, aby nie usuwać "//" w URL-ach
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

        json_content = "".join(no_comments_lines)
        return json.loads(json_content)
    except Exception as e:
        print(f"❌ Błąd podczas odczytu lub parsowania '{CONFIG_PATH}': {e}")
        return None

def save_config_value(key, value):
    """
    Bezpiecznie aktualizuje pojedynczą parę klucz-wartość w config.jsonc, zachowując oryginalne formatowanie i komentarze.
    Działa dla wartości typu string lub number.
    """
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()

        # Używamy wyrażenia regularnego do bezpiecznej zamiany wartości
        # Znajdzie "key": "dowolna_wartość" i zastąpi dowolna_wartość nową wartością
        pattern = re.compile(rf'("{key}"\s*:\s*")[^"]*(")')
        new_content, count = pattern.subn(rf'\g<1>{value}\g<2>', content, 1)

        if count == 0:
            print(f"🤔 Ostrzeżenie: Nie znaleziono klucza '{key}' w pliku '{CONFIG_PATH}'.")
            return False

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    except Exception as e:
        print(f"❌ Błąd podczas aktualizacji '{CONFIG_PATH}': {e}")
        return False

def save_session_ids(session_id, message_id):
    """Zapisuje nowe session_id i message_id do pliku config.jsonc."""
    print(f"\n📝 Próbuję zapisać ID do '{CONFIG_PATH}'...")
    res1 = save_config_value("session_id", session_id)
    res2 = save_config_value("message_id", message_id)
    if res1 and res2:
        print(f"✅ Pomyślnie zaktualizowano ID.")
        print(f"   - session_id: {session_id}")
        print(f"   - message_id: {message_id}")
    else:
        print(f"❌ Aktualizacja ID nie powiodła się. Sprawdź powyższe komunikaty o błędach.")

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path == '/update':
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data)

                session_id = data.get('sessionId')
                message_id = data.get('messageId')

                if session_id and message_id:
                    print("\n" + "=" * 50)
                    print("🎉 Pomyślnie przechwycono ID z przeglądarki!")
                    print(f"  - Session ID: {session_id}")
                    print(f"  - Message ID: {message_id}")
                    print("=" * 50)

                    save_session_ids(session_id, message_id)

                    self.send_response(200)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"status": "success"}')

                    print("\nZadanie zakończone. Serwer zamknie się automatycznie za 1 sekundę.")
                    threading.Thread(target=self.server.shutdown).start()

                else:
                    self.send_response(400, "Bad Request")
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Brak sessionId lub messageId"}')
            except Exception as e:
                self.send_response(500, "Internal Server Error")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(f'{{"error": "Błąd wewnętrzny serwera: {e}"}}'.encode('utf-8'))
        else:
            self.send_response(404, "Not Found")
            self._send_cors_headers()
            self.end_headers()

    def log_message(self, format, *args):
        # Wyłączamy domyślne logowanie HTTP, żeby konsola była czytelniejsza
        return

def run_server():
    with socketserver.TCPServer((HOST, PORT), RequestHandler) as httpd:
        print("\n" + "="*50)
        print("  🚀 Nasłuchiwacz aktualizacji Session ID uruchomiony")
        print(f"  - Adres nasłuchu: http://{HOST}:{PORT}")
        print("  - Wykonaj w przeglądarce operacje na stronie LMArena, aby wywołać przechwycenie ID.")
        print("  - Po pomyślnym przechwyceniu ten skrypt zamknie się automatycznie.")
        print("="*50)
        httpd.serve_forever()

def notify_api_server():
    """Powiadamia główny serwer API, że proces aktualizacji ID został rozpoczęty."""
    api_server_url = "http://127.0.0.1:5102/internal/start_id_capture"
    try:
        response = requests.post(api_server_url, timeout=3)
        if response.status_code == 200:
            print("✅ Pomyślnie powiadomiono serwer główny o aktywacji trybu przechwytywania ID.")
            return True
        else:
            print(f"⚠️ Powiadomienie serwera głównego nie powiodło się, kod statusu: {response.status_code}.")
            print(f"   - Treść odpowiedzi: {response.text}")
            return False
    except requests.ConnectionError:
        print("❌ Nie można połączyć się z głównym serwerem API. Upewnij się, że api_server.py jest uruchomiony.")
        return False
    except Exception as e:
        print(f"❌ Wystąpił nieznany błąd podczas powiadamiania serwera głównego: {e}")
        return False

if __name__ == "__main__":
    config = read_config()
    if not config:
        exit(1)

    # --- Pobranie wyboru użytkownika ---
    last_mode = config.get("id_updater_last_mode", "direct_chat")
    mode_map = {"a": "direct_chat", "b": "battle"}
    
    prompt = f"Wybierz tryb [a: DirectChat, b: Battle] (domyślnie ostatnio wybrany: {last_mode}): "
    choice = input(prompt).lower().strip()

    if not choice:
        mode = last_mode
    else:
        mode = mode_map.get(choice)
        if not mode:
            print(f"Nieprawidłowy wybór — używam wartości domyślnej: {last_mode}")
            mode = last_mode

    save_config_value("id_updater_last_mode", mode)
    print(f"Obecny tryb: {mode.upper()}")
    
    if mode == 'battle':
        last_target = config.get("id_updater_battle_target", "A")
        target_prompt = f"Wybierz wiadomość do aktualizacji [A (dla modeli search wybierz A) lub B] (domyślnie: {last_target}): "
        target_choice = input(target_prompt).upper().strip()

        if not target_choice:
            target = last_target
        elif target_choice in ["A", "B"]:
            target = target_choice
        else:
            print(f"Nieprawidłowy wybór — używam wartości domyślnej: {last_target}")
            target = last_target
        
        save_config_value("id_updater_battle_target", target)
        print(f"Battle — cel: Assistant {target}")
        print("Uwaga: niezależnie od wyboru (A lub B), przechwycone ID zostaną zapisane jako główne session_id i message_id.")

    # Przed uruchomieniem serwera powiadamiamy główny serwis
    if notify_api_server():
        run_server()
        print("Serwer został zatrzymany.")
    else:
        print("\nProces aktualizacji ID przerwany z powodu braku komunikacji z serwerem głównym.")