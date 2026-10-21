"""Bounded, deterministic research-scope detection for the first task slice.

This is deliberately not a general NLP resolver.  Only the aliases and scope
markers declared here are recognized; everything else remains unknown.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .research_intent import validate_intent


RESOLUTION_METHOD = "bounded_alias_rules_v1"

_COMPANIES = {
    "MSFT": {"label": "Microsoft", "aliases": ("MSFT", "Microsoft", "Microsoft Corporation", "微软")},
    "AAPL": {"label": "Apple", "aliases": ("AAPL", "Apple", "Apple Inc.", "苹果")},
}
_INDUSTRY_MARKERS = ("行业", "产业", "sector", "industry")
_TOPIC_MARKERS = ("主题", "topic")
_GENERIC_ENTITY_WORDS = {
    "revenue", "income", "profit", "loss", "growth", "cash", "flow", "earnings", "quarter", "source",
    "evidence", "limitation", "limitations", "trend", "continue", "explain", "how", "much", "what", "was",
    "is", "the", "a", "an", "of", "for", "and", "or", "to", "in", "last", "latest", "recent", "earlier",
    "comparison", "fy", "q", "analyze", "analyse", "research", "review", "business", "company", "not",
    "except", "excluding", "instead", "versus", "vs", "different", "from", "product", "market", "customer",
    "strategy", "competition", "risk", "acquisition", "investment", "financing", "launch", "margin",
    "dividends", "dividend", "buybacks", "buyback", "eps", "ebitda", "gaap", "usd",
}
_EXCLUSION_PREFIX = re.compile(
    r"(?:不要|别|排除|忽略|剔除|而不是|而非|不是|不研究|不分析)\s*(?:研究|分析|选择|看|考虑)?\s*$"
    r"|(?:\bnot|\bexcept|\bexcluding|\binstead\s+of)\s*$",
    re.I,
)
_EXCLUSION_CLAUSE = re.compile(
    r"^\s*(?:不要|别|排除|忽略|剔除|不研究|不分析|not\b|except\b|excluding\b|instead\s+of\b)", re.I
)
_BUSINESS_TERM = (
    r"(?:收入|营收|利润率?|毛利率|净利率|盈利|亏损|增长|下降|现金流|回购|分红|业绩|财报|"
    r"业务|产品|市场|客户|供应链|战略|管理层|竞争|风险|并购|收购|投资|融资|发布|推出|事件|"
    r"revenue|income|profit|margin|loss|growth|cash\s*flow|earnings|business|product|market|customer|"
    r"strategy|competition|risk|acquisition|investment|financing|launch)"
)
_RESEARCH_SEMANTICS = re.compile(
    rf"{_BUSINESS_TERM}|来源|证据|口径|(?:分析|研究|了解|评估|调查|比较)|"
    r"\b(?:analy[sz]e|research|review|compare|source|evidence)\b", re.I
)
_CONTEXT_ATOMS = re.compile(
    rf"(?:{_BUSINESS_TERM}|来源|证据|口径|限制|趋势|季度|财年|最近|最新|此前|去年|今年|上季|本季|"
    r"第?[一二三四1-4]季度|Q[1-4]|FY\s*\d{4}|20\d{2}(?:年|财年)?|"
    r"该公司|这家公司|这个公司|这个|那个|其|它|继续|请|再|说明|解释|是什么|是多少|多少|如何|怎么样|表现|"
    r"有什么|需要注意|增长了|下降了|了|呢|吗|的|与|和|"
    r"\b(?:the|company|its|it|this|that|what|how|much|was|is|were|are|did|does|do|please|continue|"
    r"explain|show|last|latest|recent|earlier|previous|current|year|fiscal|quarter|fy\s*\d{4}|q[1-4]|"
    r"source|evidence|limitation|limitations|trend|and|or|of|for|in|to)\b)", re.I
)
_CONTEXT_SIGNAL = re.compile(
    rf"{_BUSINESS_TERM}|来源|证据|口径|限制|季度|财年|20\d{{2}}|该公司|这家公司|这个公司|"
    r"\b(?:its|this company|the company|source|evidence|quarter|fy\s*\d{4}|q[1-4])\b", re.I
)
_PERIOD_PATTERNS = (
    re.compile(r"(?:FY\s*\d{4}|\d{4}\s*财年)(?:\s*(?:第?[一二三四1-4]\s*季度|Q[1-4]))?", re.I),
    re.compile(r"(?:20\d{2}(?:[-/年]\d{1,2})?(?:[-/月]\d{1,2}日?)?)(?:\s*(?:第?[一二三四1-4]\s*季度|Q[1-4]))?", re.I),
    re.compile(r"(?:第?[一二三四1-4]\s*季度|Q[1-4])", re.I),
)


def _contains_alias(text: str, alias: str) -> bool:
    if alias.isascii():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", text, re.I) is not None
    return alias in text


def _alias_match(text: str, alias: str):
    if alias.isascii():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", text, re.I)
    return re.search(re.escape(alias), text)


def _company_mentions(question: str) -> tuple[list[str], set[str]]:
    mentioned, excluded = [], set()
    for company_id, item in _COMPANIES.items():
        matches = [match for alias in item["aliases"] if (match := _alias_match(question, alias))]
        if not matches:
            continue
        mentioned.append(company_id)
        # Only tightly adjacent, explicit instructions exclude an alias. Generic
        # negation elsewhere (for example, "非金融损失") must not count.
        def explicitly_excluded(match):
            prefix = question[max(0, match.start() - 14):match.start()]
            clause = re.split(r"[，,。；;！？?]", question[:match.start()])[-1]
            return bool(_EXCLUSION_PREFIX.search(prefix) or _EXCLUSION_CLAUSE.search(clause))

        if all(explicitly_excluded(match) for match in matches):
            excluded.add(company_id)
    return mentioned, excluded


def _unknown_entity(question: str) -> str | None:
    """Find bounded entity-like tokens without maintaining a company blacklist."""
    # Whole-question financial follow-ups are stronger evidence than a partial
    # Chinese name-shaped match (e.g. “这个季度” or “它的利润率”). Unknown
    # tokens still fail this grammar, so this does not reinstate default scope.
    if _is_contextual_followup(question):
        return None
    scrubbed = question
    for item in _COMPANIES.values():
        for alias in item["aliases"]:
            scrubbed = re.sub(re.escape(alias), " ", scrubbed, flags=re.I if alias.isascii() else 0)
    # AI is also a stock ticker: recognize only explicit technology phrases,
    # not a bare AI subject (or $AI), so unrelated companies still require scope.
    scrubbed = re.sub(r"(?<![$A-Za-z0-9])AI(?=\s*(?:投资|投入|技术|回报|基础设施))", " ", scrubbed, flags=re.I)
    latin_candidates = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.&-]*", scrubbed):
        lowered = token.lower()
        if lowered not in _GENERIC_ENTITY_WORDS and not re.fullmatch(r"q[1-4]|fy(?:\d{4})?", lowered):
            latin_candidates.append(token)
    # Once a registered company is explicit, ordinary lower-case words may be
    # an uncommon metric. A second proper-name-shaped token remains unresolved.
    known_company_present = any(
        _contains_alias(question, alias) for item in _COMPANIES.values() for alias in item["aliases"]
    )
    # In an explicit conjunction/comparison even a lower-case unresolved name
    # may be another subject, not an uncommon metric of the known company.
    multiple_subject_wording = re.search(r"\b(?:and|or|vs|versus|against|compare[ds]?|comparison)\b|与|和|对比|比较|相比", question, re.I)
    if known_company_present and not multiple_subject_wording:
        latin_candidates = [token for token in latin_candidates if token[0].isupper()]
    # A free-form English protocol turn is not an entity merely because it has
    # words. Require bounded research semantics or an explicit subject field.
    if latin_candidates and (
        known_company_present
        or _RESEARCH_SEMANTICS.search(question)
        or re.search(r"[A-Za-z][A-Za-z0-9.&-]*\s*的", scrubbed)
        or re.search(r"\b(?:subject|company)\s*[:=]\s*[A-Za-z]", scrubbed, re.I)
    ):
        return latin_candidates[0]

    explicit = re.search(r"(?:对象|主体)\s*(?:是|为|[:：])\s*([\u4e00-\u9fff]{2,12})", scrubbed)
    if explicit:
        return explicit.group(1)

    # A Chinese name can directly precede a year, metric, business area, or
    # event. The shape is bounded, but the possible company names are not.
    subject_pattern = re.compile(
        rf"(?:^|[，,。；;！？?\s])(?:请|请帮我|帮我)?(?:分析|研究|了解|评估|调查|比较|关于)?\s*"
        rf"([\u4e00-\u9fff]{{2,12}}?)(?=(?:的)?(?:20\d{{2}}(?:年|财年)?|最近|近[一二三四\d])?{_BUSINESS_TERM})"
    )
    generic = {
        "公司", "该公司", "这家公司", "这个公司", "看看", "请看", "分析", "研究", "了解", "评估",
        "今年", "去年", "最近", "最新", "当前", "此前", "季度", "财年", "收入", "营收", "利润", "投资", "投入",
    }
    for match in subject_pattern.finditer(scrubbed):
        candidate = match.group(1)
        if candidate not in generic:
            return candidate
    return None


def _is_contextual_followup(question: str) -> bool:
    """Accept only a metric/period/pronoun follow-up made entirely of known atoms."""
    if not _CONTEXT_SIGNAL.search(question):
        return False
    unresolved = _CONTEXT_ATOMS.sub("", question)
    unresolved = re.sub(r"[\s\d,，.。;；:：!?！？'\"()（）/\-]+", "", unresolved)
    return not unresolved


def is_non_research_turn(question: str) -> bool:
    """Return true for legacy protocol chatter with no bounded research signal."""
    return not _RESEARCH_SEMANTICS.search(question) and _unknown_entity(question) is None


def _period_text(question: str) -> str | None:
    matches = [match for pattern in _PERIOD_PATTERNS for match in pattern.finditer(question)]
    if not matches:
        return None
    matches.sort(key=lambda item: (item.start(), -len(item.group(0))))
    return matches[0].group(0).strip()


def company_subject(company_id: str, catalog=None) -> dict[str, str]:
    company = (_COMPANIES if catalog is None else catalog)[company_id]
    return {"kind": "company", "id": company_id, "label": company["label"]}


def _base_scope(subjects: list[dict[str, Any]], question: str, *, ambiguities=None, gaps=None,
                context_conversation_id=None, human_selection=None) -> dict[str, Any]:
    return {
        "subjects": subjects,
        "period_text": _period_text(question),
        "metrics": [],
        "geography": None,
        "sample_definition_ref": None,
        "material_selection": None,
        "ambiguities": list(ambiguities or []),
        "gaps": list(gaps or []),
        "resolution_method": RESOLUTION_METHOD,
        "context_conversation_id": context_conversation_id,
        "human_selection": human_selection,
    }


def resolve_scope(question: str, *, context_company_id: str | None = None,
                  context_conversation_id: str | None = None,
                  coverage_lookup: Callable[[str], dict] | None = None) -> dict[str, Any]:
    """Resolve only registered aliases and explicit first-slice scope markers."""
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    question = question.strip()
    if not question or len(question) > 16_000:
        raise ValueError("question must contain 1..16000 characters")

    mentioned_companies, excluded_companies = _company_mentions(question)
    companies = [company_id for company_id in mentioned_companies if company_id not in excluded_companies]
    industry = any(_contains_alias(question, marker) for marker in _INDUSTRY_MARKERS)
    unknown_name = _unknown_entity(question)
    topic = any(_contains_alias(question, marker) for marker in _TOPIC_MARKERS)

    if industry:
        scope = _base_scope(
            [{"kind": "industry", "id": None, "label": "Industry scope in the user’s question"}], question,
            gaps=["Industry sample definition and a multi-company execution adapter are not yet available"],
            context_conversation_id=context_conversation_id,
        )
        return {
            "status": "unsupported_scope", "code": "industry_adapter_unavailable",
            "message": "Industry research identified. The task can be saved, but this version cannot reduce it to single-company execution.",
            "scope": scope, "options": [], "coverage": {"facts": None, "documents": None},
        }

    if len(companies) > 1:
        labels = " and ".join(_COMPANIES[item]["label"] for item in companies)
        scope = _base_scope(
            [{"kind": "comparison", "id": None, "label": labels + " comparison"}], question,
            ambiguities=["The question contains multiple registered companies; the scope for this research must be clarified"],
            context_conversation_id=context_conversation_id,
        )
        return {
            "status": "needs_clarification", "code": "multiple_companies",
            "message": "The question contains multiple companies. Select a single company for new research; comparison execution will be available in a future version.",
            "scope": scope,
            "options": [{"company_id": item, "label": _COMPANIES[item]["label"]} for item in companies],
            "coverage": {"facts": None, "documents": None},
        }

    if topic:
        scope = _base_scope(
            [{"kind": "topic", "id": None, "label": "Topic in the user’s question"}], question,
            gaps=["A topic-research execution adapter is not yet available"],
            context_conversation_id=context_conversation_id,
        )
        return {
            "status": "unsupported_scope", "code": "topic_adapter_unavailable",
            "message": "Topic research identified. The task can be saved, but this version cannot reduce it to single-company execution.",
            "scope": scope, "options": [], "coverage": {"facts": None, "documents": None},
        }

    if unknown_name or (mentioned_companies and not companies):
        label = unknown_name or _COMPANIES[mentioned_companies[0]]["label"]
        scope = _base_scope(
            [{"kind": "unknown", "id": None, "label": label}], question,
            ambiguities=[f"{label} is not an executable company registered by the current bounded parser"],
            context_conversation_id=context_conversation_id,
        )
        return {
            "status": "needs_clarification", "code": "unknown_subject",
            "message": "An unregistered subject was identified; the company will not be guessed from an old page or session.",
            "scope": scope, "options": [], "coverage": {"facts": None, "documents": None},
        }

    # Context is weaker than explicit text: inherit it only for a bounded
    # metric/period/pronoun follow-up whose whole text contains known atoms.
    selected = companies[0] if companies else (
        context_company_id if not mentioned_companies and _is_contextual_followup(question) else None
    )
    if selected in _COMPANIES:
        coverage = coverage_lookup(selected) if coverage_lookup else {"facts": {"available": True}, "documents": None}
        capability_available = _coverage_available(coverage)
        status = "ready" if capability_available else "missing_data"
        code = "scope_ready" if capability_available else "company_data_unavailable"
        message = ("The scope was resolved from bounded company aliases and can run with the existing single-company adapter." if capability_available else
                   "The company scope is resolved, but no facts or materials are currently available.")
        return {
            "status": status, "code": code, "message": message,
            "scope": _base_scope([company_subject(selected)], question,
                                 context_conversation_id=context_conversation_id),
            "options": [], "coverage": coverage,
        }

    scope = _base_scope(
        [{"kind": "unknown", "id": None, "label": "Research subject not specified"}], question,
        ambiguities=["The new research does not include an explicit, currently registered company alias"],
        context_conversation_id=context_conversation_id,
    )
    return {
        "status": "needs_clarification", "code": "subject_required",
        "message": "Specify the research subject; new research does not inherit a default company from the page.",
        "scope": scope,
        "options": [{"company_id": item, "label": _COMPANIES[item]["label"]} for item in ("MSFT", "AAPL")],
        "coverage": {"facts": None, "documents": None},
    }


def selected_company(result: dict[str, Any]) -> str | None:
    subjects = result.get("scope", {}).get("subjects", [])
    if len(subjects) == 1 and subjects[0].get("kind") == "company":
        return subjects[0].get("id")
    return None


def selected_companies(result):
    subjects = result.get("scope", {}).get("subjects", [])
    if (not subjects or any(not isinstance(item, dict) or item.get("kind") != "company"
                            or not isinstance(item.get("id"), str) or not item["id"] for item in subjects)):
        return ()
    identifiers = tuple(item["id"] for item in subjects)
    return identifiers if len(set(identifiers)) == len(identifiers) else ()


MODEL_RESOLUTION_METHOD = "llm_structured_intent_v1"


def registered_company_ids() -> tuple[str, ...]:
    """Legacy deterministic-fixture IDs; production reads EvidenceService's catalog."""
    return tuple(_COMPANIES)


