# 🚀 LMArena Bridge — proxy API dla LMArena.ai 🌉

Witamy w nowej wersji LMArena Bridge! 🎉 To narzędzie oparte na FastAPI i WebSocket zapewnia wysokowydajne pośrednictwo, które pozwala używać modeli z platformy [LMArena.ai](https://lmarena.ai/) za pomocą dowolnego klienta zgodnego z API OpenAI.

Ta przebudowana wersja ma na celu lepszą stabilność, łatwiejszą konserwację i rozbudowę.

## ✨ Główne cechy

- 🚀 Wysokowydajny backend: oparty na FastAPI i Uvicorn, asynchroniczny i szybki.
- 🔌 Stabilna komunikacja WebSocket: zastępuje Server-Sent Events (SSE) dla bardziej niezawodnej, dwukierunkowej komunikacji o niskim opóźnieniu.
- 🤖 Interfejs kompatybilny z OpenAI: zgodny z końcówkami `v1/chat/completions`, `v1/models` oraz `v1/images/generations`.
- 📋 Ręczna aktualizacja listy modeli: skrypt `model_updater.py` do ręcznego pobierania listy dostępnych modeli ze strony LMArena i zapisania do `available_models.json`.
- 📎 Uniwersalny upload plików: obsługa plików w Base64 (obrazy, audio, PDF, kod itp.) i przesyłania wielu plików naraz.
- 🎨 Zintegrowane strumieniowe generowanie obrazów: obsługa modeli generujących obrazy poprzez endpoint `/v1/chat/completions`, zwracając obrazy w formacie Markdown w strumieniu, podobnie jak tekst.
- 🗣️ Pełne wsparcie historii konwersacji: automatyczne wstrzykiwanie historii rozmowy do LMArena dla zachowania kontekstu.
- 🌊 Strumieniowe odpowiedzi w czasie rzeczywistym: jak w natywnym API OpenAI.
- 🔄 Automatyczne aktualizacje programu: przy starcie sprawdza repozytorium GitHub i może pobrać aktualizacje.
- 🆔 Aktualizacja ID sesji jednym kliknięciem: skrypt `id_updater.py` automatycznie przechwytuje i zapisuje `sessionId`/`messageId` do `config.jsonc`.
- ⚙️ Automatyzacja w przeglądarce: skrypt Tampermonkey (`LMArenaApiBridge.js`) współpracuje z backendem i wykonuje niezbędne operacje w przeglądarce.
- 🍻 Tryb „Tavern” (Tavern Mode): dedykowany SillyTavern i podobnym aplikacjom — inteligentne łączenie promptów `system`.
- 🤫 Tryb Bypass: próba obejścia filtrów poprzez dodanie pustej wiadomości użytkownika; przy załącznikach obrazów można dodać `--bypass` na końcu prompta, by skonstruować fałszywą odpowiedź AI i ominąć dodatkowe sprawdzenia.
- 🔐 Ochrona klucza API: możliwość ustawienia klucza w pliku konfiguracyjnym.
- 🎯 Zaawansowane mapowanie modeli i sesji: przypisywanie osobnych puli sessionId dla różnych modeli, z możliwością określenia trybu (np. `battle` lub `direct_chat`).
- 🖼️ Opcjonalny zewnętrzny „file bed” (serwer przechowywania plików): pozwala na upload większych plików i różnych typów, omijając ograniczenia LMArena dotyczące Base64.

## 📂 Nowość: serwer przechowywania plików (file bed)

Aby obejść ograniczenia LMArena dotyczące rozmiaru Base64 (zwykle ~5MB) i obsłużyć więcej typów plików, projekt zawiera teraz oddzielny serwer plików.

### Jak to działa

1. Włączysz `file_bed_enabled` w `config.jsonc`.
2. `api_server.py` wykryje załączniki w formacie `data:` URI.
3. Wywoła API serwera plików `/upload`, aby przesłać plik.
4. Serwer zapisze plik w `file_bed_server/uploads/` i zwróci publiczny URL (np. `http://127.0.0.1:5104/uploads/xxxx.png`).
5. `api_server.py` wstawi ten URL jako czysty tekst do treści wiadomości zamiast przesyłać załącznik.
6. Dzięki temu duże pliki, wideo itp. mogą być przesłane jako link do modelu.

### Jak używać

1. Instalacja zależności
   ```bash
   cd file_bed_server
   pip install -r requirements.txt
   cd ..
   ```

2. Uruchom serwer plików (w nowym terminalu):
   ```bash
   python file_bed_server/main.py
   ```
   Domyślnie serwer działa pod `http://127.0.0.1:5104`.

3. Zmiana konfiguracji głównej
   W `config.jsonc` ustaw:
   - `"file_bed_enabled": true,`
   - `"file_bed_upload_url": "http:\/\/127.0.0.1:5180/upload",` (upewnij się, że adres i port są poprawne)
   - `"file_bed_api_key": "twoj_tajny_klucz"` (opcjonalnie, jeśli zmieniono API_KEY w serwerze plików)

4. Uruchom główny serwis (`api_server.py`) i wysyłaj żądania z załącznikami — będą automatycznie obsługiwane przez file bed.

## ⚙️ Pliki konfiguracyjne

Główne zachowanie programu kontrolowane jest przez `config.jsonc`, `models.json` i `model_endpoint_map.json`.

### `models.json` — podstawowe mapowanie modeli

Zawiera mapowanie nazw modeli (publicznych) na ich ID używane przez LMArena. Można też określić typ modelu.

- Ważne: plik jest wymagany do działania programu.
- Format:
  - tekstowe modele: `"nazwa-modelu": "model-id"`
  - modele generujące obrazy: `"nazwa-modelu": "model-id:image"`
- Program rozpoznaje modele obrazkowe po występowaniu `:image` w ID; brak sufiksu = typ `text`.

Przykład:
```json
{
  "gemini-1.5-pro-flash-20240514": "gemini-1.5-pro-flash-20240514",
  "dall-e-3": "null:image"
}
```

### `available_models.json` — lista dostępnych modeli (opcjonalna)

- Plik referencyjny generowany przez `model_updater.py`.
- Zawiera pełne informacje o modelach pobrane z LMArena (`id`, `publicName`, itp.).
- Użyj go do uzupełnienia `models.json`.

### `config.jsonc` — konfiguracja globalna

Zawiera ustawienia globalne jak `session_id`, `message_id`, tryby domyślne i flagi sterujące.

- `session_id`/`message_id`: wartości domyślne używane, gdy nie znaleziono mapowania w `model_endpoint_map.json`.
- `id_updater_last_mode` / `id_updater_battle_target`: domyślne tryby dla przechwytywania ID.
- `use_default_ids_if_mapping_not_found`:
  - true (domyślnie): gdy nie ma mapowania modelu — użyj wartości domyślnych z `config.jsonc`.
  - false: gdy brak mapowania — zwróć błąd (przydatne przy ścisłej kontroli per-model).
- Inne opcje: `api_key`, `tavern_mode_enabled` itd. — patrz komentarze w pliku.

### `model_endpoint_map.json` — przypisania modeli do sesji

Zaawansowane ustawienia pozwalają określić indywidualne puli sesji dla konkretnych modeli.

Zalety:
1. Izolacja konwersacji między modelami.
2. Lepsza równoważność obciążenia przez pulę sessionId.
3. Możliwość wiązania trybu (np. `direct_chat` lub `battle`) z konkretnymi sesjami.

Przykład:
```json
{
  "claude-3-opus-20240229": [
    {
      "session_id": "session_for_direct_chat_1",
      "message_id": "message_for_direct_chat_1",
      "mode": "direct_chat"
    },
    {
      "session_id": "session_for_battle_A",
      "message_id": "message_for_battle_A",
      "mode": "battle",
      "battle_target": "A"
    }
  ],
  "gemini-1.5-pro-20241022": {
      "session_id": "single_session_id_no_mode",
      "message_id": "single_message_id_no_mode"
  }
}
```
- Dla Opus możesz skonfigurować pulę — program wybierze losowo jeden wpis i użyje powiązanego `mode` i `battle_target`.
- Dla Geminiego można użyć pojedynczego obiektu (stara, nadal obsługiwana forma). Jeśli brak `mode`, użyty zostanie tryb globalny z `config.jsonc`.

## 🛠️ Instalacja i użycie

Wymagane: środowisko Python i przeglądarka obsługująca Tampermonkey.

### 1. Przygotowanie

- Instalacja zależności:
  ```bash
  pip install -r requirements.txt
  ```

- Instalacja menedżera skryptów Tampermonkey w przeglądarce (Chrome, Firefox, Edge).

- Instalacja skryptu Tampermonkey:
  1. Otwórz panel Tampermonkey.
  2. Dodaj nowy skrypt.
  3. Wklej zawartość `TampermonkeyScript/LMArenaApiBridge.js`.
  4. Zapisz.

### 2. Uruchomienie serwera

1. W katalogu projektu uruchom:
   ```bash
   python api_server.py
   ```
   Po uruchomieniu serwisu na `http://127.0.0.1:5102` serwer jest gotowy.

2. Upewnij się, że masz otwartą przynajmniej jedną stronę LMArena z aktywnym skryptem Tampermonkey (ikona statusu powinna zmienić się na ✅). Nie musi to być strona rozmowy — wystarczy domena.

### 3. Aktualizacja listy modeli (opcjonalnie)

Generuje `available_models.json`:

1. Upewnij się, że główny serwer działa.
2. W nowym terminalu uruchom:
   ```bash
   python model_updater.py
   ```
3. Skrypt poprosi przeglądarkę o przesłanie źródła strony i zapisze `available_models.json`.
4. Skopiuj interesujące Cię wpisy (`publicName` i `id`) do `models.json`.

### 4. Konfiguracja session ID (zwykle wykonuje się raz)

1. Uruchom `api_server.py`.
2. W nowym terminalu uruchom:
   ```bash
   python id_updater.py
   ```
   - Wybierz tryb (DirectChat / Battle).
   - Skrypt powiadomi serwer, który włączy tryb przechwytujący w skrypcie przeglądarkowym.

3. W przeglądarce:
   - Po aktywacji tytuł strony LMArena pokaże ikonę wskazującą tryb przechwytywania.
   - Otwórz stronę z odpowiedzią docelowego modelu (dla Battle nie podglądaj nazwy modelu).
   - Kliknij „Retry” (Ponów) przy odpowiedzi modelu — skrypt przechwyci `sessionId` i `messageId` i wyśle je do `id_updater.py`.

4. W terminalu `id_updater.py` zobaczysz zapisane ID i komunikat o zapisaniu do `config.jsonc`. Skrypt zakończy działanie.

### 5. Konfiguracja klienta OpenAI

W kliencie ustaw:
- API Base URL: `http://127.0.0.1:5102/v1`
- API Key: jeśli `api_key` w `config.jsonc` jest pusty — dowolny, jeśli ustawiony — podaj poprawny.
- Model: podaj nazwę zgodną z `models.json`.

### 6. Rozpocznij rozmowę 💬

Teraz można używać klienta OpenAI — żądania będą przepuszczane przez lokalny serwer na LMArena.

## 🤔 Jak to działa (skrót)

Projekt składa się z lokalnego serwera FastAPI i skryptu Tampermonkey działającego w przeglądarce. Komunikacja odbywa się przez WebSocket.

```mermaid
sequenceDiagram
    participant C as Klient OpenAI 💻
    participant S as Lokalny serwer FastAPI 🐍
    participant MU as Skrypt model_updater.py 📋
    participant IU as Skrypt id_updater.py 🆔
    participant T as Skrypt Tampermonkey 🐵 (w LMArena)
    participant L as LMArena.ai 🌐

    alt Inicjalizacja
        T->>+S: (ładowanie strony) nawiąż WebSocket
        S-->>-T: potwierdzenie połączenia
    end

    alt Ręczna aktualizacja listy modeli
        MU->>+S: POST /internal/request_model_update
        S->>T: (WebSocket) wyślij 'send_page_source'
        T->>T: pobierz HTML strony
        T->>S: POST /internal/update_available_models (z HTML)
        S->>S: parsuj HTML i zapisz available_models.json
        S-->>-MU: potwierdzenie
    end

    alt Ręczne przechwycenie ID sesji
        IU->>+S: POST /internal/start_id_capture
        S->>T: (WebSocket) wyślij 'activate_id_capture'
        T->>L: (użytkownik klik) przechwyć fetch
        T->>IU: (HTTP) wyślij przechwycone ID
        IU->>IU: zaktualizuj config.jsonc
        IU-->>-T: potwierdzenie
    end

    alt Normalny przepływ rozmowy
        C->>+S: POST /v1/chat/completions
        S->>S: konwersja do formatu LMArena (szukaj modelu w models.json)
        S->>T: (WebSocket) wyślij zadanie z request_id
        T->>L: (fetch) wyślij żądanie do LMArena
        L-->>T: (strumieniowo) odpowiedź modelu
        T->>S: (WebSocket) odeślij fragmenty odpowiedzi
        S-->>-C: (strumieniowo) przekaż odpowiedź klientowi
    end

    alt Generowanie obrazów
        C->>+S: POST /v1/chat/completions (model obrazkowy)
        S->>S: wykryj typ modelu i utwórz zadania (n razy)
        S->>T: (WebSocket) wyślij n zadań z request_id
        T->>L: (fetch) wysyłaj żądania
        L-->>T: (strumieniowo) zwraca URL-e obrazów
        T->>S: (WebSocket) odsyła URL-e
        S->>S: sformatuj jako Markdown
        S-->>-C: zwróć odpowiedź jak zwykły chat
    end
```

1. Połączenie: skrypt Tampermonkey łączy się z lokalnym serwerem przez WebSocket. Uwaga: tylko ostatnia otwarta karta przeglądarki jest aktywna.
2. Odbiór żądań: klient OpenAI wysyła typowe żądanie chat z polem `model`.
3. Dystrybucja zadań: serwer mapuje nazwę modelu na ID z `models.json`, pakuje payload z unikanym `request_id` i wysyła do skryptu w przeglądarce.
4. Wykonanie i odpowiedź: skrypt w przeglądarce robi fetch do LMArena i przesyła strumieniowo odpowiedzi z powrotem do serwera.
5. Przekazanie odpowiedzi: serwer mapuje fragmenty po `request_id` i strumieniowo odsyła je klientowi OpenAI.

## 📖 Endpointy API

- GET /v1/models — zwraca listę modeli zgodną z OpenAI (czytane z `models.json`).
- POST /v1/chat/completions — obsługa standardowych żądań chat, wspiera strumieniowanie i generowanie obrazów.
- (Generowanie obrazów zintegrowane) — wysyłając model obrazkowy do `/v1/chat/completions` otrzymasz wynik jako odpowiedź chat zawierającą Markdown z odnośnikami do obrazów.

Przykład żądania generowania obrazu:
```bash
curl http://127.0.0.1:5102/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "dall-e-3",
    "messages": [
      {
        "role": "user",
        "content": "A futuristic cityscape at sunset, neon lights, flying cars"
      }
    ],
    "n": 1
  }'
```

Przykładowa odpowiedź (format zgodny z chat):
```json
{
  "id": "img-as-chat-...",
  "object": "chat.completion",
  "created": 1677663338,
  "model": "dall-e-3",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "![A futuristic cityscape at sunset, neon lights, flying cars](https://...)"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": { ... }
}
```

## 📂 Struktura plików

```
.
├── .gitignore
├── api_server.py               # główny backend (FastAPI)
├── id_updater.py               # skrypt do przechwytywania ID sesji
├── model_updater.py            # skrypt aktualizujący listę modeli
├── models.json                 # mapowanie nazw modeli -> ID (wymagane)
├── available_models.json       # generowany plik referencyjny
├── model_endpoint_map.json     # zaawansowane mapowania modeli do sesji
├── requirements.txt
├── README.md                   # ten plik
├── config.jsonc                # konfiguracja globalna
├── modules/
│   ├── update_script.py
│   └── file_uploader.py
├── file_bed_server/
│   ├── main.py                 # serwer plików (FastAPI)
│   ├── requirements.txt
│   ├── .gitignore
│   └── uploads/                # katalog docelowy uploadów
└── TampermonkeyScript/
    └── LMArenaApiBridge.js     # skrypt uruchamiany w przeglądarce
```

Miłej pracy i swobodnego eksplorowania modeli na LMArena.ai! 💖