# Architecture

TrustStack separates authoring, verification, and publication so that a model completing a task does not automatically produce an approved report.

## Trust pipeline

1. **Intake and scope** — A natural-language request is converted into an explicit research scope. Ambiguity requires a decision rather than silent guessing.
2. **Source acquisition** — Public material is fetched through allowlisted, SSRF-protected acquisition. Original bytes are stored by content hash and parsed versions retain provenance.
3. **Model execution** — A pinned, isolated Copilot runtime receives bounded tools and records calls, inputs, outputs, budgets, and termination state.
4. **Structured candidate** — The author produces a canonical report candidate with claims, source bindings, numerical inputs, calculations, limitations, and scope metadata.
5. **Independent checks** — Verification covers source identity, temporal alignment, numerical reproducibility, semantic support, and limitation disclosure. Verification has its own lifecycle and budget.
6. **Publication** — Only a verified candidate can become a formal report. Publication freezes the rendered report, evidence projection, verification receipts, and lineage.
7. **Correction** — Published bytes are never silently rewritten. A correction creates a new version linked to the version it supersedes.

## Main components

- `workbench/research_tasks.py` and `research_scope.py` manage intake, scope, and decisions.
- `workbench/worker.py` runs durable jobs independently of the HTTP process.
- `workbench/source_archive.py` and `documents.py` preserve originals and parsed material.
- `workbench/author_execution.py` and `candidate_contracts.py` define author execution and candidate boundaries.
- `workbench/verification_executor.py`, `verification_store.py`, and `evidence_checks.py` perform and persist checks.
- `workbench/report_publications.py` and `report_reader.py` publish immutable reports and reader payloads.
- `workbench/server.py` exposes the loopback HTTP API and reviewed static application.

## Reliability boundaries

TrustStack improves reliability but cannot guarantee truth. A source may itself be wrong, a model may omit a relevant fact, and a verifier may fail to detect a semantic error. The product therefore preserves visible uncertainty and review status instead of presenting verification as certainty.

A citation is not sufficient by itself. TrustStack distinguishes source identity, exact evidence location, semantic support, numerical reproducibility, and the time at which a claim could have been known.

## Storage

Runtime data is local and ignored by Git:

- `var/evidence.sqlite3` — entity registry and deterministic evidence metadata
- `var/chat.sqlite3` — conversations, runs, candidates, verification, and publication records
- `var/documents/` — content-addressed source objects and parsed document versions
- `var/auth/` — local owner authentication state
- `var/copilot-runtime/` — isolated, hash-verified model SDK environment

The evidence, chat, and document stores are deliberately distinct. Startup rejects unsafe database aliasing.
