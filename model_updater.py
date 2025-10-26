# model_updater.py
import requests
import time
import logging

# --- Konfiguracja ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
API_SERVER_URL = "http://127.0.0.1:5102" # Powinno pasować do portu używanego przez api_server.py

def trigger_model_update():
    """
    Powiadamia serwer główny o rozpoczęciu procesu aktualizacji listy modeli.
    """
    try:
        logging.info("Wysyłam żądanie aktualizacji listy modeli do serwera głównego...")
        response = requests.post(f"{API_SERVER_URL}/internal/request_model_update")
        response.raise_for_status()
        
        if response.json().get("status") == "success":
            logging.info("✅ Pomyślnie wysłano żądanie aktualizacji listy modeli.")
            logging.info("Upewnij się, że strona LMArena jest otwarta — skrypt automatycznie pobierze najnowszą listę modeli.")
            logging.info("Serwer zapisze wynik do pliku `available_models.json`.")
        else:
            logging.error(f"❌ Serwer zwrócił błąd: {response.json().get('message')}")

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Nie można połączyć się z serwerem głównym ({API_SERVER_URL}).")
        logging.error("Upewnij się, że `api_server.py` jest uruchomiony.")
    except Exception as e:
        logging.error(f"Wystąpił nieznany błąd: {e}")

if __name__ == "__main__":
    trigger_model_update()
    # Po wykonaniu skrypt automatycznie zakończy działanie
    time.sleep(2)
