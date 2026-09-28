import ast
import importlib
import importlib.metadata
import inspect
from pathlib import Path
import re
import subprocess
import sys
import tempfile
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
IMPORT_LIST_RE = re.compile(r"\bimport\s+([A-Za-z_][\w.]*(?:\s+as\s+[A-Za-z_]\w*)?(?:\s*,\s*[A-Za-z_][\w.]*(?:\s+as\s+[A-Za-z_]\w*)?)*)", re.IGNORECASE)
FROM_IMPORT_RE = re.compile(r"\bfrom\s+(\.*[A-Za-z_][\w.]*)\s+import\b", re.IGNORECASE)
USING_MODULE_RE = re.compile(r"\busing\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b", re.IGNORECASE)
APP_ROOT = Path(__file__).resolve().parent
KNOWN_LIBRARIES = (
    ("google.genai", "google.genai", "google-genai"),
    ("python-dotenv", "dotenv", "python-dotenv"),
    ("fastapi", "fastapi", "fastapi"),
    ("pydantic", "pydantic", "pydantic"),
    ("uvicorn", "uvicorn", "uvicorn"),
    ("requests", "requests", "requests"),
    ("ollama", "ollama", "ollama"),
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
            "Create api_key.txt in the same folder as main.py and put your Gemini API key inside it."
        )

    api_key = key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError("api_key.txt is empty.")

    return api_key


def load_skill_prompt():
    skill_path = Path(__file__).with_name("skill.md")
    if skill_path.exists():
        return skill_path.read_text(encoding="utf-8").strip()
    return "You are a helpful assistant."


