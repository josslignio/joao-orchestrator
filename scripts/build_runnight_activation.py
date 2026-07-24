#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from joao_orchestrator.run_night.activation import (
    RunNightActivation,
    load_hmac_key,
    sha256_file,
    verify_activation,
)
from joao_orchestrator.run_night.models import spec_from_dict


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--tranche3-sha", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ttl-hours", type=int, default=12)
    ns = parser.parse_args()

    if not 1 <= ns.ttl_hours <= 24:
        raise SystemExit("ttl-hours must be between 1 and 24")

    spec = spec_from_dict(json.loads(Path(ns.spec).read_text(encoding="utf-8")))
    spec.validate()
    key = load_hmac_key(Path(ns.key_file))
    repo = Path(spec.repo_root).expanduser().resolve()
    sec_boot = repo / "src/joao_orchestrator/bubble/write_tier_policy.py"
    now = datetime.now(timezone.utc).replace(microsecond=0)

    activation = RunNightActivation(
        activation_id=f"rna-{secrets.token_hex(8)}",
        run_id=spec.run_id,
        spec_sha256=spec.spec_sha256,
        tranche3_sha=ns.tranche3_sha,
        runnight_core_sha=spec.authorized_sha,
        sec_boot_sha256=sha256_file(sec_boot),
        tranche3_evidence_sha256=sha256_file(Path(spec.tranche3_evidence_path)),
        tranche3_closure_sha256=sha256_file(Path(spec.tranche3_closure_path)),
        runnight_evidence_sha256=sha256_file(Path(spec.runnight_evidence_path)),
        runnight_closure_sha256=sha256_file(Path(spec.runnight_closure_path)),
        m7_closed=True,
        m8_closed=True,
        m9_closed=True,
        m10_closed=True,
        tranche3_gpt_pass=True,
        runnight_core_gpt_pass=True,
        write_tier_off=True,
        issued_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(hours=ns.ttl_hours)).isoformat().replace("+00:00", "Z"),
        nonce=secrets.token_hex(32),
        key_id=ns.key_id,
    ).sign(key)

    # Fail before writing if any evidence, closure, SHA, SEC-BOOT or repo gate
    # is not valid. The HMAC alone is never treated as authority.
    verify_activation(activation, spec, key=key)

    output = Path(ns.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(activation.__dict__, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    output.chmod(0o600)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
