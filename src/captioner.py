"""
captioner — toolkit-agnostic auto-captioning backend.

Captioning runs in a llama.cpp ``llama-server`` subprocess that this module
starts, keeps loaded between requests, and stops. The app talks to it over
its OpenAI-compatible HTTP API with the standard library only, so the app
gains no ML dependencies and a model crash cannot take the UI down.

Everything lives in a ``captioning/`` directory next to the config file:

    captioning/
      llama.cpp/llama-server        prebuilt llama.cpp binaries (+ shared libs)
      models/<Display Name>/        one directory per model:
          *.gguf                      the language model (exactly one)
          *mmproj*.gguf               the vision projector (exactly one)
          model.ini                   optional: [model] max_pixels = N
      prompts/<name>.txt            saved prompt presets

No Qt imports. ``LlamaServer`` is not thread-safe: all calls except
``shutdown`` must come from one thread (the app's caption worker).
"""
from __future__ import annotations

import base64
import collections
import configparser
import io
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

CAPTIONING_DIRNAME = "captioning"
SERVER_BIN_RELPATH = os.path.join("llama.cpp", "llama-server")
PROMPT_EXT = ".txt"

# Upper bounds the UI exposes; the server is started with room for both.
MAX_IMAGE_PIXELS = 4_000_000
MAX_NEW_TOKENS = 2048
# Qwen-VL family: 16px patches merged 2x2 -> one vision token per 32x32 px.
_PIXELS_PER_IMAGE_TOKEN = 32 * 32
_CTX_SIZE = 8192

_LOAD_TIMEOUT_S = 300.0
_REQUEST_TIMEOUT_S = 600.0


class CaptionerError(Exception):
    """Base error for the captioning backend."""


class ServerError(CaptionerError):
    """The server could not be started or stopped responding. Aborts a run."""


