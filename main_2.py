import ast
import importlib
import importlib.metadata
import importlib.util
import inspect
import re
import sys
from functools import lru_cache
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from google import genai
from google.genai import types


MAX_RETRIES = 2
MAX_DOC_CHARS = 800
MAX_FN_DOC_CHARS = 240
MAX_SIGNATURES = 40
PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
VALID_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
IMPORT_LIST_RE = re.compile(
    r"\bimport\s+"
    r"([A-Za-z_][\w.]*(?:\s+as\s+[A-Za-z_]\w*)?"
    r"(?:\s*,\s*[A-Za-z_][\w.]*(?:\s+as\s+[A-Za-z_]\w*)?)*)",
    re.IGNORECASE,
)
FROM_IMPORT_RE = re.compile(
    r"\bfrom\s+(\.*[A-Za-z_][\w.]*)\s+import\b",
    re.IGNORECASE,
)
USING_MODULE_RE = re.compile(
    r"\busing\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b",
    re.IGNORECASE,
)
APP_ROOT = Path(__file__).resolve().parent

# token -> (import module, distribution name for importlib.metadata)
KNOWN_LIBRARIES: tuple[tuple[str, str, str], ...] = (
    ("google.genai", "google.genai", "google-genai"),
    ("python-dotenv", "dotenv", "python-dotenv"),
    ("fastapi", "fastapi", "fastapi"),
    ("pydantic", "pydantic", "pydantic"),
    ("uvicorn", "uvicorn", "uvicorn"),
    ("requests", "requests", "requests"),
    ("ollama", "ollama", "ollama"),
    ("dotenv", "dotenv", "python-dotenv"),
    ("numpy", "numpy", "numpy"),
    ("pandas", "pandas", "pandas"),
    ("starlette", "starlette", "starlette"),
    ("httpx", "httpx", "httpx"),
    ("sqlalchemy", "sqlalchemy", "sqlalchemy"),
    ("pytest", "pytest", "pytest"),
    ("aiohttp", "aiohttp", "aiohttp"),
)


def load_api_key():
    key_file = Path(__file__).with_name("api_key.txt")
    if not key_file.exists():
        raise FileNotFoundError(
            "Create api_key.txt in the same folder as main_2.py and put your Gemini API key inside it."
        )

    api_key = key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError("api_key.txt is empty.")

    return api_key