def _truncate(text, limit):
    cleaned = " ".join((text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def _safe_version(module_name, distribution_name=None):
    candidates = [distribution_name, module_name, module_name.replace("_", "-"), module_name.split(".", 1)[0]]
    for name in dict.fromkeys(candidate for candidate in candidates if candidate):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception as error:
            return f"unknown ({error})"
    return "unknown (distribution metadata not found)"


def _safe_signature(obj):
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return "(signature unavailable)"


def _introspect_library(module_name, distribution_name=None):
    version = _safe_version(module_name, distribution_name)
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        return f"- {module_name}: version {version}. Import failed ({type(error).__name__}: {error}). Do not invent APIs for this library."
    functions = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_") or not (inspect.isfunction(obj) or inspect.isbuiltin(obj)):
            continue
        functions.append(f"{name}{_safe_signature(obj)} - {_truncate(inspect.getdoc(obj) or '', MAX_FN_DOC_CHARS) or '(no docstring)'}")
        if len(functions) >= MAX_SIGNATURES:
            break
    docstring = _truncate(inspect.getdoc(module) or "", MAX_DOC_CHARS) or "(no module docstring)"
    return f"- {module_name}: version {version}. Docstring: {docstring} Public functions: {'; '.join(functions) or '(no public functions found)'}"


def _normalize_module_name(raw):
    name = re.split(r"\s+as\s+", (raw or "").strip().strip("`'\""), maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if name.startswith(".") or not VALID_MODULE_RE.match(name) or name in {"__main__", "__builtin__", "builtins"}:
        return None
    return name


def _local_module_names():
    names = []
    for module_path in APP_ROOT.glob("*.py"):
        if module_path.stem not in {"main", "main_2"}:
            names.append(module_path.stem)
    for package_init in APP_ROOT.glob("*/__init__.py"):
        names.append(package_init.parent.name)
    return sorted(set(names))


def _local_module_context(module_name):
    module_path = APP_ROOT / f"{module_name}.py"
    if not module_path.exists():
        package_init = APP_ROOT / module_name.replace(".", "/") / "__init__.py"
        module_path = package_init if package_init.exists() else None
    if module_path is None:
        return None

    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        return f"- Local module {module_name}: source inspection failed ({error})."

    functions = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
            continue
        try:
            signature = ast.unparse(ast.arguments(
                posonlyargs=node.args.posonlyargs,
                args=node.args.args,
                vararg=node.args.vararg,
                kwonlyargs=node.args.kwonlyargs,
                kw_defaults=node.args.kw_defaults,
                kwarg=node.args.kwarg,
                defaults=node.args.defaults,
            ))
        except Exception:
            signature = "(...)"
        functions.append(f"{node.name}({signature})")
    return f"- Local module {module_name} at {module_path.name}; public functions: {', '.join(functions) or '(none found)'}"


def inject_runtime_context(message):
    lowered = message.lower()
    seen = set()
    snapshots = []
    local_modules = _local_module_names()
    local_module_names_lower = {name.lower() for name in local_modules}
    distribution_by_module = {module_name: dist_name for _, module_name, dist_name in KNOWN_LIBRARIES}
    requested_modules = [part.strip() for match in IMPORT_LIST_RE.finditer(message) for part in match.group(1).split(",")]
    requested_modules.extend(match.group(1) for match in FROM_IMPORT_RE.finditer(message))
    requested_modules.extend(match.group(1) for match in USING_MODULE_RE.finditer(message))
    known_tokens = {token.lower() for token, _, _ in KNOWN_LIBRARIES}
    for raw_name in requested_modules:
        module_name = _normalize_module_name(raw_name)
        if not module_name or module_name in seen or (
            "." not in module_name
            and module_name.lower() not in known_tokens
            and module_name.lower() not in local_module_names_lower
        ):
            continue
        seen.add(module_name)
        local_context = _local_module_context(module_name)
        snapshots.append(local_context or _introspect_library(module_name, distribution_by_module.get(module_name)))
    for token, module_name, distribution_name in KNOWN_LIBRARIES:
        if module_name in seen or not re.search(r"(?<![\w.])" + re.escape(token.lower()) + r"(?![\w.])", lowered):
            continue
        seen.add(module_name)
        snapshots.append(_introspect_library(module_name, distribution_name))
    if local_modules:
        snapshots.insert(0, "Available local modules: " + ", ".join(local_modules))
    if not snapshots:
        return message
    return message + "\n\nSystem Context: Treat this runtime introspection as ground truth for APIs, versions, signatures, and docstrings.\n" + "\n".join(snapshots)


def verify_code_ast(response_text):
    errors = []
    for index, code in enumerate(PYTHON_FENCE_RE.findall(response_text or ""), start=1):
        try:
            ast.parse(code)
        except SyntaxError as error:
            errors.append(f"block {index} (line {error.lineno or 'unknown'}): {error}")
    return not errors, " | ".join(errors)


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


def extract_python_code(response_text):
    match = re.search(r"```(?:python|py)\s*\n(.*?)```", response_text, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def is_code_request(message):
    return bool(
        re.search(
            r"\b(?:write|provide|generate|create|show|give|build|make)\b"
            r"[\w\s-]{0,40}\b(?:code|program|script|function|class|snippet)\b",
            message,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:code|program|script|function|class|snippet)\b"
            r"[\w\s-]{0,40}\b(?:for|that|to)\b",
            message,
            re.IGNORECASE,
        )
    )


def run_python_verification(code):
    with tempfile.TemporaryDirectory(prefix="hackgrid-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        script_path = temporary_path / "deterministic_check.py"
        output_path = temporary_path / "python_output.txt"
        script_path.write_text(code, encoding="utf-8")

        with output_path.open("w", encoding="utf-8") as output_file:
            execution_environment = dict(__import__("os").environ)
            existing_python_path = execution_environment.get("PYTHONPATH", "")
            execution_environment["PYTHONPATH"] = (
                str(APP_ROOT)
                + (";" + existing_python_path if existing_python_path else "")
            )
            completed = subprocess.run(
                [sys.executable, str(script_path)],
                stdout=output_file,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=APP_ROOT,
                env=execution_environment,
                timeout=10,
            )

        output = output_path.read_text(encoding="utf-8").strip()
        if completed.returncode != 0:
            raise RuntimeError(output or "The Python verification program failed.")
        return output


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
                    break
                if attempts >= MAX_RETRIES:
                    return {"reply": "Code generation failed internal syntax checks."}
                attempts += 1
                outgoing = (
                    "Your provided code failed AST syntax validation with the following "
                    f"error: {ast_error}. Please fix and output the corrected code."
                )
        python_code = extract_python_code(reply_text)
        if python_code and not is_code_request(message):
            calculated_output = run_python_verification(python_code)
            return {"reply": f"Python calculated output- {calculated_output}"}
        return {"reply": reply_text}
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
                    "Google's Gemini servers are temporarily unavailable. "
                    "Please wait a moment and try again."
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