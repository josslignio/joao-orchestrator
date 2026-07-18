"""V2.2 BLOC B — the chat brain (cost-aware, honest, streaming) + BLOC B3 attachments.

JOÃO answers a CHAT-classified message directly, like Claude/GPT, via a local CLI:
  - Claude (default) through `claude -p … --output-format stream-json` → REAL token streaming,
    pinned to Sonnet (quota doctrine: Sonnet yes, Opus never) — the model ACTUALLY used is
    always read back from the stream and displayed (existing honesty rule).
  - GLM (selectable, 0 Claude-forfait) through `opencode run … --format json`.

Attachments (B3) are read by a DETERMINISTIC stage — real text extraction, never an eyeball
estimate: PDF via pypdf, text/code/csv/json read directly, images honestly declared
un-read (no vision channel wired). The answer is grounded in the EXTRACTED text, which is
traceable in the evidence.

Anti-lie (B4, non-negotiable): the system prompt forbids inventing facts; if a doc cannot be
read or an answer isn't known, JOÃO says so. Attachments that failed extraction are passed to
the model as an explicit "NON LISIBLE" note so it cannot silently fabricate their contents.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

CHAT_CLAUDE_MODEL = "sonnet"          # doctrine: Sonnet yes, Opus never (real model shown anyway)
GLM_MODEL = "zai-coding-plan/glm-4.5-air"
MAX_ATTACHMENT_CHARS = 20000
STREAM_DEADLINE_S = 180

CHAT_SYSTEM = (
    "Tu es JOÃO en mode CHAT : tu réponds directement et utilement, comme un assistant, en "
    "français par défaut (ou dans la langue de l'utilisateur). RÈGLE ABSOLUE D'HONNÊTETÉ : si "
    "tu ne sais pas, si tu ne peux pas lire un document joint, ou si tu n'as pas accès à une "
    "information (par ex. le web), DIS-LE clairement — n'invente JAMAIS un chiffre, un fait ou "
    "le contenu d'un document. Quand tu réponds à partir d'une pièce jointe, appuie-toi "
    "uniquement sur le TEXTE EXTRAIT fourni et cite-le. Tu n'as pas d'accès internet."
)

_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".py", ".js",
                  ".ts", ".tsx", ".jsx", ".html", ".css", ".yaml", ".yml", ".toml", ".ini",
                  ".sh", ".rst", ".xml", ".sql", ".c", ".cpp", ".h", ".go", ".rs", ".java"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}


@dataclass
class Attachment:
    name: str
    ok: bool
    kind: str = ""
    text: str = ""
    chars: int = 0
    method: str = ""
    truncated: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "kind": self.kind, "chars": self.chars,
                "method": self.method, "truncated": self.truncated, "error": self.error}


def extract_attachment(path: Path) -> Attachment:
    """Deterministic extraction — real text or an honest failure, NEVER an estimate (B3)."""
    path = Path(path)
    name = path.name
    suffix = path.suffix.lower()
    if not path.is_file():
        return Attachment(name, False, error="fichier introuvable")
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix in _TEXT_SUFFIXES:
        try:
            raw = path.read_text(errors="replace")
        except OSError as exc:
            return Attachment(name, False, kind="text", error=f"lecture impossible: {exc}")
        return _clip(Attachment(name, True, kind="text", method="read_text"), raw)
    if suffix in _IMAGE_SUFFIXES:
        return Attachment(name, False, kind="image", method="none",
                          error="image non lue — aucun canal vision branché (annoncé honnêtement)")
    # unknown binary — try a text read, else declare it honestly
    try:
        raw = path.read_text()
    except (OSError, UnicodeDecodeError):
        return Attachment(name, False, kind="binary", error="format binaire non pris en charge")
    return _clip(Attachment(name, True, kind="text", method="read_text"), raw)


def _extract_pdf(path: Path) -> Attachment:
    name = path.name
    try:
        import pypdf  # noqa: PLC0415
    except ImportError:
        return Attachment(name, False, kind="pdf", error="pypdf non installé — extraction impossible")
    try:
        reader = pypdf.PdfReader(str(path))
        parts = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # corrupt/encrypted PDF → honest failure, no invented content
        return Attachment(name, False, kind="pdf", error=f"extraction PDF échouée: {type(exc).__name__}: {exc}")
    text = "\n".join(parts).strip()
    if not text:
        return Attachment(name, False, kind="pdf", method="pypdf",
                          error="aucun texte extractible (PDF probablement scanné/image)")
    return _clip(Attachment(name, True, kind="pdf", method=f"pypdf({len(reader.pages)}p)"), text)


def _clip(att: Attachment, raw: str) -> Attachment:
    att.chars = len(raw)
    if len(raw) > MAX_ATTACHMENT_CHARS:
        att.text = raw[:MAX_ATTACHMENT_CHARS]
        att.truncated = True
    else:
        att.text = raw
    return att


def compose_prompt(message: str, history: Optional[list[dict]] = None,
                   attachments: Optional[list[Attachment]] = None) -> str:
    """Build the user-turn text: prior turns + attachment EXTRACTED text + the new message."""
    chunks: list[str] = []
    for turn in (history or [])[-8:]:
        role = "Utilisateur" if turn.get("role") == "user" else "JOÃO"
        chunks.append(f"{role}: {turn.get('content', '').strip()}")
    for att in (attachments or []):
        if att.ok:
            note = " (texte tronqué)" if att.truncated else ""
            chunks.append(f"### PIÈCE JOINTE « {att.name} » — TEXTE EXTRAIT via {att.method}"
                          f" ({att.chars} caractères{note}) :\n{att.text}")
        else:
            chunks.append(f"### PIÈCE JOINTE « {att.name} » — NON LISIBLE : {att.error}. "
                          f"Dis-le honnêtement à l'utilisateur ; n'invente pas son contenu.")
    chunks.append(f"Utilisateur: {message.strip()}")
    return "\n\n".join(chunks)


# ─────────────────────────── streaming brains ───────────────────────────
@dataclass
class ChatBrain:
    claude_executable: str = "claude"
    opencode_executable: str = "opencode"
    claude_model: str = CHAT_CLAUDE_MODEL
    glm_model: str = GLM_MODEL
    deadline_s: int = STREAM_DEADLINE_S
    # test seams: a factory (argv, cwd) -> iterable[str] of stdout lines; defaults to real Popen
    _line_source: object = field(default=None, repr=False)

    def reply_stream(self, message: str, *, history: Optional[list[dict]] = None,
                     model: str = "claude", attachments: Optional[list[Attachment]] = None) -> Iterator[dict]:
        """Yield {event: model|delta|done|error, ...}. The real model is always surfaced."""
        prompt = compose_prompt(message, history, attachments)
        if model == "glm":
            yield from self._stream_glm(prompt)
        else:
            yield from self._stream_claude(prompt)

    # ── Claude: real token streaming, real model read from message_start ──
    def _stream_claude(self, prompt: str) -> Iterator[dict]:
        argv = [self.claude_executable, "-p", prompt, "--model", self.claude_model,
                "--append-system-prompt", CHAT_SYSTEM, "--output-format", "stream-json",
                "--verbose", "--include-partial-messages"]
        model_seen = f"claude-cli:{self.claude_model}"
        announced = False
        full: list[str] = []
        for line in self._lines(argv):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "stream_event":
                ev = obj.get("event", {})
                et = ev.get("type")
                if et == "message_start":
                    model_seen = ev.get("message", {}).get("model", model_seen)
                    if not announced:
                        announced = True
                        yield {"event": "model", "model": model_seen, "provider": "claude-cli"}
                elif et == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        full.append(delta["text"])
                        yield {"event": "delta", "text": delta["text"]}
            elif kind == "result" and not full:
                # no deltas arrived (older CLI) → fall back to the final result text
                text = obj.get("result", "")
                if text:
                    full.append(text)
                    yield {"event": "delta", "text": text}
        if not announced:
            yield {"event": "model", "model": model_seen, "provider": "claude-cli"}
        yield {"event": "done", "text": "".join(full), "model": model_seen,
               "provider": "claude-cli", "cost": None}

    # ── GLM via opencode: text parts + real cost from step_finish (0 Claude forfait) ──
    def _stream_glm(self, prompt: str) -> Iterator[dict]:
        argv = [self.opencode_executable, "run", "-m", self.glm_model, "--format", "json", prompt]
        yield {"event": "model", "model": self.glm_model, "provider": "zai-coding-plan"}
        full: list[str] = []
        cost = None
        for line in self._lines(argv):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            part = obj.get("part", {})
            if obj.get("type") == "text" and part.get("text"):
                full.append(part["text"])
                yield {"event": "delta", "text": part["text"]}
            elif obj.get("type") == "step_finish":
                cost = part.get("cost", cost)
        yield {"event": "done", "text": "".join(full), "model": self.glm_model,
               "provider": "zai-coding-plan", "cost": cost}

    # ── bounded line source (real subprocess by default; injectable for tests) ──
    def _lines(self, argv: list[str]) -> Iterator[str]:
        if self._line_source is not None:
            yield from self._line_source(argv)
            return
        try:
            proc = subprocess.Popen(argv, shell=False, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                    start_new_session=True)
        except OSError as exc:
            yield json.dumps({"type": "result", "result": f"(CLI indisponible: {exc})"})
            return
        deadline = time.monotonic() + self.deadline_s
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                yield line
                if time.monotonic() > deadline:
                    self._kill(proc)
                    break
        finally:
            if proc.poll() is None:
                self._kill(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def available_brains(claude_executable: str = "claude", opencode_executable: str = "opencode") -> dict:
    import shutil
    return {"claude": {"available": bool(shutil.which(claude_executable)), "model": CHAT_CLAUDE_MODEL},
            "glm": {"available": bool(shutil.which(opencode_executable)), "model": GLM_MODEL}}
