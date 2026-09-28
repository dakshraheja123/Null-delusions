# Gemini Desk

Gemini Desk is a small local chat application built with FastAPI and a browser-based chat interface. It sends user messages to Google's Gemini API, displays the response, and can run Python snippets returned for deterministic questions.

## Features

- Browser chat served locally at `http://127.0.0.1:8000`.
- Gemini conversations through the Google Gen AI Python SDK, using the `gemini-3.6-flash` model.
- A system prompt loaded from `SKILL.md` on Windows (the code looks for `skill.md`; Windows file paths are case-insensitive). On case-sensitive systems, rename the file to `skill.md`. If the prompt file is unavailable, the server uses a generic assistant instruction.
- Runtime context for library questions: the app can inspect selected installed Python libraries and local Python modules, including versions, public function signatures, and docstrings, and append that information to the prompt.
- Fenced code blocks labeled `python` in model responses are syntax-checked with Python's `ast` module. Invalid responses are retried up to two times. The `main.py` code extractor also recognizes `py` fences for execution, although those fences are not covered by this AST check.
- In `main.py`, a Python code block returned for a message that does not appear to request code is executed and its output is returned instead of the code. Execution has a 10-second timeout and runs in a temporary directory.
- Light/dark theme toggle, remembered in browser local storage; “New chat” resets the server-side conversation.
- Clear API errors for empty messages, exhausted Gemini quota, and temporary Gemini unavailability.

## How It Works

1. `start.bat` switches to the project directory and checks whether a server is already responding on port 8000.
2. If not, it installs the packages in `requirements.txt` and starts `main.py` with Python.
3. FastAPI serves `static/index.html` at `/`. The page submits chat messages to `/api/chat` using `fetch` and JSON.
4. The API validates the message, adds relevant runtime library/module details when it recognizes them, and sends the result to Gemini with the configured system prompt.
5. The server checks `python`-labeled fenced blocks in the response for syntax. In `main.py`, extracted `python` or `py` code may also be executed for a message not recognized as a code request; exceptions and timeouts are returned as HTTP errors.
6. The browser displays the assistant reply or the API's error message in the conversation.

The API key and chat client are loaded lazily when the first message is sent. The chat history is held in memory by the Gemini chat session and is reset when the server restarts or `/api/reset` is called. A lock serializes chat/reset operations in this single-process app.

## Requirements

- Windows for the supplied `start.bat` launcher.
- Python 3.10 or newer is recommended. `main_2.py` uses modern built-in generic and union type syntax, which requires Python 3.10+.
- A Gemini API key with access to the configured model.
- Internet access for Gemini requests. The browser loads its fonts from Google Fonts when available.

## Setup and Run

1. Install Python and ensure `python` and `pip` are available in PowerShell or Command Prompt.
2. Create a Gemini API key in Google AI Studio.
3. Create `api_key.txt` beside `main.py` and put only the key in the file. Do not commit or share this file; it is excluded by `.gitignore`.
4. Double-click `start.bat`, or run the following from the project directory:

   ```powershell
   python -m pip install -r requirements.txt
   python main.py
   ```

5. Open `http://127.0.0.1:8000` in a browser.

The launcher automatically installs dependencies each time it starts a new server. To stop the server, close its console window or press `Ctrl+C` in that console.

## Web API

### `GET /`

Returns the single-page chat UI from `static/index.html`.

### `POST /api/chat`

Request JSON:

```json
{
  "message": "What does this project do?"
}
```

Successful response JSON:

```json
{
  "reply": "The assistant's response"
}
```

The `message` value must be a non-empty string. An empty or whitespace-only message returns HTTP 400. A Gemini quota/rate-limit error is mapped to HTTP 429; an upstream unavailable error is mapped to HTTP 503; other exceptions are generally returned as HTTP 500.

### `POST /api/reset`

Takes no request body. Clears the in-memory Gemini client/chat so the next message creates a fresh chat. Returns:

```json
{
  "status": "reset"
}
```

The browser sends a new chat request when Enter is pressed. Shift+Enter inserts a newline. On an API error, the browser displays the response's `detail` text.

## Libraries and APIs

### Application dependencies

Declared in `requirements.txt`:

| Package | Role in this project |
| --- | --- |
| `fastapi` | Defines the web application and HTTP routes. |
| `uvicorn` | Runs the FastAPI application when `main.py` is launched directly. |
| `google-genai` | Creates the Gemini client, chat session, and generation configuration. |
| `pydantic` | Validates the incoming chat request model. |
| `ollama`, `python-dotenv`, `requests` | Listed as install requirements, but not directly used by the current server implementation. |

The implementation also uses Python standard-library modules for AST parsing, module loading and introspection, regular expressions, subprocess execution, temporary files, paths, and thread locking.

### External APIs

- Google Gemini API, accessed through `google-genai` with `genai.Client`, `client.chats.create`, and `chat.send_message`.
- The local HTTP API listed above, served by FastAPI/Uvicorn.
- Google Fonts is referenced in the page's CSS; the app remains usable if those fonts cannot load.

There is no browser-side direct call to Gemini: the browser calls the local FastAPI server, which keeps the API key on the server side.

## Project Files

| File | Purpose |
| --- | --- |
| `main.py` | Primary web server and chat implementation; launched by `start.bat`. |
| `main_2.py` | Alternate version of the server. It has a similar chat API and runtime inspection, but does not execute generated Python snippets in `/api/chat`. |
| `static/index.html` | Chat UI, styling, theme persistence, and browser-side API calls. |
| `SKILL.md` | System prompt/behavior instructions loaded by the app on Windows. |
| `api_key.txt` | Local Gemini key; create this file yourself. It is ignored by Git. |
| `requirements.txt` | Python package requirements installed by the launcher. |
| `start.bat` | Windows startup script. |
| `hi.py`, `test.py` | Small standalone example files; they are not used to start the web application. |

## Important Safety Note

`main.py` executes Python code generated by the model for some non-code questions. The temporary directory and 10-second timeout do not make this a sandbox: executed code may still access files, environment variables, or the network with the same permissions as the app process. Only run this app with prompts and model output you trust. Do not expose the local server to an untrusted network without adding authentication and stronger process isolation.

## Troubleshooting

- **HTTP 503 from `/api/chat`:** the app received a Gemini `503`/`UNAVAILABLE` response. Retry later and check Gemini service/model availability and API access.
- **HTTP 429 from `/api/chat`:** Gemini quota or rate limits were reached. Check the API project's quota/billing and wait before retrying.
- **HTTP 500 on the first message:** confirm that `api_key.txt` exists beside `main.py`, is not empty, and contains a valid key. The key is read when the first chat session is created.
- **Port 8000 is already in use:** `start.bat` assumes anything responding at `127.0.0.1:8000` is the existing app. Stop the other process or choose a different port in the Uvicorn launch configuration.
- **`GET /favicon.ico` returns 404:** the app does not currently include a favicon. This does not affect chat functionality.