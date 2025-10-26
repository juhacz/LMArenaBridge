# modules/file_uploader.py
import httpx
import logging

logger = logging.getLogger(__name__)

from typing import Tuple

from typing import Tuple

async def upload_to_file_bed(file_name: str, file_data: str, upload_url: str, api_key: str | None = None) -> Tuple[str | None, str | None]:
    """
    Wysyła plik zakodowany w base64 do serwera file bed.

    :param file_name: Oryginalna nazwa pliku.
    :param file_data: Base64 data URI (np. "data:image/png;base64,...").
    :param upload_url: URL endpointu /upload serwera file bed.
    :param api_key: (opcjonalnie) Klucz API do autoryzacji.
    :return: Krotka (filename, error_message). Przy sukcesie filename to nazwa pliku, error_message jest None;
             przy niepowodzeniu filename jest None, a error_message zawiera opis błędu.
    """
    payload = {
        "file_name": file_name,
        "file_data": file_data,
        "api_key": api_key
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(upload_url, json=payload)
            
            response.raise_for_status()  # jeśli status 4xx/5xx, rzuć wyjątek
            
            result = response.json()
            if result.get("success") and result.get("filename"):
                logger.info(f"Plik '{file_name}' został pomyślnie przesłany do file bed, nazwa pliku: {result['filename']}")
                return result["filename"], None
            else:
                error_msg = result.get("error", "Serwer file bed zwrócił nieznany błąd.")
                logger.error(f"Przesyłanie do file bed nie powiodło się: {error_msg}")
                return None, error_msg
                
    except httpx.HTTPStatusError as e:
        error_details = f"Błąd HTTP: {e.response.status_code} - {e.response.text}"
        logger.error(f"Wystąpił błąd podczas przesyłania do file bed: {error_details}")
        return None, error_details
    except httpx.RequestError as e:
        error_details = f"Błąd połączenia: {e}"
        logger.error(f"Błąd połączenia z serwerem file bed: {e}")
        return None, error_details
    except Exception as e:
        error_details = f"Nieznany błąd: {e}"
        logger.error(f"Podczas przesyłania pliku wystąpił nieznany błąd: {e}", exc_info=True)
        return None, error_details