def _catalog_name_id(name: str, catalog=None) -> str | None:
    """Link one complete model-produced name, never substrings from the question."""
    normalized = name.strip().casefold()
    matches = [company_id for company_id, item in (_COMPANIES if catalog is None else catalog).items()
               if normalized in {alias.casefold() for alias in item["aliases"]}]
    return matches[0] if len(matches) == 1 else None


def _coverage_available(coverage: dict[str, Any]) -> bool:
    return bool((coverage.get("facts") or {}).get("available") or
                (coverage.get("documents") or {}).get("available"))


def _model_scope(intent: dict[str, Any], subjects: list[dict[str, Any]], *,
                 context_conversation_id: str | None, provenance: dict[str, Any],
                 ambiguities=None, gaps=None, relationship_to_context="new") -> dict[str, Any]:
    return {
        "subjects": subjects, "period_text": intent["period_text"],
        "metrics": intent["metrics"], "basis": intent["basis"], "unit": intent["unit"],
        "directions": intent["directions"], "assumptions": intent["assumptions"],
        "uncertainties": intent["uncertainties"], "original_intent": intent["summary"],
        "normalized_question": intent.get("normalized_question"),
        "geography": None, "sample_definition_ref": None, "material_selection": None,
        "ambiguities": list(ambiguities or []), "gaps": list(gaps or []),
        "resolution_method": MODEL_RESOLUTION_METHOD,
        "context_conversation_id": context_conversation_id,
        "relationship_to_context": relationship_to_context, "human_selection": None,
        "intent_metadata": provenance,
    }


