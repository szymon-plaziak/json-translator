# Jak działa robotctl — kompletna explikacja

## 1. Idea nadrzędna: aplikacja to tłumacz z ludzkiego na robotowy

Cała aplikacja robi jedną rzecz: zamienia **ludzki język** na **ścisłą komendę JSON** i bezpiecznie dostarcza ją do robota:

```
Człowiek:  "podnieś hak pół metra, potem obróć o 90 stopni"
                │
                ▼
     ┌──────────────────┐
     │  LLM (model AI)  │  ← "tłumacz przysięgły", ale NIEZAUFAŁYWANY
     └──────────────────┘
                │  proponuje JSON:
                │  {"version":1, "mode":"plan", "steps":[
                │     {"action":"lift","meters":0.5},
                │     {"action":"slew","degrees":90}]}
                ▼
     ┌──────────────────┐
     │ Warstwa kontroli │  ← deterministyczny kod, NIE AI: sprawdza wszystko
     └──────────────────┘
                │  wysyła KROK PO KROKU
                ▼
     ┌──────────────────┐
     │      Robot       │  przez zwykłe żądania HTTP
     └──────────────────┘
```

Kluczowa zasada projektowa: **LLM tylko proponuje, nigdy nie decyduje**. Model AI jest świetny w tłumaczeniu języka, ale bywa nieprzewidywalny — dlatego wszystko, co powiedzie, przechodzi przez rygorystyczną weryfikację zanim cokolwiek dotknie robota. Awaryjny stop (`POST /stop`) w ogóle nie przechodzi przez LLM.

---

## 2. Problem uniwersalności i jego rozwiązanie: profil robota

Żuraw wieżowy mówi "podnieś/opuszczaj/obracaj". Robot jeżdżący mówi "jedź/skręć". Ramię fabryczne mówi "chwyć/spawaj". Aplikacja **nie ma wbudowanego żadnego z tych języków** — byłoby to zaprzeczeniem uniwersalności.

Zamiast tego cała wiedza o konkretnym robocie żyje w **profilu** — pliku JSON, który **Ty wgrywasz** przez `POST /profile`:

```json
{
  "profile_version": 1,
  "robot": { "name": "Crane-9", "description": "żuraw warsztatowy" },
  "actions": [
    {
      "action": "lift",
      "description": "podnosi hak",
      "params": [
        { "name": "meters",    "type": "float", "unit": "m",  "min": 0, "max": 5, "required": true },
        { "name": "speed_mps", "type": "float", "unit": "m/s","min": 0, "max": 1, "default": 0.2 }
      ]
    }
  ],
  "examples": [
    { "version": 1, "mode": "plan",
      "steps": [ {"action": "slew", "degrees": 90}, {"action": "lower", "meters": 2.0} ] }
  ]
}
```

Profil mówi trzy rzeczy:

1. **Słownik akcji** — jakie komendy robot rozumie i jakie parametry przyjmują (z typami i twardymi limitami min/max),
2. **Przykłady odpowiedzi** — 1–5 wzorców *dokładnie poprawnego* JSON-a. To jest ten "przykładowy poprawny plik JSON": profil uczy LLM, jak ma odpowiadać **dla TEGO robota**,
3. **Limitы bezpieczeństwa** — np. "podnieś maksymalnie 5 m".

Wgrałeś profil żurawia → aplikacja planuje `lift`/`slew`. Wgrasz profil łazika → planuje `move`/`turn`. **Rdzeń aplikacji nie zmienia się ani o linię.** Sprawdziłem to na żywo: po wgraniu profilu żurawia komenda *"podnieś hak pół metra, potem obróć o 90 stopni"* dała plan `[{"action":"lift","meters":0.5},{"action":"slew","degrees":90}]` — model użył słownictwa z profilu, nie swoich domyślnych.

Przy wgraniu profil jest **weryfikowany**: przykłady muszą być poprawnymi odpowiedziami, parametry muszą mieć sensowne typy i zakresy (min ≤ max, `required` nie może mieć `default` itd.). Zły profil → odrzucenie z konkretnym komunikatem. Poprawny → zapisywany na dysk (`PROFILE_PATH`), przeżywa restart, działa od razu bez przeładowania (hot-apply).

---

## 3. Jak LLM "wie", jak odpowiadać

Gdy przychodzi komenda w języku naturalnym, aplikacja buduje **prompt systemowy** z trzech składników:

- **uniwersalna koperta odpowiedzi** (stała, wbudowana):
  ```json
  {"version": 1, "mode": "action|plan|clarification", "steps": [...], "question": "...", "options": [...]}
  ```
  - `mode: "action"` — dokładnie jeden krok,
  - `mode: "plan"` — 2–8 uporządkowanych kroków,
  - `mode: "clarification"` — model pyta (np. "w lewo czy w prawo?") zamiast zgadywać. Istnieje zasada *"split, don't ask"*: jeśli żądanie przekracza limity jednego kroku, model ma je **podzielić na plan**, a nie dopytywać,
