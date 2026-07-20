"""joao_worker_host — the standalone sibling process that launches Claude/GLM
subscription CLIs on behalf of JOAO's controller (Boss architecture decision,
2026-07-20): JOAO is the sole standalone controller; workers never launch
other workers, and no worker is nested inside an active model session.
"""
