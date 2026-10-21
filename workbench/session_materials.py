"""Conversation-owned originals and read-only federation with public documents."""
from contextlib import closing, contextmanager, ExitStack
import base64
import binascii
import hashlib
from pathlib import Path

from .documents import DocumentError, DocumentService
from .material_parser import MaterialDocumentService, UPLOAD_TYPES
from .chat import ChatError


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BODY = ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 8192


class SessionMaterials:
    def __init__(self, chat, *, object_store_factory=None):
        self.chat = chat
        self.root = chat.chat_db.with_name(chat.chat_db.name + ".materials")
        self.object_store_factory = object_store_factory

    def _store(self, conversation_id, *, create=False):
        conversation_id = self.chat._valid_id(conversation_id, "conversation_id")
        with closing(self.chat._connect()) as connection:
            owner = connection.execute("SELECT company_id FROM conversations WHERE conversation_id=?",
                                       (conversation_id,)).fetchone()
        if owner is None:
            raise ChatError("conversation_not_found", "Research session not found.", 404)
        path = self.root / hashlib.sha256(conversation_id.encode()).hexdigest()
        if self.root.is_symlink() or path.is_symlink():
            raise ChatError("material_storage_invalid", "The session materials directory cannot be a symbolic link.", 409)
        if path.exists() and not (path / "catalog.sqlite3").is_file():
            raise ChatError("material_storage_invalid", "The session materials directory exists but its registry is missing; uploaded materials were not ignored.", 409)
        if not create and not path.exists():
            return None, owner["company_id"]
        object_store = (self.object_store_factory(conversation_id)
                        if self.object_store_factory is not None else None)
        return MaterialDocumentService(path, object_store=object_store), owner["company_id"]

    def documents(self, conversation_id):
        private, company_id = self._store(conversation_id)
        if private is None:
            return self.chat.document_service
        return ConversationDocuments(
            self.chat.document_service, private, conversation_id, company_id,
            company_ids=self.chat.execution_scope_for({"conversation_id": conversation_id})["company_ids"])

    def list(self, conversation_id):
        private, company_id = self._store(conversation_id)
        coverage = private.get_document_coverage(company_id=company_id) if private else {"documents": []}
        if private:
            for item in coverage["documents"]:
                if item["rights_status"] != f"session_only:{conversation_id}":
                    raise ChatError("material_owner_mismatch", "The material does not belong to this session.", 409)
        return {"conversation_id": conversation_id, "materials": [
            {**item, "source_scope": "session_material", "conversation_id": conversation_id}
            for item in coverage["documents"]],
            "supported_upload_types": list(dict.fromkeys(UPLOAD_TYPES.values())),
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "limitations": ["Supports HTML, Markdown, UTF-8 text/CSV, XLSX, and PDFs with extractable text.",
                            "XLSX parsing reads cells only and does not execute formulas; date serial values are not automatically interpreted as dates, and charts or external links are not read.",
                            "Markdown is processed as UTF-8 text; PDFs are not OCRed, and tables or complex layouts are not reconstructed.",
                            "Used only in this session and not added to the public corpus; uploading does not modify older reports or the currently frozen research."]}

    def raw(self, conversation_id, document_version_id):
        private, company_id = self._store(conversation_id)
        if private is None:
            raise ChatError("material_not_found", "This material was not found in the session.", 404)
        with closing(private._connect()) as connection:
            row = connection.execute(
                "SELECT title,media_type,rights_status FROM document_versions WHERE document_version_id=? AND company_id=?",
                (document_version_id, company_id)).fetchone()
        if row is None or row["rights_status"] != f"session_only:{conversation_id}":
            raise ChatError("material_not_found", "This material was not found in the session.", 404)
        return (private.read_raw_version(company_id=company_id, document_version_id=document_version_id),
                row["media_type"], "material-" + document_version_id[:12] + Path(row["title"]).suffix.lower())

    def upload(self, conversation_id, filename, content_base64, allow_model_use):
        if allow_model_use is not True:
            raise ChatError("material_consent_required", "Confirm that this material may be used in this session and sent to the configured research model.")
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 240
                or any(character in filename for character in ("/", "\\"))
                or any(ord(character) < 32 or ord(character) == 127 for character in filename)
                or Path(filename).suffix.lower() not in UPLOAD_TYPES):
            raise ChatError("unsupported_material", "Supported formats are HTML, Markdown, TXT, CSV, XLSX, and PDFs with extractable text; do not upload other formats by changing their file extension.")
        if not isinstance(content_base64, str) or len(content_base64) > MAX_UPLOAD_BODY:
            raise ChatError("material_too_large", "Each material must not exceed 5 MiB.", 413)
        try:
            raw = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ChatError("invalid_material", "Invalid material encoding.") from exc
        if not 0 < len(raw) <= MAX_UPLOAD_BYTES:
            raise ChatError("material_too_large", "Each material must contain between 1 byte and 5 MiB.", 413)
        with closing(self.chat._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM runs WHERE conversation_id=? AND status IN ('queued','running')",
                                  (conversation_id,)).fetchone():
                raise ChatError("research_active", "Wait for the current research run to finish before uploading to avoid changing active research inputs.", 409)
            if connection.execute(
                    "SELECT 1 FROM research_decisions d JOIN runs r USING(run_id) "
                    "LEFT JOIN research_decision_answers a USING(decision_id) "
                    "WHERE r.conversation_id=? AND a.decision_id IS NULL", (conversation_id,)).fetchone():
                raise ChatError("confirmation_pending", "Resolve the pending confirmation first; input materials cannot be changed during confirmation.", 409)
            private, company_id = self._store(conversation_id, create=True)
            record = private.register_bytes(
                raw, company_id=company_id, source_key=f"session:{conversation_id}:{filename}",
                title=filename, media_type=UPLOAD_TYPES[Path(filename).suffix.lower()],
                rights_status=f"session_only:{conversation_id}",
                expected_sha256=hashlib.sha256(raw).hexdigest())
        return {**record, "conversation_id": conversation_id, "source_scope": "session_material",
                "model_use_authorized": True, "uploaded_at": record["registered_at"],
                "message": "Material saved for this session only. Continue with a question to include it in new research."
                if record["parse_status"] == "parsed" else "The original file was saved, but its text could not be parsed and it cannot yet be used as searchable evidence."}