- **sekcja profilu**: nazwa robota, słownik akcji z limitami ("używaj WYŁĄCZNIE tych akcji") oraz **Twoje przykłady wklejone dosłownie** ("naśladuj ten format"),
- **aktualna telemetria** (z robota): pozycja, bateria, status — model ma podstawować decyzje tylko na realnych danych, nigdy niczego nie wymyślać.

Dodatkowo aplikacja wysyła do modelu **JSON Schema wygenerowaną dynamicznie z profilu** (tzw. structured output) — serwer modelu jest *wymuszany* formatem, więc szansa na złą odpowiedź spada niemal do zera. Jeśli serwer nie obsługuje tej funkcji, jest automatyczny fallback: `json_schema` → `json_object` → sam prompt.

---

## 4. Obwód bezpieczeństwa — 4 niezależne warstwy

LLM nigdy nie trafia do robota bezpośrednio. Każda odpowiedź przechodzi:

| Warstwa | Co sprawdza | Co się dzieje przy błędzie |
|---|---|---|
| **1. Parser** | Czy odpowiedź to w ogóle JSON? Czy ma poprawną kopertę (wersja, tryb, liczba kroków)? | Automatyczna prośba o korektę (do 2 prób na format) |
| **2. Walidacja profilu** (przy parsowaniu) | Czy akcje istnieją w profilu? Czy parametry są w zakresach? | Retry z komunikatem "ta akcja nie istnieje dla tego robota, użyj: …" |
| **3. Warstwa bezpieczeństwa** (deterministyczna, bez AI) | To samo jeszcze raz + nieznane parametry + akcje z `RISKY_ACTIONS` wymagają `"confirm": true` | Komenda `rejected` — do robota nie wychodzi nic |
| **4. Wykonawca** | Świeża telemetria między krokami, timeout na każdy krok, odczyt od robota | Bezpieczny stop + status `failed` |

I niezależnie od tego wszystkiego: **`POST /stop` przerywa wszystko natychmiast**, omijając LLM.

Efekt na przykładzie: gdy `RISKY_ACTIONS=` jest puste, nic nie wymaga potwierdzenia. Jeśli ustawisz `RISKY_ACTIONS=lift`, to każda komenda zawierająca "lift" dostanie status `rejected`, chyba że aplikacja-klient wyśle `"confirm": true` świadomie.

---

## 5. Jak aplikacja rozmawia z robotem — most HTTP

To jest sedno sprawy. Aplikacja **nie wie, czym fizycznie jest robot**. Oczekuje tylko, że robot (a właściwie jego firmware albo mała aplikacja towarzysząca — na Raspberry Pi, ESP32, komputerze przemysłowym, czymkolwiek) wystawi **trzy endpointy HTTP**:

### a) `GET /telemetry` — robot melduje stan (polling)

Aplikacja odpytuje go 5× na sekundę (`ROBOT_POLL_HZ`). Robot odpowiada np.:

```json
{ "motion_status": "moving", "pose": {"x_m": 1.2, "y_m": 0, "heading_deg": 45},
  "battery_pct": 55, "obstacle_distance_m": 0.8 }
```

Wymagane jest tylko `motion_status` (`idle|moving|turning|stopped|error`) — reszta jest opcjonalna, bo nie każdy robot ma GPS czy czujniki. Nieznane wartości mapują się na `error`. Te dane napędzają: `GET /state` dla Twojej aplikacji, prompt LLM ("telemetria only, niczego nie wymyślaj") i **kontrolę świeżości** — jeśli robot przestaje odpowiadać na ~3 odpytania z rzędu, aplikacja uznaje łączę za zerwaną i przerywa wykonywany plan (bezpieczny stop).

### b) `POST /command` — aplikacja wysyła JEDEN krok

```json
{ "action": "lift", "meters": 0.5, "speed_mps": 0.2 }
```

To jest **dokładnie ten format JSON, którego LLM nauczył się z profilu** — dzięki temu przepływ jest prosty: profil definiuje format → LLM odpowiada w tym formacie → warstwa bezpieczeństwa go waliduje → wykonawca przesyła krok *dosłownie* do robota. Robot wykonuje i **dopiero wtedy odpowiada**: HTTP 200 = sukces, 4xx/5xx = porażka, a nawet 200 z treścią `{"ok": false, "error": "silnik przegrzany"}` traktowany jest jak porażka (komunikat trafia do statusu komendy).

### c) `POST /stop` — awaryjny stop

Wysłany za każdym razem, gdy coś idzie nie tak (timeout kroku, e-stop, porażka kroku, przerwanie). Best-effort — nawet jeśli robot nie odpowiada, aplikacja nigdy nie zawisa.

