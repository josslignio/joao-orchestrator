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
import select
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


def _is_identity_or_capability_question(message: str) -> bool:
    """Detect if message is asking about identity or capabilities (H defect fix)."""
    low = message.lower().strip()
    identity_patterns = [
        "qui es-tu", "qui etes vous", "who are you", "what are you",
        "qu'est-ce que tu es", "quest-ce que tu es", "what is joao",
        "c'est quoi joao", "cest quoi joao", "what is this",
    ]
    # Finding 2 fix: pure capability questions (no action/internet required)
    pure_capability_patterns = [
        "que peux-tu faire", "que peut tu faire", "que peux tu faire",
        "what can you do", "de quoi es-tu capable", "de quoi tu es capable",
        "quels outils as-tu", "quels outils as tu", "quels outils tu as",
        "quel outil as-tu", "quel outil as tu", "what tools do you have",
    ]
    capability_patterns = [
        "peux-tu", "peut tu", "peux tu", "can you",
        "est-ce que tu peux", "est ce que tu peux",
        "as-tu", "as tu", "do you have",
        "sais-tu", "sais tu", "can you",
        "es-tu capable", "es tu capable", "are you capable",
        "quels outils", "quel outil", "what tools", "what can you do",
    ]
    action_patterns = [
        "modifier un fichier", "modifier des fichiers", "modify a file",
        "exécuter une commande", "executer une commande", "execute a command",
        "lancer des tests", "lancer un test", "run tests",
        "écrire du code", "ecrire du code", "write code",
        "créer un fichier", "creer un fichier", "create a file",
    ]
    internet_patterns = [
        "internet", "web", "en ligne", "online", "réseau", "reseau",
        "accès internet", "acces web", "web search", "search the web",
    ]

    # Check if message asks about identity
    if any(p in low for p in identity_patterns):
        return True

    # Finding 2 fix: check for pure capability questions FIRST (no action/internet required)
    if any(p in low for p in pure_capability_patterns):
        return True

    # Check if message asks about capabilities with actions
    if any(p in low for p in capability_patterns):
        if any(p in low for p in action_patterns):
            return True
        if any(p in low for p in internet_patterns):
            return True

    return False


def _capability_manifest() -> dict[str, str]:
    """Authoritative capability manifest - single source of truth for identity/capability responses."""
    return {
        "identity": (
            "Je suis JOÃO, un assistant d'orchestration local. "
            "Je fonctionne en mode chat normal : je peux répondre à des questions, "
            "expliquer du code, et analyser des documents que tu me fournis."
        ),
        "what_is_joao": (
            "JOÃO est un système d'orchestration local pour le développement logiciel. "
            "Je suis actuellement en mode chat normal : je réponds à tes questions "
            "et t'aide à comprendre du code, sans exécuter de commandes."
        ),
        "write_capability": (
            "En mode chat normal, je ne peux pas modifier de fichiers. "
            "Le write-tier (écriture/exécution) est actuellement désactivé par SEC-BOOT. "
            "Les missions avec écriture sont indisponibles actuellement."
        ),
        "execute_capability": (
            "En mode chat normal, je ne peux pas exécuter de commandes ou lancer des tests. "
            "L'exécution est désactivée par SEC-BOOT. "
            "Les missions avec exécution sont indisponibles actuellement."
        ),
        "code_creation_capability": (
            "En mode chat normal, je ne peux pas écrire ou créer de fichiers. "
            "Le write-tier est désactivé par SEC-BOOT. "
            "Les missions avec création de code sont indisponibles actuellement."
        ),
        "internet_capability": (
            "Je n'ai pas accès à internet. Je fonctionne uniquement en local, "
            "avec les fichiers et documents que tu me fournis."
        ),
        "tools_capability": (
            "En mode chat normal, j'ai accès à des modèles de langage (Claude, GLM) "
            "pour répondre à tes questions et analyser du texte. Je peux lire "
            "les fichiers que tu m'envoies (PDF, code, textes), mais je ne peux "
            "pas écrire ou exécuter de code."
        ),
        "general": (
            "En mode chat normal, je peux répondre à tes questions, expliquer du code, "
            "et analyser des documents. Je ne peux pas modifier de fichiers, exécuter "
            "des commandes, ou accéder à internet — ces capacités sont désactivées "
            "par SEC-BOOT. Les missions avec écriture/exécution sont indisponibles actuellement."
        ),
        "mission_write_unavailable": (
            "Les missions avec écriture (write-tier) sont indisponibles actuellement "
            "car SEC-BOOT bloque cette fonctionnalité. En mode chat normal, "
            "je peux uniquement répondre à des questions et analyser des documents, "
            "sans aucune capacité d'écriture, d'exécution ou de modification de fichiers."
        ),
    }