class ConversationDocuments:
    """Only these two stores are reachable; never search another conversation."""
    def __init__(self, public, private, conversation_id, company_id, *, company_ids=None):
        self.public, self.private = public, private
        self.conversation_id, self.company_id = conversation_id, company_id
        self.company_ids = tuple(company_ids) if company_ids is not None else (company_id,)

    @contextmanager
    def frozen_replay_scope(self):
        # This scope changes no routing or ownership checks. The stores remain
        # separate, and their request-local caches expire together on exit.
        with ExitStack() as scopes:
            for store in (self.public, self.private):
                if store is not None:
                    scopes.enter_context(store.frozen_replay_scope())
            yield

    def _check_company(self, company_id):
        if company_id not in self.company_ids:
            raise DocumentError("document_not_found", "Material is outside this conversation")

    def _store_company(self, store, company_id):
        return self.company_id if store is self.private else company_id

    def _tag(self, result, store):
        if store is self.private:
            if result.get("rights_status") != f"session_only:{self.conversation_id}":
                raise DocumentError("material_owner_mismatch", "Private material ownership differs from this conversation")
            result = {**result, "source_scope": "session_material", "conversation_id": self.conversation_id}
        return result

    def _owner(self, company_id, identifier, *, passage=False):
        self._check_company(company_id)
        for store in (self.private, self.public):
            if store is None:
                continue
            with closing(store._connect()) as connection:
                stored_company = self._store_company(store, company_id)
                if passage:
                    found = connection.execute(
                        "SELECT v.rights_status FROM passages p JOIN document_versions v USING(document_version_id) "
                        "WHERE p.passage_id=? AND v.company_id=?", (identifier, stored_company)).fetchone()
                else:
                    found = connection.execute(
                        "SELECT rights_status FROM document_versions WHERE document_version_id=? AND company_id=?",
                        (identifier, stored_company)).fetchone()
            if found:
                if store is self.private and found["rights_status"] != f"session_only:{self.conversation_id}":
                    raise DocumentError("material_owner_mismatch", "Private material ownership differs from this conversation")
                return store
        raise DocumentError("passage_not_found" if passage else "document_not_found",
                            "Source is not available in this conversation")

    def get_document_coverage(self, *, company_id):
        self._check_company(company_id)
        result = {key: 0 for key in ("enrolled", "parsed", "failed", "unsupported")}
        result["documents"] = []
        for store in (self.public, self.private):
            if store:
                coverage = store.get_document_coverage(company_id=self._store_company(store, company_id))
                for key in ("enrolled", "parsed", "failed", "unsupported"):
                    result[key] += coverage[key]
                result["documents"].extend(self._tag(item, store) for item in coverage["documents"])
        return result

    def search_documents(self, *, company_id, query, limit=8, document_version_ids=None):
        self._check_company(company_id)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DocumentError("invalid_arguments", "limit must be an integer from 1 to 50")
        ids = document_version_ids
        if ids is None:
            ids = [item["document_version_id"] for item in self.get_document_coverage(company_id=company_id)["documents"]]
        if not isinstance(ids, (list, tuple)):
            raise DocumentError("invalid_arguments", "document_version_ids must be a list")
        selections = {store: [] for store in (self.private, self.public) if store is not None}
        for identifier in ids:
            selections[self._owner(company_id, identifier)].append(identifier)
        batches, coverage = [], {key: 0 for key in ("enrolled", "parsed", "failed", "unsupported")}
        coverage["documents"] = []
        expanded = None
        for store, selected in selections.items():
            if not selected:
                continue
            found = store.search_documents(company_id=self._store_company(store, company_id),
                                           query=query, limit=limit, document_version_ids=selected)
            batches.append([self._tag(item, store) for item in found["results"]])
            expanded = found["expanded_query"]
            for key in ("enrolled", "parsed", "failed", "unsupported"):
                coverage[key] += found["coverage"][key]
            coverage["documents"].extend(self._tag(item, store) for item in found["coverage"]["documents"])
        # Scores from independent FTS indexes are not comparable.
        results = [batch[index] for index in range(limit) for batch in batches if index < len(batch)][:limit]
        return {"results": results, "coverage": coverage, "expanded_query": expanded,
                "version_scope": "explicit" if document_version_ids is not None else "current_per_source",
                "as_of": None, "interpretation": "Conversation materials and public matches interleaved; independent FTS scores are not compared. Missing matches do not establish absence of disclosure."}

    def read_passage(self, *, company_id, passage_id, **kwargs):
        store = self._owner(company_id, passage_id, passage=True)
        return self._tag(store.read_passage(
            company_id=self._store_company(store, company_id), passage_id=passage_id, **kwargs), store)

    def derive_pdf_tables(
        self, *, company_id, base_document_version_id, table_selectors,
    ):
        store = self._owner(company_id, base_document_version_id)
        result = self._tag(store.derive_pdf_tables(
            company_id=self._store_company(store, company_id),
            base_document_version_id=base_document_version_id,
            table_selectors=table_selectors,
        ), store)
        # Private bytes are stored once under the conversation anchor but are explicitly
        # dispatched as each authorized logical subject. Addition grants bind that choice.
        return {**result, "company_id": company_id} if store is self.private else result

    def read_frozen_version(self, *, company_id, document_version_id):
        store = self._owner(company_id, document_version_id)
        return self._tag(store.read_frozen_version(
            company_id=self._store_company(store, company_id), document_version_id=document_version_id), store)

    def read_raw_version(self, *, company_id, document_version_id):
        store = self._owner(company_id, document_version_id)
        return store.read_raw_version(
            company_id=self._store_company(store, company_id), document_version_id=document_version_id)

    def read_table(self, *, company_id, document_version_id, table_index):
        store = self._owner(company_id, document_version_id)
        return self._tag(store.read_table(company_id=self._store_company(store, company_id), document_version_id=document_version_id,
                                         table_index=table_index), store)
