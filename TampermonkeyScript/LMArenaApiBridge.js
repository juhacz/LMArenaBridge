// ==UserScript==
// @name         LMArena API Bridge
// @namespace    http://tampermonkey.net/
// @version      2.5
// @description  Łączy LMArena z lokalnym serwerem API przez WebSocket w celu automatyzacji.
// @author       Lianues
// @match        https://lmarena.ai/*
// @match        https://*.lmarena.ai/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=lmarena.ai
// @grant        none
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // --- KONFIGURACJA ---
    const SERVER_URL = "ws://localhost:5102/ws"; // Powinno pasować do portu w api_server.py
    let socket;
    let isCaptureModeActive = false; // Flaga trybu przechwytywania ID

    // --- GŁÓWNA LOGIKA ---
    function connect() {
        console.log(`[API Bridge] Łączę z lokalnym serwerem: ${SERVER_URL}...`);
        socket = new WebSocket(SERVER_URL);

        socket.onopen = () => {
            console.log("[API Bridge] ✅ Połączenie WebSocket z lokalnym serwerem nawiązane.");
            document.title = "✅ " + document.title;
        };

        socket.onmessage = async (event) => {
            try {
                const message = JSON.parse(event.data);

                // Jeśli to polecenie, obsłuż je zamiast standardowego żądania chat
                if (message.command) {
                    console.log(`[API Bridge] ⬇️ Otrzymano polecenie: ${message.command}`);
                    if (message.command === 'refresh' || message.command === 'reconnect') {
                        console.log(`[API Bridge] Otrzymano polecenie '${message.command}' — odświeżam stronę...`);
                        location.reload();
                    } else if (message.command === 'activate_id_capture') {
                        console.log("[API Bridge] ✅ Tryb przechwytywania ID aktywowany. Wykonaj na stronie jednorazowo akcję 'Retry'.");
                        isCaptureModeActive = true;
                        // Opcjonalny wizualny wskaźnik dla użytkownika
                        document.title = "🎯 " + document.title;
                    } else if (message.command === 'send_page_source') {
                       console.log("[API Bridge] Otrzymano polecenie wysłania źródła strony — wysyłam...");
                       sendPageSource();
                    }
                    return;
                }

                const { request_id, payload } = message;

                if (!request_id || !payload) {
                    console.error("[API Bridge] Otrzymano od serwera nieprawidłową wiadomość:", message);
                    return;
                }
                
                console.log(`[API Bridge] ⬇️ Otrzymano żądanie czatu ${request_id.substring(0, 8)}. Przygotowuję fetch.`);
                await executeFetchAndStreamBack(request_id, payload);

            } catch (error) {
                console.error("[API Bridge] Błąd podczas przetwarzania wiadomości z serwera:", error);
            }
        };

        socket.onclose = () => {
            console.warn("[API Bridge] 🔌 Połączenie z lokalnym serwerem zostało zamknięte. Ponowne połączenie za 5 sekund...");
            if (document.title.startsWith("✅ ")) {
                document.title = document.title.substring(2);
            }
            setTimeout(connect, 5000);
        };

        socket.onerror = (error) => {
            console.error("[API Bridge] ❌ Błąd WebSocket:", error);
            socket.close(); // wywoła logikę ponownego łączenia w onclose
        };
    }

    async function executeFetchAndStreamBack(requestId, payload) {
        console.log(`[API Bridge] Bieżąca domena: ${window.location.hostname}`);
        const { is_image_request, message_templates, target_model_id, session_id, message_id } = payload;

        // --- Używamy session_id/message_id przekazanych z backendu ---
        if (!session_id || !message_id) {
            const errorMsg = "Otrzymane z backendu session_id lub message_id są puste. Uruchom skrypt `id_updater.py` i skonfiguruj ID.";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // Endpoint dla czatu i generowania obrazów
        const apiUrl = `/nextjs-api/stream/retry-evaluation-session-message/${session_id}/messages/${message_id}`;
        const httpMethod = 'PUT';
        
        console.log(`[API Bridge] Używany endpoint API: ${apiUrl}`);
        
        const newMessages = [];
        let lastMsgIdInChain = null;

        if (!message_templates || message_templates.length === 0) {
            const errorMsg = "Lista wiadomości otrzymana z backendu jest pusta.";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // Budujemy łańcuch wiadomości (działa zarówno dla czatu jak i generowania obrazów)
        for (let i = 0; i < message_templates.length; i++) {
            const template = message_templates[i];
            const currentMsgId = crypto.randomUUID();
            const parentIds = lastMsgIdInChain ? [lastMsgIdInChain] : [];
            
            // Dla zapytań obrazkowych status zawsze 'success', w przeciwnym razie ostatnia wiadomość 'pending'
            const status = is_image_request ? 'success' : ((i === message_templates.length - 1) ? 'pending' : 'success');

            newMessages.push({
                role: template.role,
                content: template.content,
                id: currentMsgId,
                evaluationId: null,
                evaluationSessionId: session_id,
                parentMessageIds: parentIds,
                experimental_attachments: Array.isArray(template.attachments) ? template.attachments : [],
                failureReason: null,
                metadata: null,
                participantPosition: template.participantPosition || "a",
                createdAt: new Date().toISOString(),
                updatedAt: new Date().toISOString(),
                status: status,
            });
            lastMsgIdInChain = currentMsgId;
        }

        const body = {
            messages: newMessages,
            modelId: target_model_id,
        };

        console.log("[API Bridge] Przygotowano ładunek do wysłania do LMArena API:", JSON.stringify(body, null, 2));

        // Flaga informująca interceptor fetch, że to żądanie wysłane przez ten skrypt
        window.isApiBridgeRequest = true;
        try {
            const response = await fetch(apiUrl, {
                method: httpMethod,
                headers: {
                    'Content-Type': 'text/plain;charset=UTF-8', // LMArena oczekuje text/plain
                    'Accept': '*/*',
                },
                body: JSON.stringify(body),
                credentials: 'include' // wymagane cookie
            });

            if (!response.ok || !response.body) {
                const errorBody = await response.text();
                throw new Error(`Nieprawidłowa odpowiedź sieci. Status: ${response.status}. Treść: ${errorBody}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                const { value, done } = await reader.read();
                if (done) {
                    console.log(`[API Bridge] ✅ Strumień dla żądania ${requestId.substring(0, 8)} zakończony pomyślnie.`);
                    // Tylko po poprawnym zakończeniu wysyłamy [DONE]
                    sendToServer(requestId, "[DONE]");
                    break;
                }
                const chunk = decoder.decode(value);
                // Przekazujemy surowy kawałek dalej do backendu
                sendToServer(requestId, chunk);
            }

        } catch (error) {
            console.error(`[API Bridge] ❌ Błąd podczas fetch dla żądania ${requestId.substring(0, 8)}:`, error);
            // Przy błędzie wysyłamy tylko informację o błędzie (bez [DONE])
            sendToServer(requestId, { error: error.message });
        } finally {
            // Reset flagi niezależnie od rezultatu
            window.isApiBridgeRequest = false;
        }
    }

    function sendToServer(requestId, data) {
        if (socket && socket.readyState === WebSocket.OPEN) {
            const message = {
                request_id: requestId,
                data: data
            };
            socket.send(JSON.stringify(message));
        } else {
            console.error("[API Bridge] Nie można wysłać danych — połączenie WebSocket nie jest otwarte.");
        }
    }

    // --- PRZERYWANIE ZAPYTAŃ (INTERCEPT) ---
    const originalFetch = window.fetch;
    window.fetch = function(...args) {
        const urlArg = args[0];
        let urlString = '';

        // Normalizujemy różne typy argumentów URL
        if (urlArg instanceof Request) {
            urlString = urlArg.url;
        } else if (urlArg instanceof URL) {
            urlString = urlArg.href;
        } else if (typeof urlArg === 'string') {
            urlString = urlArg;
        }

        // Dopasowujemy ścieżkę zawierającą sessionId/messageId
        if (urlString) {
            const match = urlString.match(/\/nextjs-api\/stream\/retry-evaluation-session-message\/([a-f0-9-]+)\/messages\/([a-f0-9-]+)/);

            // Jeśli żądanie nie jest wysłane przez ten skrypt i tryb przechwytywania jest aktywny — przechwytujemy ID
            if (match && !window.isApiBridgeRequest && isCaptureModeActive) {
                const sessionId = match[1];
                const messageId = match[2];
                console.log(`[API Bridge Interceptor] 🎯 W trybie przechwytywania wykryto ID! Wysyłam...`);

                // Wyłączamy tryb przechwytywania, aby wykonać to tylko raz
                isCaptureModeActive = false;
                if (document.title.startsWith("🎯 ")) {
                    document.title = document.title.substring(2);
                }

                // Asynchroniczne wysłanie przechwyconych ID do lokalnego serwera id_updater.py
                fetch('http://127.0.0.1:5103/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sessionId, messageId })
                })
                .then(response => {
                    if (!response.ok) throw new Error(`Serwer odpowiedział statusem: ${response.status}`);
                    console.log(`[API Bridge] ✅ ID zostały wysłane pomyślnie. Tryb przechwytywania zamknięty.`);
                })
                .catch(err => {
                    console.error('[API Bridge] Błąd podczas wysyłania aktualizacji ID:', err.message);
                    // Nawet przy błędzie tryb przechwytywania pozostaje wyłączony — brak ponawiania
                });
            }
        }

        // Wywołujemy oryginalny fetch, aby nie zaburzać działania strony
        return originalFetch.apply(this, args);
    };


    // --- Wysyłanie źródła strony ---
    async function sendPageSource() {
        try {
            const htmlContent = document.documentElement.outerHTML;
            await fetch('http://localhost:5102/internal/update_available_models', { // nowy endpoint
                method: 'POST',
                headers: {
                    'Content-Type': 'text/html; charset=utf-8'
                },
                body: htmlContent
            });
            console.log("[API Bridge] Źródło strony zostało pomyślnie wysłane.");
        } catch (e) {
            console.error("[API Bridge] Błąd wysyłania źródła strony:", e);
        }
    }

    // --- Start ---
    console.log("========================================");
    console.log("  LMArena API Bridge v2.5 działa.");
    console.log("  - Funkcja czatu połączona z ws://localhost:5102");
    console.log("  - Przechwytywanie ID będzie wysyłane na http://localhost:5103");
    console.log("========================================");
    
    connect(); // Nawiązanie połączenia WebSocket

})();
