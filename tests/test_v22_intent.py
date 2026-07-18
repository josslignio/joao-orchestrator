"""V2.2 BLOC A — deterministic intent router, incl. the exact demos that failed live 17/07."""
from __future__ import annotations

import pytest

from joao_orchestrator.bubble.intent import (AMBIGU, CHAT, KICKOFF, MISSION_CODE,
                                             classify, heuristic)


# ── the two demos that misfired on 17/07 (the whole reason for this run) ──
def test_hello_is_chat_never_a_run():
    r = heuristic("Hello ca va ?")
    assert r.intent == CHAT


def test_pdf_count_question_with_attachment_is_chat():
    r = heuristic("combien de A dans ce PDF", has_attachment=True)
    assert r.intent == CHAT


def test_build_request_is_mission():
    r = heuristic("crée password_coach.py avec des tests")
    assert r.intent == MISSION_CODE


# ── 5 chat / 5 mission (V2 table) ──
@pytest.mark.parametrize("text", [
    "Hello ca va ?",
    "explique-moi la récursion",
    "résume ce document",
    "c'est quoi un décorateur en python ?",
    "traduis cette phrase en anglais",
])
def test_five_chat_messages(text):
    assert heuristic(text).intent == CHAT


@pytest.mark.parametrize("text", [
    "crée password_coach.py avec tests",
    "écris un script qui parse un csv",
    "implémente une fonction is_prime dans primes.py",
    "build une petite app flask avec un endpoint /health",
    "refactor le module utils.py et ajoute des tests",
])
def test_five_mission_messages(text):
    assert heuristic(text).intent == MISSION_CODE


# ── Kickoff routing (adaptation 2.2b) ──
def test_new_project_routes_to_kickoff():
    assert heuristic("nouveau projet : un bot de veille d'emplois").intent == KICKOFF
    assert heuristic("lance un kickoff pour un CV bot").intent == KICKOFF


# ── manual override (A2) ──
def test_mode_override_wins():
    assert classify("Hello", mode="mission").intent == MISSION_CODE
    assert classify("crée un script", mode="chat").intent == CHAT


# ── ambiguity + optional LLM tie-break (A3) ──
def test_ambiguous_stays_ambiguous_without_classifier():
    r = heuristic("le module de paiement")   # noun, no verb, no question
    assert r.intent == AMBIGU


def test_llm_classifier_only_breaks_a_genuine_tie():
    calls = []

    def fake_llm(text):
        calls.append(text)
        return "MISSION_CODE"

    # a clearly-chat message must NOT consult the classifier
    classify("Hello ca va ?", llm_classifier=fake_llm)
    assert calls == []
    # an ambiguous one does, and the verdict is used
    r = classify("le module de paiement", llm_classifier=fake_llm)
    assert calls and r.intent == MISSION_CODE and r.source == "llm_classifier"


def test_unsure_classifier_stays_ambiguous():
    r = classify("le module de paiement", llm_classifier=lambda _t: "peut-être")
    assert r.intent == AMBIGU
