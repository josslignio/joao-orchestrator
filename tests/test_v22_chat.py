"""V2.2 BLOC B — chat brain: deterministic attachment extraction, anti-lie, streaming parse."""
from __future__ import annotations

import json
from pathlib import Path

from joao_orchestrator.bubble.chat import (Attachment, ChatBrain, compose_prompt,
                                           extract_attachment)


# ─────────────────────────── B3 — deterministic extraction ───────────────────────────
def test_text_file_extracted(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Titre\nligne un\nligne deux\n")
    att = extract_attachment(p)
    assert att.ok and att.kind == "text" and "ligne deux" in att.text


def test_csv_extracted(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("a,b\n1,2\n")
    att = extract_attachment(p)
    assert att.ok and att.chars == len("a,b\n1,2\n")


def test_image_is_honestly_unreadable(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n and then bytes")
    att = extract_attachment(p)
    assert att.ok is False and att.kind == "image" and "vision" in att.error


def test_missing_file_is_honest(tmp_path):
    att = extract_attachment(tmp_path / "nope.txt")
    assert att.ok is False and "introuvable" in att.error


def test_real_pdf_extraction_counts_are_grounded(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_minimal_pdf("BANANA has three A letters: A A A"))
    att = extract_attachment(pdf)
    assert att.ok and att.kind == "pdf" and att.method.startswith("pypdf")
    assert att.text.count("A") == "BANANA has three A letters: A A A".count("A")


# ─────────────────────────── B4 — anti-lie composition ───────────────────────────
def test_prompt_grounds_on_extracted_text():
    att = Attachment("doc.pdf", True, kind="pdf", text="Le chiffre est 42.", chars=18, method="pypdf(1p)")
    prompt = compose_prompt("quel est le chiffre ?", attachments=[att])
    assert "TEXTE EXTRAIT" in prompt and "Le chiffre est 42." in prompt


def test_prompt_flags_unreadable_attachment_so_model_cannot_invent():
    att = Attachment("scan.pdf", False, kind="pdf", error="aucun texte extractible")
    prompt = compose_prompt("combien de pages ?", attachments=[att])
    assert "NON LISIBLE" in prompt and "n'invente pas" in prompt


# ─────────────────────────── streaming parse (real model surfaced) ───────────────────────────
def _claude_lines(_argv):
    return [
        json.dumps({"type": "stream_event", "event": {"type": "message_start",
                    "message": {"model": "claude-sonnet-5"}}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Bonjour"}}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": " !"}}}),
        json.dumps({"type": "result", "result": "Bonjour !"}),
    ]


def test_claude_stream_surfaces_real_model_then_deltas():
    brain = ChatBrain(_line_source=_claude_lines)
    events = list(brain.reply_stream("salut", model="claude"))
    model_ev = next(e for e in events if e["event"] == "model")
    assert model_ev["model"] == "claude-sonnet-5" and model_ev["provider"] == "claude-cli"
    deltas = [e["text"] for e in events if e["event"] == "delta"]
    assert "".join(deltas) == "Bonjour !"
    done = events[-1]
    assert done["event"] == "done" and done["text"] == "Bonjour !" and done["model"] == "claude-sonnet-5"
    # the model is announced BEFORE any text (honesty: the user always sees who answered)
    assert events.index(model_ev) < min(i for i, e in enumerate(events) if e["event"] == "delta")


def _glm_lines(_argv):
    return [
        json.dumps({"type": "step_start", "part": {}}),
        json.dumps({"type": "text", "part": {"text": "PONG"}}),
        json.dumps({"type": "step_finish", "part": {"cost": 0, "tokens": {"total": 10}}}),
    ]


def test_glm_stream_parses_text_and_cost():
    brain = ChatBrain(_line_source=_glm_lines)
    events = list(brain.reply_stream("ping", model="glm"))
    assert events[0] == {"event": "model", "model": "zai-coding-plan/glm-4.5-air",
                         "provider": "zai-coding-plan"}
    assert "".join(e["text"] for e in events if e["event"] == "delta") == "PONG"
    assert events[-1]["event"] == "done" and events[-1]["cost"] == 0


def _minimal_pdf(text: str) -> bytes:
    """A hand-rolled one-page PDF with a single text line — enough for pypdf.extract_text."""
    esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\nBT /F1 18 Tf 72 700 Td (%s) Tj ET\nendstream"
        % (len(esc) + 30, esc.encode("latin-1", "replace")),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
