"""V2.2 BLOC A — the intent router (the heart of the Chat Era).

Before JOÃO turns a message into anything, it classifies the message:

    CHAT           — a question, a greeting, "explique/résume/traduis", a question about an
                     attached doc → answered directly in chat (BLOC B), NO code run.
    MISSION_CODE   — "crée/écris/build un script/fichier/app + tests" → the existing pipeline
                     (now the Phase-1 cascade), unchanged.
    KICKOFF        — "nouveau projet / démarre un kickoff" → the Phase-0 interview (adaptation
                     2.2b: the CERVEAU Kickoff becomes a routed intent).
    AMBIGU         — genuinely unclear → the UI shows TWO buttons (💬 / 🛠️). NEVER a blind
                     22-minute run on a doubt.

A2 — a manual mode override (`chat` / `mission` / `auto`) always wins over the heuristic.
A3 — the DETERMINISTIC heuristic runs FIRST (no LLM, no clock, no randomness). Only when the
heuristic genuinely hesitates may an injected LLM classifier break the tie; if none is given,
or it too is unsure, the result stays AMBIGU (buttons), never a guess.

Pure and deterministic → unit-testable with no model call. Every decision carries its reason
and the signals that fired, so the routing is auditable in the evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CHAT = "CHAT"
MISSION_CODE = "MISSION_CODE"
KICKOFF = "KICKOFF"
AMBIGU = "AMBIGU"

# ── deterministic lexicons (fr + en) ──
_BUILD_VERBS = (
    "crée", "creer", "créer", "cree", "écris", "ecris", "écrire", "build", "buildé", "builde",
    "implémente", "implemente", "implémenter", "génère", "genere", "générer", "code", "coder",
    "développe", "developpe", "développer", "programme", "programmer", "refactor", "refactore",
    "refactorise", "corrige", "fixe", "débugge", "debugge", "réécris", "reecris", "ajoute",
    "write", "create", "implement", "generate", "develop", "refactor", "add", "fix", "make",
)
_BUILD_NOUNS = (
    "script", "fichier", "fichiers", "app", "application", "fonction", "fonctions", "classe",
    "classes", "module", "modules", "endpoint", "api", "programme", "package", "cli", "test",
    "tests", "composant", "librairie", "bibliothèque", "repo", "dépôt", "file", "function",
)
_FILE_EXT = re.compile(r"\b[\w./-]+\.(py|js|ts|tsx|jsx|go|rs|java|rb|sh|bash|zsh|html|css|scss|"
                       r"json|ya?ml|toml|md|c|cpp|cc|h|hpp|sql|php|swift|kt)\b", re.I)
_GREETINGS = ("hello", "hi", "hey", "salut", "bonjour", "bonsoir", "coucou", "yo", "hola",
              "ça va", "ca va", "cava", "wesh", "good morning", "good evening")
_CHAT_WORDS = (
    "explique", "expliquer", "explique-moi", "résume", "resume", "résumer", "traduis", "traduire",
    "c'est quoi", "cest quoi", "qu'est-ce", "quest-ce", "comment", "pourquoi", "combien", "quand",
    "quel", "quelle", "quels", "quelles", "où se", "définis", "definis", "définition", "definition",
    "dis-moi", "dis moi", "peux-tu m'expliquer", "raconte", "donne-moi ton avis", "que penses",
    "what", "why", "how", "explain", "summarize", "translate", "who", "which", "tell me",
)
_KICKOFF_SIGNALS = ("nouveau projet", "nouveau produit", "démarre un projet", "demarre un projet",
                    "démarrer un projet", "lance un kickoff", "lancer un kickoff", "kickoff",
                    "phase 0", "phase zéro", "phase zero", "new project", "start a project")


@dataclass
class IntentResult:
    intent: str
    confidence: float          # 0..1, the normalised margin between the two scores
    reason: str
    signals: dict = field(default_factory=dict)
    source: str = "heuristic"  # heuristic | override | llm_classifier

    def to_dict(self) -> dict:
        return {"intent": self.intent, "confidence": round(self.confidence, 3),
                "reason": self.reason, "signals": self.signals, "source": self.source}


def _count(text: str, needles) -> list[str]:
    return [n for n in needles if n in text]


def heuristic(text: str, *, has_attachment: bool = False) -> IntentResult:
    """The deterministic first stage — no LLM, no clock, no randomness."""
    raw = (text or "").strip()
    low = raw.lower()
    if not low:
        return IntentResult(AMBIGU, 0.0, "message vide", {})

    kickoff_hits = _count(low, _KICKOFF_SIGNALS)
    verbs = _count(low, _BUILD_VERBS)
    nouns = _count(low, _BUILD_NOUNS)
    exts = _FILE_EXT.findall(low)
    greetings = [g for g in _GREETINGS if low == g or low.startswith(g) or f" {g}" in low]
    chat_words = _count(low, _CHAT_WORDS)
    is_question = low.endswith("?")
    short = len(low.split()) <= 4

    if kickoff_hits:
        return IntentResult(KICKOFF, 0.95, f"signal Kickoff: {kickoff_hits}",
                            {"kickoff": kickoff_hits})

    mission_score = 2 * len(verbs) + len(nouns) + (2 if exts else 0)
    # a build verb that directly governs a build noun / file is a strong mission signal
    if verbs and (nouns or exts):
        mission_score += 2
    chat_score = len(chat_words) + (2 if greetings else 0) + (1 if is_question else 0)
    if has_attachment and (is_question or chat_words):
        chat_score += 2  # "combien de A dans ce PDF" + pièce jointe → chat, pas un build
    if short and greetings and not verbs:
        chat_score += 1

    signals = {"build_verbs": verbs, "build_nouns": nouns, "file_ext": exts,
               "greetings": greetings, "chat_words": chat_words, "question": is_question,
               "has_attachment": has_attachment, "mission_score": mission_score,
               "chat_score": chat_score}

    total = mission_score + chat_score
    if total == 0:
        return IntentResult(AMBIGU, 0.0, "aucun signal déterministe", signals)
    margin = abs(mission_score - chat_score) / total

    # a clear, unambiguous lead decides; a thin margin stays AMBIGU (→ 2 boutons)
    if mission_score >= 2 and mission_score > chat_score and margin >= 0.34:
        return IntentResult(MISSION_CODE, min(1.0, 0.5 + margin / 2),
                            f"verbes de build {verbs} / noms {nouns} / fichiers {exts}", signals)
    if chat_score >= 1 and chat_score > mission_score and margin >= 0.34:
        return IntentResult(CHAT, min(1.0, 0.5 + margin / 2),
                            f"salutation/question ({greetings or chat_words or '?'}) sans demande de build",
                            signals)
    return IntentResult(AMBIGU, round(1 - margin, 3),
                        "signaux mixtes — l'humain tranche (💬 / 🛠️)", signals)


def classify(text: str, *, mode: str = "auto", has_attachment: bool = False,
             llm_classifier=None) -> IntentResult:
    """Full router: manual override (A2) → deterministic heuristic (A3) → optional LLM tie-break.

    `llm_classifier(text) -> str in {CHAT, MISSION_CODE}` is consulted ONLY when the heuristic
    returns AMBIGU. If it is absent or returns anything else, the result stays AMBIGU (buttons).
    """
    mode = (mode or "auto").lower()
    if mode == "chat":
        return IntentResult(CHAT, 1.0, "mode manuel : Chat", {"override": True}, source="override")
    if mode == "mission":
        return IntentResult(MISSION_CODE, 1.0, "mode manuel : Mission", {"override": True}, source="override")

    result = heuristic(text, has_attachment=has_attachment)
    if result.intent != AMBIGU or llm_classifier is None:
        return result
    try:
        verdict = (llm_classifier(text) or "").strip().upper()
    except Exception as exc:  # a broken classifier must never fabricate a route
        result.signals["llm_error"] = f"{type(exc).__name__}: {exc}"
        return result
    if verdict in (CHAT, MISSION_CODE):
        return IntentResult(verdict, 0.6, f"heuristique hésitante → classifieur LLM: {verdict}",
                            {**result.signals, "llm_verdict": verdict}, source="llm_classifier")
    return result  # classifier unsure → stay AMBIGU (2 boutons), never guess
