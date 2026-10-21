"""Read-only preparation of the canonical formal publication reader closure."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib

from .answer_artifacts import AnswerArtifactService
from .chat import ChatService
from .report_reader import build_reader_payload
from .verification_store import required_verifier_projection


class _PreparedPublications:
    def __init__(self, publication, semantic_review, required_verifier_findings):
        self.payload = {**deepcopy(publication), "semantic_review": deepcopy(semantic_review),
                        "required_verifier_findings": deepcopy(list(required_verifier_findings))}

    def read(self, answer):
        if self.payload.get("answer") != answer.get("answer"):
            raise ValueError("prepared publication differs from prospective answer")
        return deepcopy(self.payload)


class _ProjectionChat(ChatService):
    """ChatService-compatible read facade for a not-yet-inserted formal message."""

    def __init__(self, base, answer, publication, semantic_review,
                 required_verifier_findings, published):
        # Deliberately do not initialize another service or database connection.
        self._base = base
        self._answer = deepcopy(answer)
        self._published = deepcopy(published)
        self.publications = _PreparedPublications(
            publication, semantic_review, required_verifier_findings)

    def __getattr__(self, name):
        return getattr(self._base, name)

    def answer_context(self, conversation_id, message_id):
        if (conversation_id, message_id) != (
                self._answer["conversation_id"], self._answer["message_id"]):
            return self._base.answer_context(conversation_id, message_id)
        return deepcopy(self._answer)

    def published_verification_evidence(self, conversation_id, message_id):
        if (conversation_id, message_id) == (
                self._answer["conversation_id"], self._answer["message_id"]):
            return deepcopy(self._published)
        return self._base.published_verification_evidence(conversation_id, message_id)

    def published_evidence_for_author_run(self, run_id):
        if run_id == self._published["author_run_id"]:
            return deepcopy(self._published)
        return self._base.published_evidence_for_author_run(run_id)


class PublicationSnapshotPreparer:
    """Prepare immutable artifact and reader bytes without opening a write transaction."""

    def __init__(self, chat, store):
        self.chat = chat
        self.store = store

    @staticmethod
    def formal_message_id(candidate_id: str, version_id: str) -> str:
        digest = hashlib.sha256(
            f"formal-publication/1\0{candidate_id}\0{version_id}".encode("utf-8")
        ).hexdigest()
        return f"msg_{digest}"

    def __call__(self, claim, prepared, formal_message_id):
        packet = self.store.read_claim_input(claim)
        if (packet["candidate_id"] != prepared.candidate_id
                or packet["version_id"] != prepared.version_id
                or packet["input_hash"] != prepared.input_hash):
            raise ValueError("snapshot projection is not bound to the current candidate version")
        events = self.store.read_evidence_events(
            claim, candidate_id=prepared.candidate_id, version_id=prepared.version_id)
        receipts = [event for event in events if event.get("verification_receipt_id")]
        persisted_findings = self.store.read_findings(
            claim, candidate_id=prepared.candidate_id, version_id=prepared.version_id)
        if (list(required_verifier_projection(persisted_findings))
                != list(prepared.required_verifier_findings)):
            raise ValueError("snapshot verifier projection differs from persisted findings")
        published = {
            "formal_message_id": formal_message_id,
            "candidate_id": prepared.candidate_id,
            "version_id": prepared.version_id,
            "publication_sha256": packet["publication_sha256"],
            "author_run_id": packet["author_run_id"],
            "conversation_id": packet["conversation_id"],
            "scope": deepcopy(packet["scope"]),
            "input_snapshot": deepcopy(packet["input_snapshot"]),
            "receipts": deepcopy(receipts),
        }
        with closing(self.chat._connect()) as connection:
            company = connection.execute(
                "SELECT company_id FROM conversations WHERE conversation_id=?",
                (packet["conversation_id"],),
            ).fetchone()
        if company is None:
            raise ValueError("snapshot conversation does not exist")
        answer = {
            "conversation_id": packet["conversation_id"],
            "message_id": formal_message_id,
            "run_id": packet["author_run_id"],
            "company_id": company["company_id"],
            "execution_scope": self.chat.execution_scope_for(
                {"conversation_id": packet["conversation_id"]}),
            "question": packet["question"],
            "execution_question": packet["question"],
            "answer": prepared.publication.answer,
            "citations": [item.to_json() for item in prepared.citations],
            "publication_schema": prepared.publication.schema_version,
            "source_snapshot": deepcopy(packet["input_snapshot"]),
        }
        projection = _ProjectionChat(
            self.chat, answer, prepared.publication.to_json(), prepared.semantic_review.to_json(),
            tuple(item.to_json() for item in prepared.required_verifier_findings), published)
        artifact = AnswerArtifactService(projection)._get(
            packet["conversation_id"], formal_message_id, answer)
        payload = build_reader_payload(artifact)
        receipt_manifest = sorted(
            (event["verification_receipt_id"], event["verification_receipt_sha256"])
            for event in receipts)
        return {"artifact": artifact, "payload": payload,
                "receipt_manifest": receipt_manifest}