def _get_identity_or_capability_response(message: str) -> str:
    """Generate local response for identity/capability questions from capability_manifest (H defect fix)."""
    manifest = _capability_manifest()
    low = message.lower()

    if any(p in low for p in ["qui es-tu", "qui etes vous", "who are you", "what are you"]):
        return manifest["identity"]

    if "what is joao" in low or "c'est quoi joao" in low or "cest quoi joao" in low:
        return manifest["what_is_joao"]

    if any(p in low for p in ["modifier un fichier", "modifier des fichiers", "modify a file"]):
        return manifest["write_capability"]

    if any(p in low for p in ["exécuter une commande", "executer une commande", "execute a command",
                               "lancer des tests", "lancer un test", "run tests"]):
        return manifest["execute_capability"]

    if any(p in low for p in ["écrire du code", "ecrire du code", "write code",
                               "créer un fichier", "creer un fichier", "create a file"]):
        return manifest["code_creation_capability"]

    if any(p in low for p in ["internet", "web", "en ligne", "online"]):
        return manifest["internet_capability"]

    if "quels outils" in low or "quel outil" in low or "what tools" in low:
        return manifest["tools_capability"]

    return manifest["general"]


def _is_execution_request_without_project(message: str, history: Optional[list[dict]] = None,
                                          attachments: Optional[list[Attachment]] = None,
                                          active_project_id: Optional[str] = None) -> bool:
    """Detect if message is an execution request WITHOUT an active project (B defect fix).

    Uses ONLY authoritative project state (active_project_id), never deduces from words
    in history or attachments (I&B defect fix).
    """
    low = message.lower().strip()

    # Finding 3 fix: exclude interrogative/how-to forms (questions) to avoid false positives
    question_indicators = [
        "?", " pourquoi", " comment", " comment ", " expliqu", " qu'est-ce que",
        " quest-ce que", " c'est quoi", " c quoi", " how do", " how can",
        " explain", " what is", " what are", " why does",
    ]
    is_question = any(indicator in low for indicator in question_indicators)

    # Finding 3 fix: match whole words only, not substrings (e.g., "run" shouldn't match "runner")
    import re
    word_pattern = r'\b'

    # Build verbs from intent.py (whole-word matching)
    build_verbs = (
        "crée", "creer", "créer", "cree", "écris", "ecris", "écrire", "build", "buildé", "builde",
        "implémente", "implemente", "implémenter", "génère", "genere", "générer", "code", "coder",
        "développe", "developpe", "développer", "programme", "programmer", "refactor", "refactore",
        "refactorise", "corrige", "fixe", "débugge", "debugge", "réécris", "reecris", "ajoute",
        "write", "create", "implement", "generate", "develop", "refactor", "add", "fix", "make",
    )

    # Build nouns (whole-word matching)
    build_nouns = (
        "script", "fichier", "fichiers", "app", "application", "fonction", "fonctions", "classe",
        "classes", "module", "modules", "endpoint", "api", "programme", "package", "cli", "test",
        "tests", "composant", "librairie", "bibliothèque", "repo", "dépôt", "file", "function",
    )

    # Finding 3 fix: check for imperative action verbs (beginning of sentence or after comma)
    # rather than just presence anywhere in the sentence
    imperative_start = r'^[,\s]*[a-z]+'
    has_imperative_verb = any(
        re.search(rf'{word_pattern}{re.escape(verb)}{word_pattern}', low.split('.')[0].split(',')[0].strip())
        for verb in build_verbs
    )

    # Check for explicit execution requests (also whole-word)
    execution_patterns = [
        r"\blance les tests\b", r"\blance un test\b", r"\brun tests\b", r"\brun test\b",
        r"\bexécute\b", r"\bexecute\b", r"\blance\b", r"\brun\b",
        r"\bcorrige le bug\b", r"\bfix the bug\b", r"\bcorrige\b",
    ]
    has_execution = any(re.search(pattern, low) for pattern in execution_patterns)

    # Finding 3 fix: if it's a question, treat as general question, not execution request
    if is_question:
        return False

    # Finding 3 fix: require clear imperative intent for execution
    if not (has_imperative_verb or has_execution):
        return False

    # I&B defect fix: Use ONLY authoritative active_project_id, never deduce from history/attachments
    has_active_project = active_project_id is not None and active_project_id != ""

    # If it looks like an execution request but has NO active project, it's a B defect case
    if has_imperative_verb or has_execution:
        return not has_active_project

    return False


