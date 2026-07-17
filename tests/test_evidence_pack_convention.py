"""B-25 — certified packs are immutable; later additions go to supplements."""
from __future__ import annotations

from joao_orchestrator.validation.evidence_pack import (
    MANIFEST_NAME, is_certified, is_reconstructed, is_supplement,
    next_supplement_dir, plan_addition, supplement_base, verify_pack, write_manifest,
)


def make_pack(tmp_path, name="pack-20260717"):
    pack = tmp_path / name
    (pack / "sub").mkdir(parents=True)
    (pack / "report.md").write_text("verdict\n")
    (pack / "sub" / "evidence.json").write_text("{}\n")
    return pack


def test_sealed_pack_verifies_clean_and_detects_every_mutation(tmp_path):
    pack = make_pack(tmp_path)
    assert not is_certified(pack)
    write_manifest(pack)
    assert is_certified(pack)
    assert verify_pack(pack) == []

    (pack / "report.md").write_text("verdict TAMPERED\n")
    assert any("content changed: report.md" in item for item in verify_pack(pack))
    write_manifest(pack)

    (pack / "late-addition.txt").write_text("added after certification\n")
    assert any("file added after certification" in item for item in verify_pack(pack))
    (pack / "late-addition.txt").unlink()

    (pack / "sub" / "evidence.json").unlink()
    assert any("missing file" in item for item in verify_pack(pack))


def test_additions_to_a_certified_pack_go_to_the_next_supplement(tmp_path):
    pack = make_pack(tmp_path)
    # Uncertified pack: writing in place is still allowed.
    assert plan_addition(pack) == pack
    write_manifest(pack)
    # Certified: the addition must land in the sibling supplement v1.
    target = plan_addition(pack)
    assert target == tmp_path / "pack-20260717-supplement-v1"
    target.mkdir()
    (target / "RECONSTRUCTED-driver.py").write_text("print('rebuilt')\n")
    write_manifest(target)
    assert verify_pack(target) == []
    # The next addition gets v2, and the original pack stays intact.
    assert next_supplement_dir(pack) == tmp_path / "pack-20260717-supplement-v2"
    assert verify_pack(pack) == []


def test_supplement_and_reconstructed_naming_rules():
    assert is_supplement("three-project-20260716T230824Z-supplement-v1")
    assert supplement_base("x-supplement-v3") == "x"
    assert not is_supplement("x-supplement-v0")
    assert not is_supplement("plain-pack")
    assert is_reconstructed("RECONSTRUCTED-three_project_acceptance.py")
    assert not is_reconstructed("three_project_acceptance.py")


def test_manifest_uses_sha256sum_format(tmp_path):
    pack = make_pack(tmp_path)
    manifest = write_manifest(pack)
    assert manifest.name == MANIFEST_NAME
    for line in manifest.read_text().splitlines():
        checksum, relative = line.split(None, 1)
        assert len(checksum) == 64
        assert not relative.startswith("/")