class ImageError(CaptionerError):
    """One image could not be read. The run continues with the next image."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model_path: str
    mmproj_path: str
    max_pixels: Optional[int] = None   # per-model default for the UI


@dataclass(frozen=True)
class CaptionParams:
    max_tokens: int = 768
    temperature: float = 0.0
    max_pixels: int = 1_000_000


# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------

def captioning_dir(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), CAPTIONING_DIRNAME)


def server_bin(cap_dir: str) -> str:
    return os.path.join(cap_dir, SERVER_BIN_RELPATH)


def prompts_dir(cap_dir: str) -> str:
    return os.path.join(cap_dir, "prompts")


def discover_models(cap_dir: str) -> tuple[list[ModelSpec], list[str]]:
    """Scan ``models/``: one ModelSpec per subdirectory that contains GGUF files.

    Returns (specs, problems). Directories without any GGUF are ignored. A
    directory that is malformed (not exactly one model and one mmproj file,
    bad model.ini) is excluded and described in ``problems`` so the caller can
    report it — one bad directory (e.g. a half-finished download) must not
    hide the valid models.
    """
    models_root = os.path.join(cap_dir, "models")
    if not os.path.isdir(models_root):
        return [], []
    specs: list[ModelSpec] = []
    problems: list[str] = []
    for name in sorted(os.listdir(models_root), key=str.lower):
        d = os.path.join(models_root, name)
        if not os.path.isdir(d) or name.startswith("."):
            continue
        ggufs = sorted(f for f in os.listdir(d) if f.lower().endswith(".gguf"))
        if not ggufs:
            continue
        mmproj = [f for f in ggufs if "mmproj" in f.lower()]
        model = [f for f in ggufs if "mmproj" not in f.lower()]
        if len(model) != 1 or len(mmproj) != 1:
            problems.append(
                f"Model directory {d!r} must contain exactly one model .gguf and "
                f"one *mmproj*.gguf; found models={model}, mmproj={mmproj}"
            )
            continue
        try:
            max_pixels = _read_model_ini(os.path.join(d, "model.ini"))
        except CaptionerError as e:
            problems.append(str(e))
            continue
        specs.append(ModelSpec(
            name=name,
            model_path=os.path.join(d, model[0]),
            mmproj_path=os.path.join(d, mmproj[0]),
            max_pixels=max_pixels,
        ))
    return specs, problems


def _read_model_ini(path: str) -> Optional[int]:
    if not os.path.exists(path):
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
        return parser.getint("model", "max_pixels", fallback=None)
    except (configparser.Error, ValueError) as e:
        raise CaptionerError(f"Invalid {path!r}: {e}") from e


# ---------------------------------------------------------------------------
# Prompt presets (one UTF-8 text file per preset)
# ---------------------------------------------------------------------------

def validate_prompt_name(name: str) -> str:
    """Return the stripped name, or raise ValueError if unusable as a filename."""
    name = name.strip()
    if not name:
        raise ValueError("Prompt name cannot be empty")
    if name.startswith(".") or any(c in name for c in '/\\\0'):
        raise ValueError("Prompt name cannot start with '.' or contain / or \\")
    return name


def _prompt_path(pdir: str, name: str) -> str:
    return os.path.join(pdir, validate_prompt_name(name) + PROMPT_EXT)


def list_prompts(pdir: str) -> dict[str, str]:
    """{name: text} for every preset, sorted case-insensitively by name."""
    if not os.path.isdir(pdir):
        return {}
    names = sorted(
        (f[: -len(PROMPT_EXT)] for f in os.listdir(pdir)
         if f.endswith(PROMPT_EXT) and not f.startswith(".")),
        key=str.lower,
    )
    return {n: read_prompt(pdir, n) for n in names}


def read_prompt(pdir: str, name: str) -> str:
    with open(_prompt_path(pdir, name), encoding="utf-8") as f:
        return f.read().strip()


def save_prompt(pdir: str, name: str, text: str) -> None:
    path = _prompt_path(pdir, name)
    os.makedirs(pdir, exist_ok=True)
    staging = path + ".imgdb-tmp"
    with open(staging, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    os.replace(staging, path)


def rename_prompt(pdir: str, old: str, new: str) -> None:
    src, dst = _prompt_path(pdir, old), _prompt_path(pdir, new)
    if src == dst:
        return
    # Case-only renames are the same file on case-insensitive filesystems.
    if os.path.exists(dst) and os.path.normcase(src) != os.path.normcase(dst):
        raise ValueError(f"A prompt named {new.strip()!r} already exists")
    os.rename(src, dst)


def delete_prompt(pdir: str, name: str) -> None:
    os.remove(_prompt_path(pdir, name))


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------

def encode_image(path: str, max_pixels: int) -> str:
    """Decode with Pillow (any format it supports, incl. AVIF), apply EXIF
    rotation, downscale to at most ``max_pixels``, return a PNG data URL.

    Resizing client-side keeps max_pixels a per-request setting instead of a
    server start flag.
    """
    try:
        with Image.open(path) as im:
            img = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:  # noqa: BLE001 — any decode failure is per-image
        raise ImageError(f"Cannot read image {path!r}: {e}") from e
    w, h = img.size
    if w * h > max_pixels:
        scale = (max_pixels / (w * h)) ** 0.5
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# Server process
# ---------------------------------------------------------------------------

class LlamaServer:
    """A llama-server subprocess holding at most one model."""

    def __init__(self, server_path: str) -> None:
        self._server_path = server_path
        self._proc: Optional[subprocess.Popen] = None
        self._drain_thread: Optional[threading.Thread] = None
        self._spec: Optional[ModelSpec] = None
        self._base = ""
        self._log: collections.deque[str] = collections.deque(maxlen=40)

    @property
    def loaded_model(self) -> Optional[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._spec.name if self._spec else None
        return None

    def load(self, spec: ModelSpec) -> None:
        """Start the server with ``spec`` unless it is already serving it."""
        if self.loaded_model == spec.name and self._spec == spec:
            return
        self.unload()
        for p in (self._server_path, spec.model_path, spec.mmproj_path):
            if not os.path.isfile(p):
                raise ServerError(f"File not found: {p!r}")
        port = _free_port()
        image_tokens = MAX_IMAGE_PIXELS // _PIXELS_PER_IMAGE_TOKEN
        cmd = [
            self._server_path,
            "-m", spec.model_path, "--mmproj", spec.mmproj_path,
            "--host", "127.0.0.1", "--port", str(port),
            # Explicit device: llama.cpp silently falls back to CPU if its CUDA
            # backend fails to load; naming the device makes that a hard error.
            "--device", "CUDA0", "-ngl", "999",
            "-c", str(_CTX_SIZE), "--parallel", "1",
            "--image-max-tokens", str(image_tokens),
            "--jinja",
        ]
        logger.info("Starting llama-server for %s on port %d", spec.name, port)
        self._log.clear()
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, errors="replace",
        )
        self._drain_thread = threading.Thread(
            target=self._drain, args=(self._proc,), name="llama-server-log", daemon=True
        )
        self._drain_thread.start()
        self._spec = spec
        self._base = f"http://127.0.0.1:{port}"
        try:
            self._wait_healthy()
        except ServerError:
            self.unload()
            raise

    def caption(self, image_path: str, prompt: str, params: CaptionParams) -> str:
        if self.loaded_model is None:
            raise ServerError("No model loaded")
        payload = {
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": encode_image(image_path, params.max_pixels)}},
                {"type": "text", "text": prompt},
            ]}],
            "max_tokens": params.max_tokens,
            "temperature": params.temperature,
            "top_p": 0.8,
            "top_k": 20,
        }
        req = urllib.request.Request(
            f"{self._base}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise ServerError(f"llama-server returned HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ServerError(f"llama-server request failed: {e}{self._log_tail()}") from e
        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise ServerError(f"Unexpected llama-server response: {body!r:.500}") from e

    def unload(self) -> None:
        proc, self._proc, self._spec = self._proc, None, None
        drain, self._drain_thread = self._drain_thread, None
        if proc is None:
            return
        if proc.poll() is None:
            logger.info("Stopping llama-server")
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if drain is not None:
            drain.join(timeout=5)  # reaches EOF once the process is gone

    def shutdown(self) -> None:
        """Stop the process; safe to call from any thread at app exit."""
        self.unload()

    # -- internals ----------------------------------------------------------

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + _LOAD_TIMEOUT_S
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                code = proc.returncode if proc else None
                if self._drain_thread is not None:
                    self._drain_thread.join(timeout=2)  # collect its last words
                raise ServerError(f"llama-server exited (code {code}){self._log_tail()}")
            try:
                with urllib.request.urlopen(f"{self._base}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass  # still loading (503) or not listening yet
            time.sleep(0.25)
        raise ServerError(f"llama-server did not become ready in {_LOAD_TIMEOUT_S:.0f}s"
                          f"{self._log_tail()}")

    def _drain(self, proc: subprocess.Popen) -> None:
        # Keep the pipe empty (a full pipe would block the server) and retain
        # the tail for error messages.
        assert proc.stdout is not None
        with proc.stdout:
            for line in proc.stdout:
                self._log.append(line.rstrip())

    def _log_tail(self) -> str:
        lines = list(self._log)[-12:]
        return ("\n\nllama-server output:\n" + "\n".join(lines)) if lines else ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