**Dlaczego HTTP?** Bo to najprostszy możliwy uniwersalny "gniazdek": nie ma znaczenia, czy robotem jest Raspberry Pi z Pythonem, ESP32, czy przemysłowy sterownik PLC z opakowaniem REST. Twoja aplikacja-robotowa po prostu obsługuje trzy adresy URL. **Cały protokół komunikacji z robotem to te 3 endpointy i 2 dokumenty JSON** (telemetria, komenda) — wszystkie opisane w `API_README.md`.

---

## 6. Cykl życia jednej komendy (end-to-end)

```
Twoja aplikacja                robotctl (127.0.0.1:8081)                robot
────────────────               ────────────────────────────             ──────
POST /commands            ───►  status: "planning"
{"command":"podnieś hak"}         │ LLM planuje (profil w promptie)
                                  ▼
                                status: "executing"
                                  │ krok 1: walidacja profilu ✓
                                  ├──────────────────────────────►  POST /command
                                  │                                 {"action":"lift","meters":0.5}
                                  │  ◄─────────────────────────────  200 OK (po wykonaniu)
                                  │ krok 2: telemetria świeża? ✓
                                  ├──────────────────────────────►  POST /command
GET /commands/{id}          ◄───  status: "completed"
(cyklicznie, aż status końcowy)
```

Statusy końcowe: `completed` / `failed` (błąd kroku lub telemetrii) / `rejected` (warstwa bezpieczeństwa) / `stopped` (e-stop) / `clarification` (model pyta — odpowiedź wysyłasz jako NOWĄ komendę, a kontekst wcześniejszego pytania jest przenoszony automatycznie). Tylko jedna komenda naraz — druga dostaje 409.

---

## 7. Technologie i zależności

| Technologia | Rola |
|---|---|
| **Python 3.12 + asyncio** | Cała aplikacja asynchroniczna, jedna pętla zdarzeń (serwer + robot + LLM) |
| **uv** | Menedżer pakietów/środowiska (`uv sync`, `uv run robotctl`) — nigdy pip |
| **FastAPI** | Framework REST API (walidacja ciała żądań, routing) |
| **uvicorn** | Serwer ASGI obsługujący HTTP; przejmuje sygnały Ctrl+C/SIGTERM → bezpieczne zamknięcie |
| **pydantic / pydantic-settings** | Schemy kontraktów (koperta odpowiedzi, profil, telemetria — wszystko `extra="forbid"`, nic nadmiarowego nie przechodzi) + konfiguracja z `.env` |
| **openai (AsyncOpenAI)** | Klient do **dowolnego** endpointu zgodnego z OpenAI — vLLM, Ollama, LM Studio… |
| **httpx** | Asynchroniczny klient HTTP do rozmowy z robotem (+ wstrzykiwany MockTransport w testach) |
| **pytest** | 137 testów bez żadnego sprzętu (fałszywy adapter, fałszywy robot HTTP) |

Brak bazy danych — stan komend w pamięci, log interakcji z LLM jako JSONL (`logs/llm.jsonl`, widoczny przez `GET /logs`). Całość nasłuchuje **tylko na 127.0.0.1** (loopback) — z zewnątrz nieosiągalne.

Struktura kodu (`src/robotctl/`): `core.py` (ustawienia, uniwersalna koperta, profil robota z walidacją, generowanie promptu i schemy, bramka bezpieczeństwa) → `runtime.py` (most HTTP do robota, telemetria, rozmowa z LLM, wykonanie krok po kroku) → `service.py` (endpointy REST + start serwisu).

---

## 8. Co się dzieje w skrajnych przypadkach

- **Brak profilu**: `POST /commands` → 422 "wgraj profil przez POST /profile". Serwis działa.
- **Brak robota** (`ROBOT_BASE_URL` puste): serwis startuje normalnie, `/health` → `ok:false`, planowanie działa, wykonanie **bezpiecznie zawodzi** ("telemetry stale or robot disconnected").
- **Robot pada w trakcie**: telemetria przestaje być świeża → plan przerywany → wysyłany stop → status `failed`.
- **LLM kłamie** (wymyśla akcję "teleportuj"): parser to wyłapuje → automatyczny retry z korektą → jeśli dalej źle → `failed`, robot nietknięty.
- **LLM każe skoczyć 20 m przy limicie 5 m**: odrzucenie albo (zwykle) model sam dzieli na plan zgodnie z zasadą "split, don't ask".

---

**Jednym zdaniem:** to headlessowa usługa REST, która traktuje LLM jako tłumacza zbudowanego na Twoim własnym słowniku robota (profil JSON), a każdy jego wyrok weryfikuje deterministycznym kodem, po czym dostarcza kroki do dowolnego robota, który wystawi trzy proste endpointy HTTP — uniwersalność bierze się z tego, że rdzeń nie zna żadnego robota, a całą "wiedzę o robocie" dostaje w danych, nie w kodzie.