def resolve_model_intent(model_result: dict[str, Any], *, context_company_id: str | None = None,
                         context_conversation_id: str | None = None,
                         coverage_lookup: Callable[[str], dict] | None = None,
                         entity_catalog=None, context_company_ids=None) -> dict[str, Any]:
    """Validate a semantic model candidate against registry and current coverage only."""
    if not isinstance(model_result, dict):
        raise ValueError("model intent result must be an object")
    intent = validate_intent(model_result.get("intent"))
    provenance = model_result.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("model intent provenance must be an object")
    catalog = _COMPANIES if entity_catalog is None else entity_catalog

    candidates = []
    requested_subjects = []
    inconsistent_ids = set()
    for item in intent["subjects"]:
        raw = item["candidate_id"]
        candidate = raw.strip() if isinstance(raw, str) else None
        if candidate not in catalog and isinstance(candidate, str):
            matching_ids = [key for key in catalog if key.casefold() == candidate.casefold()]
            candidate = matching_ids[0] if len(matching_ids) == 1 else candidate
        name_candidate = _catalog_name_id(item["name"], catalog)
        if candidate is None and name_candidate is not None:
            candidate = name_candidate
        elif candidate not in catalog or (name_candidate is not None and name_candidate != candidate):
            if candidate is not None or name_candidate is not None:
                inconsistent_ids.update(value for value in (candidate, name_candidate) if value in catalog)
            candidate = None
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
        requested_subjects.append(company_subject(candidate, catalog) if candidate is not None else {
            "kind": item["kind"], "id": None, "label": item["name"] or "Subject outside the catalog"})
    excluded_ids = {matched for name in intent["excluded_entities"]
                    if (matched := _catalog_name_id(name, catalog)) is not None}
    inconsistent_ids.update(excluded_ids.intersection(candidates))
    relationship = "continuation" if intent["is_continuation"] else "new"
    context_ids = set(context_company_ids if context_company_ids is not None else (
        [context_company_id] if context_company_id else []))
    if context_ids and candidates and not set(candidates).issubset(context_ids):
        relationship = ("different_company_new_research" if len(context_ids) == len(candidates) == 1
                        else "different_scope_new_research")

    if intent["identity_ambiguity"] or intent["kind"] == "ambiguous_identity" or inconsistent_ids:
        subjects = [{"kind": "unknown", "id": None, "label": item["name"] or "Identity unresolved"}
                    for item in intent["subjects"]] or [{"kind": "unknown", "id": None, "label": "Identity unresolved"}]
        scope = _model_scope(intent, subjects, context_conversation_id=context_conversation_id,
                             provenance=provenance, ambiguities=intent["uncertainties"] or ["The research subject identity is ambiguous"],
                             relationship_to_context=relationship)
        return {
            "status": "needs_clarification",
            "code": "inconsistent_entity_link" if inconsistent_ids else "ambiguous_identity",
            "message": "The research subject identity is ambiguous. Confirm only the subject to continue; metrics, basis, and period can be explored later.",
            "scope": scope,
            "options": [{"company_id": item, "label": catalog[item]["label"]}
                        for item in dict.fromkeys(candidates + sorted(inconsistent_ids))],
            "coverage": {"facts": None, "documents": None},
        }

    if (intent["kind"] == "company_research" and candidates
            and any(subject["id"] is None for subject in requested_subjects)):
        scope = _model_scope(intent, requested_subjects, context_conversation_id=context_conversation_id,
                             provenance=provenance, relationship_to_context=relationship,
                             gaps=["Some research subjects are outside the registered catalog; the scope was not silently narrowed to companies with available materials"])
        return {"status": "unsupported_scope", "code": "partial_subject_coverage",
                "message": "The full research scope was preserved. Some subjects are not yet integrated and cannot be replaced by one company from the original question.",
                "scope": scope, "options": [], "coverage": {"facts": None, "documents": None}}

    if intent["kind"] == "company_research" and len(candidates) > 1:
        coverage = {item: coverage_lookup(item) if coverage_lookup else {
            "facts": {"available": True}, "documents": None} for item in candidates}
        missing = [item for item in candidates if not _coverage_available(coverage[item])]
        scope = _model_scope(intent, [company_subject(item, catalog) for item in candidates],
                             context_conversation_id=context_conversation_id, provenance=provenance,
                             relationship_to_context=relationship,
                             gaps=[f"{item} currently has no executable materials; the scope was not narrowed to another subject" for item in missing])
        return {"status": "missing_data" if missing else "ready",
                "code": "subject_data_unavailable" if missing else "scope_ready",
                "message": ("The full subject scope was preserved, but executable materials are temporarily unavailable for some subjects." if missing else
                            "The complete multi-subject scope is resolved; materials, periods, and source attribution remain separate for each subject."),
                "scope": scope, "options": [], "coverage": {"subjects": coverage}}

    if intent["kind"] == "company_research" and len(candidates) == 1:
        company_id = candidates[0]
        coverage = coverage_lookup(company_id) if coverage_lookup else {
            "facts": {"available": True}, "documents": None,
        }
        ready = _coverage_available(coverage)
        code = "scope_ready_new_research" if relationship == "different_company_new_research" else (
            "scope_ready" if ready else "company_data_unavailable")
        message = ("The question concerns another company. It was preserved as a distinct new research scope and will not modify the current session."
                   if relationship == "different_company_new_research" else
                   "An explorable company direction is resolved; omitted metrics, basis, or period will not prevent starting.")
        if not ready:
            message = "The company identity is resolved, but the current catalog has no executable data. The research direction was preserved for later tool recovery."
        scope = _model_scope(intent, [company_subject(company_id, catalog)],
                             context_conversation_id=context_conversation_id,
                             provenance=provenance, relationship_to_context=relationship,
                             gaps=[] if ready else ["No data is available in the current fact catalog"])
        return {"status": "ready" if ready else "missing_data", "code": code,
                "message": message, "scope": scope, "options": [], "coverage": coverage}

    kind = intent["kind"]
    if kind == "industry_research":
        subjects = [{"kind": "industry", "id": None, "label": item["name"] or "Industry research"}
                    for item in intent["subjects"]] or [{"kind": "industry", "id": None, "label": "Industry research"}]
        code, gap = "industry_coverage_unavailable", "The current catalog contains only a few companies and cannot represent an industry"
    elif kind == "topic_research":
        subjects = [{"kind": "topic", "id": None, "label": item["name"] or "Topic research"}
                    for item in intent["subjects"]] or [{"kind": "topic", "id": None, "label": "Topic research"}]
        code, gap = "topic_adapter_unavailable", "The current executor does not yet provide cross-subject topic-research tools"
    elif kind == "company_research":
        subjects = [{"kind": "unknown", "id": None, "label": item["name"] or "Company outside the catalog"}
                    for item in intent["subjects"]] or [{"kind": "unknown", "id": None, "label": "Company outside the catalog"}]
        code, gap = "company_not_covered", "The company identified by the model is not in the currently registered execution catalog"
    else:
        subjects = [{"kind": "topic", "id": None, "label": "No research request formed"}]
        code, gap = "non_research_intent", "The model did not identify a research direction that can be started"
    scope = _model_scope(intent, subjects, context_conversation_id=context_conversation_id,
                         provenance=provenance, gaps=[gap], relationship_to_context=relationship)
    return {
        "status": "unsupported_scope", "code": code,
        "message": "The original question and exploration direction were saved, but current data/tool coverage is insufficient. Continue after broader tools are integrated.",
        "scope": scope, "options": [], "coverage": {"facts": None, "documents": None},
    }