def _get_execution_requires_project_response(message: str) -> str:
    """Generate local response for execution requests without project (B defect fix)."""
    return ("Cette demande ressemble à une demande d'exécution (modification de code, lancement de tests, "
            "correction de bug), mais ces capacités sont indisponibles en mode chat normal.\n\n"
            "Le write-tier (écriture/exécution) est désactivé par SEC-BOOT. "
            "Les missions avec écriture ou exécution sont indisponibles actuellement.\n\n"
            "En mode chat normal, je peux uniquement répondre à tes questions et analyser des documents, "
            "sans aucune capacité d'écriture, d'exécution ou de modification de fichiers.")


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
                     model: str = "claude", attachments: Optional[list[Attachment]] = None,
                     active_project_id: Optional[str] = None) -> Iterator[dict]:
        """Yield {event: model|delta|done|error, ...}. The real model is always surfaced."""
        # H defect fix: handle identity/capability questions locally
        if _is_identity_or_capability_question(message):
            response = _get_identity_or_capability_response(message)
            yield {"event": "model", "model": "JOAO-local", "provider": "local-controller"}
            for char in response:
                yield {"event": "delta", "text": char}
            yield {"event": "done", "text": response, "model": "JOAO-local",
                   "provider": "local-controller", "cost": None}
            return

        # B defect fix: handle execution requests without project locally (Finding 1: use active_project_id)
        if _is_execution_request_without_project(message, history, attachments, active_project_id):
            response = _get_execution_requires_project_response(message)
            yield {"event": "model", "model": "JOAO-local", "provider": "local-controller"}
            for char in response:
                yield {"event": "delta", "text": char}
            yield {"event": "done", "text": response, "model": "JOAO-local",
                   "provider": "local-controller", "cost": None}
            return

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
        any_delta = False

        try:
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
                            any_delta = True
                            yield {"event": "delta", "text": delta["text"]}
                elif kind == "result" and not full:
                    # no deltas arrived (older CLI) → fall back to the final result text
                    text = obj.get("result", "")
                    if text:
                        full.append(text)
                        any_delta = True
                        yield {"event": "delta", "text": text}
        except OSError as exc:
            yield {"event": "error", "message": f"CLI indisponible: {exc}"}
            return
        except TimeoutError as exc:
            yield {"event": "error", "message": str(exc)}
            return
        except Exception as exc:
            yield {"event": "error", "message": f"Erreur Claude: {type(exc).__name__}: {exc}"}
            return

        # D defect fix: if no text was produced, emit an explicit error
        if not any_delta and not full:
            yield {"event": "error", "message": "Le provider Claude n'a retourné aucun texte (stream vide)"}
            return

        # D defect fix: only emit done if we have content (no error, not empty)
        if any_delta or full:
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
        any_delta = False

        try:
            for line in self._lines(argv):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                part = obj.get("part", {})
                if obj.get("type") == "text" and part.get("text"):
                    full.append(part["text"])
                    any_delta = True
                    yield {"event": "delta", "text": part["text"]}
                elif obj.get("type") == "step_finish":
                    cost = part.get("cost", cost)
        except OSError as exc:
            yield {"event": "error", "message": f"CLI indisponible: {exc}"}
            return
        except TimeoutError as exc:
            yield {"event": "error", "message": str(exc)}
            return
        except Exception as exc:
            yield {"event": "error", "message": f"Erreur GLM: {type(exc).__name__}: {exc}"}
            return

        # D defect fix: if no text was produced, emit an explicit error
        if not any_delta and not full:
            yield {"event": "error", "message": "Le provider GLM n'a retourné aucun texte (stream vide)"}
            return

        # D defect fix: only emit done if we have content (no error, not empty)
        if any_delta or full:
            yield {"event": "done", "text": "".join(full), "model": self.glm_model,
                   "provider": "zai-coding-plan", "cost": cost}

    # ── bounded line source (real subprocess by default; injectable for tests) ──
    def _lines(self, argv: list[str]) -> Iterator[str]:
        if self._line_source is not None:
            yield from self._line_source(argv)
            return
        try:
            proc = subprocess.Popen(argv, shell=False, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1,
                                    start_new_session=True)
        except OSError as exc:
            raise OSError(f"CLI indisponible: {exc}")

        deadline = time.monotonic() + self.deadline_s
        timed_out = False

        try:
            # Use select to check if stdout has data with timeout
            import select

            while True:
                # Check deadline first (timeout interruptible even without stdout output).
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._kill(proc)
                    break

                # Never sleep past the configured deadline.
                try:
                    rlist, _, _ = select.select([proc.stdout], [], [], min(0.1, remaining))
                except (ValueError, OSError):
                    break

                if rlist:
                    line = proc.stdout.readline()
                    if line:
                        yield line
                    else:
                        break  # EOF

                # Check if process has terminated
                if proc.poll() is not None:
                    break
        finally:
            # Drain stderr to avoid deadlock (D defect fix)
            if proc.poll() is None:
                self._kill(proc)

            # Read any remaining stderr to avoid deadlock
            try:
                _ = proc.stderr.read()  # type: ignore[union-attr]
            except Exception:
                pass

            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        # D defect fix: if timed out, raise an error to be caught by the stream methods
        if timed_out:
            raise TimeoutError(f"Provider timeout après {self.deadline_s}s")

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