@lru_cache(maxsize=1)
def load_skill_prompt():
    skill_path = Path(__file__).with_name("skill.md")
    if skill_path.exists():
        return skill_path.read_text(encoding="utf-8").strip()
    return "You are a helpful assistant."


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _ensure_local_import_path() -> None:
    root = str(APP_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _safe_version(module_name: str, distribution_name: str | None = None) -> str:
    candidates = []
    if distribution_name:
        candidates.append(distribution_name)
    candidates.append(module_name)
    candidates.append(module_name.replace("_", "-"))
    candidates.append(module_name.split(".", 1)[0])

    seen: set[str] = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception as error:
            return f"unknown ({error})"
    return "unknown (distribution metadata not found)"


def _safe_signature(obj) -> str:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        init = getattr(obj, "__init__", None)
        if init is not None:
            try:
                return str(inspect.signature(init))
            except (TypeError, ValueError):
                return "(signature unavailable)"
        return "(signature unavailable)"


def _is_public_function(name: str, obj) -> bool:
    if name.startswith("_"):
        return False
    return bool(
        inspect.isfunction(obj)
        or inspect.isbuiltin(obj)
        or inspect.iscoroutinefunction(obj)
        or inspect.isasyncgenfunction(obj)
        or inspect.isgeneratorfunction(obj)
    )


def _introspect_library(module_name: str, distribution_name: str | None = None) -> str:
    _ensure_local_import_path()
    version = _safe_version(module_name, distribution_name)
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        return (
            f"- {module_name}: version {version}. "
            f"Import failed ({type(error).__name__}: {error}). "
            "Do not invent APIs for this library."
        )

    docstring = inspect.getdoc(module) or getattr(module, "__doc__", None) or ""
    members: list[str] = []
    try:
        exported = inspect.getmembers(module)
    except Exception:
        exported = list(vars(module).items()) if hasattr(module, "__dict__") else []

    for name, obj in exported:
        if not _is_public_function(name, obj):
            continue
        fn_doc = _truncate(inspect.getdoc(obj) or "", MAX_FN_DOC_CHARS) or "(no docstring)"
        members.append(f"{name}{_safe_signature(obj)} — {fn_doc}")
        if len(members) >= MAX_SIGNATURES:
            break

    doc_part = _truncate(docstring, MAX_DOC_CHARS) or "(no module docstring)"
    fn_part = "; ".join(members) if members else "(no public functions found)"
    return (
        f"- {module_name}: version {version}. "
        f"Docstring: {doc_part} "
        f"Public functions: {fn_part}"
    )


def _normalize_module_name(raw: str) -> str | None:
    name = (raw or "").strip().strip("`'\"")
    name = re.split(r"\s+as\s+", name, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if name.startswith("."):
        return None
    if not VALID_MODULE_RE.match(name):
        return None
    if name in {"__main__", "__builtin__", "builtins"}:
        return None
    return name


def _modules_from_import_keywords(message: str) -> list[str]:
    found: list[str] = []

    for match in IMPORT_LIST_RE.finditer(message):
        for part in match.group(1).split(","):
            name = _normalize_module_name(part)
            if name:
                found.append(name)

    for match in FROM_IMPORT_RE.finditer(message):
        name = _normalize_module_name(match.group(1))
        if name:
            found.append(name)

    known_tokens = {
        token.lower() for token, module_name, _ in KNOWN_LIBRARIES
    } | {module_name.lower() for _, module_name, _ in KNOWN_LIBRARIES}

    for match in USING_MODULE_RE.finditer(message):
        name = _normalize_module_name(match.group(1))
        if not name:
            continue
        local_module = (APP_ROOT / f"{name}.py").exists() or (
            APP_ROOT / name.replace(".", "/") / "__init__.py"
        ).exists()
        if "." in name or name.lower() in known_tokens or local_module:
            found.append(name)

    return found


def inject_runtime_context(message: str) -> str:
    context_additions = []
    current_dir = Path(__file__).parent
    lowered = message.lower()
    seen: set[str] = set()
    snapshots: list[str] = []
    dist_by_module = {module_name: dist_name for _, module_name, dist_name in KNOWN_LIBRARIES}

    for py_file in current_dir.glob("*.py"):
        mod_name = py_file.stem
        if mod_name in ("main", "main_2"):
            continue

        if mod_name.lower() not in lowered:
            continue

        try:
            spec = importlib.util.spec_from_file_location(mod_name, py_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"Could not create import spec for {py_file}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)

            funcs = inspect.getmembers(module, inspect.isfunction)
            sig_text = []
            for name, func in funcs:
                sig = inspect.signature(func)
                doc = inspect.getdoc(func) or ""
                sig_text.append(f"Function `{name}{sig}`: {doc}")

            if sig_text:
                print(f"\n[DEBUG SUCCESS] Loaded local module '{mod_name}': {sig_text}\n")
                context_additions.append(
                    f"Verified Local Module `{mod_name}` Signatures:\n" + "\n".join(sig_text)
                )
            seen.add(mod_name)
        except Exception as e:
            print(f"Failed to inspect local file {py_file}: {e}")

    for module_name in _modules_from_import_keywords(message):
        if module_name in seen:
            continue
        seen.add(module_name)
        snapshots.append(_introspect_library(module_name, dist_by_module.get(module_name)))

    for token, module_name, distribution_name in KNOWN_LIBRARIES:
        if module_name in seen:
            continue
        pattern = r"(?<![\w.])" + re.escape(token.lower()) + r"(?![\w.])"
        if not re.search(pattern, lowered):
            continue
        seen.add(module_name)
        snapshots.append(_introspect_library(module_name, distribution_name))

    extra_parts = []
    if context_additions:
        extra_parts.append(
            "\n\n[SYSTEM INJECTED LOCAL RUNTIME CONTEXT]:\n" + "\n".join(context_additions)
        )
    if snapshots:
        extra_parts.append(
            "\n\nSystem Context: The user currently has the following local modules "
            f"detected from their prompt ({', '.join(sorted(seen))}). "
            "Treat this live runtime introspection as ground truth for APIs, versions, "
            "signatures, and docstrings. Do not invent symbols that are absent here.\n"
            + "\n".join(snapshots)
        )

    if extra_parts:
        return message + "".join(extra_parts)
    return message


def verify_code_ast(response_text: str) -> tuple[bool, str]:
    blocks = PYTHON_FENCE_RE.findall(response_text or "")
    if not blocks:
        return True, ""

    errors: list[str] = []
    for index, code in enumerate(blocks, start=1):
        try:
            ast.parse(code)
        except SyntaxError as error:
            location = f"line {error.lineno}" if error.lineno else "unknown line"
            errors.append(f"block {index} ({location}): {error}")
        except Exception as error:
            errors.append(f"block {index}: {type(error).__name__}: {error}")

    if errors:
        return False, " | ".join(errors)
    return True, ""


class ChatRequest(BaseModel):
    message: str


app = FastAPI(title="Gemini Desk")
chat_lock = Lock()
gemini_client = None
gemini_chat = None


def get_chat():
    global gemini_client, gemini_chat
    if gemini_chat is None:
        gemini_client = genai.Client(api_key=load_api_key())
        gemini_chat = gemini_client.chats.create(
            model="gemini-3.6-flash",
            config=types.GenerateContentConfig(
                system_instruction=load_skill_prompt()
            ),
        )
    return gemini_chat


@app.get("/")
def home():
    return FileResponse(Path(__file__).with_name("static").joinpath("index.html"))


@app.post("/api/chat")
def send_message(request: ChatRequest):
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    try:
        with chat_lock:
            outgoing = inject_runtime_context(message)
            attempts = 0
            while True:
                response = get_chat().send_message(outgoing)
                reply_text = response.text or ""
                is_valid, ast_error = verify_code_ast(reply_text)
                if is_valid:
                    return {"reply": reply_text}
                if attempts >= MAX_RETRIES:
                    break
                attempts += 1
                outgoing = (
                    "Your provided code failed AST syntax validation with the following "
                    f"error: {ast_error}. Please fix and output the corrected code."
                )
            return {"reply": "Code generation failed internal syntax checks."}
    except Exception as error:
        error_text = str(error)
        if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
            raise HTTPException(
                status_code=429,
                detail=(
                    "Gemini quota is temporarily exhausted. "
                    "Please wait before trying again, or check your Google AI "
                    "plan and billing details."
                ),
            ) from error
        if "503" in error_text or "UNAVAILABLE" in error_text:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Google's Gemini servers are currently experiencing high demand "
                    "and are temporarily unavailable. Please wait a moment and try again."
                ),
            ) from error
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/reset")
def reset_chat():
    global gemini_client, gemini_chat
    with chat_lock:
        gemini_client = None
        gemini_chat = None
    return {"status": "reset"}


def chat_with_gemini():
    api_key = load_api_key()
    client = genai.Client(api_key=api_key)

    skill_prompt = load_skill_prompt()
    chat = client.chats.create(
        model="gemini-3.6-flash",
        config=types.GenerateContentConfig(
            system_instruction=skill_prompt
        ),
    )

    print("Chat with Gemini")
    print("Type 'q' to quit.")

    while True:
        user_input = input("\nYou: ").strip()

        if user_input.lower() == "q":
            print("Goodbye!")
            break

        if not user_input:
            continue

        outgoing = inject_runtime_context(user_input)
        attempts = 0
        while True:
            response = chat.send_message(outgoing)
            reply_text = response.text or ""
            is_valid, ast_error = verify_code_ast(reply_text)
            if is_valid:
                print(f"\nGemini: {reply_text}")
                break
            if attempts >= MAX_RETRIES:
                print("\nGemini: Code generation failed internal syntax checks.")
                break
            attempts += 1
            outgoing = (
                "Your provided code failed AST syntax validation with the following "
                f"error: {ast_error}. Please fix and output the corrected code."
            )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
