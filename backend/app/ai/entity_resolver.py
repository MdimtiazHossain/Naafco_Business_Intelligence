"""Entity resolution: user words -> official Phase 1 master records.

Nothing is ever invented here. A term either matches a real master record or it
does not, and when it matches several the agent asks instead of guessing.

Matching order, strongest first:

1. exact code       ``Z001`` -> dim_zone
2. exact name       ``Dhaka`` -> dim_region
3. partial name     ``Dha`` -> dim_region (only when unambiguous within a type)

A term matching different *types* — an "ABC" material and an "ABC" customer —
raises :class:`AmbiguousEntityError`, which the orchestrator turns into a
clarifying question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Collection, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import (
    DimArea,
    DimBusinessUnit,
    DimCompany,
    DimMaterial,
    DimRegion,
    DimSalesLine,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
)
from ..database.models_warehouse import DimCustomer, DimSalesForce
from .exceptions import AmbiguousEntityError, EntityNotFoundError
from .schemas import EntityType, ResolvedEntity

#: Words that must never be treated as an entity name even if a master record
#: happens to share the spelling — otherwise "sales" could match a brand.
_ENGLISH_STOPWORDS = {
    "sales", "sale", "collection", "outstanding", "stock", "target", "show", "give",
    "me", "the", "of", "for", "in", "and", "or", "top", "bottom", "this", "last",
    "today", "yesterday", "month", "week", "year", "day", "days", "wise", "by",
    "region", "zone", "area", "unit", "territory", "product", "material",
    "customer", "summary",
    "total", "how", "much", "what", "which", "is", "are", "was", "were", "down", "up",
    "growth", "trend", "detail", "details", "report", "data", "please", "all",
    "achievement", "coverage", "aging", "overdue", "low", "out", "current", "vs",
    "versus", "compare", "comparison", "performance", "management", "business",
}

#: The same list for Bangla, in both scripts.
#:
#: This half was missing, and its absence was not cosmetic: the agent answers
#: English, Bangla and mixed questions, so "sales koto?" is an ordinary way to
#: ask how much was sold. ``koto`` is a question word — the exact counterpart of
#: ``how`` and ``much``, which were already here — but it is also the first four
#: letters of the sub-territory "South Kotowali", so it matched, and a question
#: about the whole company silently came back scoped to one sub-territory. Any
#: romanised Bangla word an operator types routinely belongs here for the same
#: reason its English equivalent does.
_BANGLA_STOPWORDS = {
    # question and quantity words
    "koto", "koto tk", "koto taka", "kemon", "ki", "kobe", "kothay", "kon", "kar",
    "kom", "beshi", "besi", "boro", "choto", "valo", "bhalo", "kharap",
    # verbs an operator ends a request with
    "dekhao", "dekha", "dao", "dio", "koro", "korun", "ache", "achhe", "ase",
    "hobe", "hoyeche", "chai", "lagbe", "janao", "bolo",
    # periods and common nouns
    "aaj", "aajker", "ajker", "gato", "goto", "mash", "mas", "mase", "maser",
    "bochor", "bosor", "shoptaho", "din", "taka", "tk", "bikri", "poriman",
    "গত", "মাস", "মাসে", "মাসের", "আজ", "আজকের", "কত", "কতো", "দেখাও", "দাও",
    "কেমন", "কি", "কবে", "কোথায়", "টাকা", "বিক্রি", "সেলস", "মোট", "সব",
}

STOPWORDS = _ENGLISH_STOPWORDS | _BANGLA_STOPWORDS

TOKEN_RE = re.compile(r"[A-Za-z0-9ঀ-৿][A-Za-z0-9ঀ-৿\-_/]*")

#: Words *inside a master name*, for anchoring a partial match to a word start.
_WORD_RE = re.compile(r"[a-z0-9ঀ-৿]+")


@dataclass(frozen=True)
class EntityBinding:
    """How one entity type is loaded from the warehouse."""

    entity_type: EntityType
    model: type
    code_field: str
    name_field: str | None
    extra_name_fields: tuple[str, ...] = ()


ENTITY_BINDINGS: tuple[EntityBinding, ...] = (
    EntityBinding(EntityType.COMPANY, DimCompany, "company_code", "company_name"),
    EntityBinding(EntityType.BUSINESS_UNIT, DimBusinessUnit, "bu_code", "bu_name"),
    EntityBinding(EntityType.SALES_LINE, DimSalesLine, "sales_line_code",
                  "sales_line_name"),
    EntityBinding(EntityType.ZONE, DimZone, "zone_code", "zone_name"),
    EntityBinding(EntityType.REGION, DimRegion, "region_code", "region_name"),
    EntityBinding(EntityType.AREA, DimArea, "area_code", "area_name"),
    EntityBinding(EntityType.UNIT, DimUnit, "unit_code", "unit_name"),
    EntityBinding(EntityType.TERRITORY, DimTerritory, "territory_code", "territory_name"),
    EntityBinding(EntityType.SUB_TERRITORY, DimSubTerritory, "sub_territory_code",
                  "sub_territory_name"),
    EntityBinding(EntityType.CUSTOMER, DimCustomer, "customer_code", "customer_name"),
    EntityBinding(EntityType.SALES_FORCE, DimSalesForce, "sales_force_code",
                  "sales_force_name"),
    # The one item binding since revision 0022. A material resolves by code *and*
    # by description, and by its brand and group names as further names, so
    # "Shobuj sales" finds the materials of that brand and "Shobuj stock" finds
    # the same ones. Resolving by code is what lets "stock for material 14001"
    # narrow to that material instead of being read as an ordinary number.
    EntityBinding(EntityType.MATERIAL, DimMaterial, "material_code",
                  "material_description",
                  ("material_brand", "material_group_name")),
)

BINDING_BY_TYPE = {b.entity_type: b for b in ENTITY_BINDINGS}


@dataclass
class MasterEntry:
    entity_type: EntityType
    code: str
    label: str
    names: tuple[str, ...]


@dataclass
class EntityIndex:
    """In-memory index of every master record, built once per request.

    Master data is small (thousands of rows) and every question needs it, so one
    load per request beats a query per candidate token.
    """

    entries: list[MasterEntry] = field(default_factory=list)
    by_code: dict[str, list[MasterEntry]] = field(default_factory=dict)
    by_name: dict[str, list[MasterEntry]] = field(default_factory=dict)

    @classmethod
    def load(cls, session: Session,
             entity_types: Iterable[EntityType] | None = None) -> "EntityIndex":
        index = cls()
        wanted = set(entity_types) if entity_types else None
        for binding in ENTITY_BINDINGS:
            if wanted is not None and binding.entity_type not in wanted:
                continue
            columns = [getattr(binding.model, binding.code_field)]
            name_fields = [f for f in
                           (binding.name_field, *binding.extra_name_fields) if f]
            columns += [getattr(binding.model, f) for f in name_fields]
            for row in session.execute(select(*columns)).all():
                code = row[0]
                if code is None:
                    continue
                names = tuple(str(v) for v in row[1:] if v)
                index.add(MasterEntry(
                    entity_type=binding.entity_type,
                    code=str(code),
                    label=names[0] if names else str(code),
                    names=names,
                ))
        return index

    def add(self, entry: MasterEntry) -> None:
        self.entries.append(entry)
        self.by_code.setdefault(entry.code.casefold(), []).append(entry)
        for name in entry.names:
            self.by_name.setdefault(name.casefold(), []).append(entry)

    def __len__(self) -> int:
        return len(self.entries)


class EntityResolver:
    """Resolves free text against the master data."""

    def __init__(self, session: Session, index: EntityIndex | None = None) -> None:
        self.session = session
        self.index = index if index is not None else EntityIndex.load(session)

    # -- single-term resolution ---------------------------------------------

    def resolve_term(self, term: str,
                     entity_type: EntityType | None = None) -> ResolvedEntity:
        """Resolve one term, raising rather than guessing.

        :raises EntityNotFoundError: nothing matched.
        :raises AmbiguousEntityError: several distinct records matched.
        """
        candidates = self.candidates(term, entity_type)
        if not candidates:
            raise EntityNotFoundError(term, entity_type.value if entity_type else None)
        if len(candidates) > 1:
            raise AmbiguousEntityError(term, [
                {"entity_type": c.entity_type.value, "code": c.code, "label": c.label}
                for c in candidates
            ])
        entry = candidates[0]
        return ResolvedEntity(
            entity_type=entry.entity_type,
            code=entry.code,
            label=entry.label,
            term=term,
            match=self._match_kind(term, entry),
        )

    def candidates(self, term: str,
                   entity_type: EntityType | None = None) -> list[MasterEntry]:
        """Distinct master records a term could refer to."""
        needle = term.strip().casefold()
        if not needle:
            return []

        def keep(entries: Sequence[MasterEntry]) -> list[MasterEntry]:
            if entity_type is None:
                return list(entries)
            return [e for e in entries if e.entity_type == entity_type]

        exact_code = keep(self.index.by_code.get(needle, []))
        if exact_code:
            return _dedupe(exact_code)

        exact_name = keep(self.index.by_name.get(needle, []))
        if exact_name:
            return _dedupe(exact_name)

        if len(needle) < 3:
            return []
        partial = [
            entry for entry in keep(self.index.entries)
            if any(_starts_a_word(needle, name) for name in entry.names)
        ]
        return _dedupe(partial)

    @staticmethod
    def _match_kind(term: str, entry: MasterEntry) -> str:
        needle = term.strip().casefold()
        if entry.code.casefold() == needle:
            return "code"
        if any(name.casefold() == needle for name in entry.names):
            return "exact_name"
        return "partial_name"

    # -- whole-message resolution -------------------------------------------

    def resolve_message(self, message: str, *, max_entities: int = 6,
                        raise_on_ambiguous: bool = True,
                        exclude_types: Collection[EntityType] = (),
                        ) -> list[ResolvedEntity]:
        """Find every master record mentioned in a question.

        Longer phrases are tried before single words, so "Dhaka North" resolves
        as one territory rather than as the "Dhaka" region.

        ``exclude_types`` keeps an entity type out of a question it cannot
        belong to. It exists for material codes, which are bare numbers: without
        it, the "20" in "top 20 customers" could resolve to a material and
        attach a filter to a sales report that has no materials in it. The
        caller knows what the question is about; this method does not.
        """
        excluded = set(exclude_types)
        tokens = [t for t in TOKEN_RE.findall(message)]
        resolved: list[ResolvedEntity] = []
        seen: set[tuple[str, str]] = set()
        consumed: set[int] = set()

        for size in (3, 2, 1):
            for start in range(len(tokens) - size + 1):
                if any(i in consumed for i in range(start, start + size)):
                    continue
                phrase_tokens = tokens[start:start + size]
                phrase = " ".join(phrase_tokens)
                if size == 1 and phrase.casefold() in STOPWORDS:
                    continue
                if size == 1 and len(phrase) < 2:
                    continue

                candidates = [c for c in self.candidates(phrase)
                              if c.entity_type not in excluded]
                if not candidates:
                    continue
                if len(candidates) > 1:
                    if raise_on_ambiguous and self._is_confusable(phrase, candidates):
                        raise AmbiguousEntityError(phrase, [
                            {"entity_type": c.entity_type.value, "code": c.code,
                             "label": c.label}
                            for c in candidates
                        ])
                    continue

                entry = candidates[0]
                key = (entry.entity_type.value, entry.code)
                if key not in seen:
                    seen.add(key)
                    resolved.append(ResolvedEntity(
                        entity_type=entry.entity_type,
                        code=entry.code,
                        label=entry.label,
                        term=phrase,
                        match=self._match_kind(phrase, entry),
                    ))
                consumed.update(range(start, start + size))
                if len(resolved) >= max_entities:
                    return resolved
        return resolved

    @staticmethod
    def _is_confusable(phrase: str, candidates: Sequence[MasterEntry]) -> bool:
        """Only ask for clarification when the term is a real, exact collision.

        A partial match hitting several records is weak evidence and is simply
        ignored; an exact code or name hitting two different entity types is the
        case worth interrupting the user for.
        """
        needle = phrase.strip().casefold()
        exact = [
            c for c in candidates
            if c.code.casefold() == needle or any(n.casefold() == needle for n in c.names)
        ]
        return len({c.entity_type for c in exact}) > 1 or len(exact) > 1


def _starts_a_word(needle: str, name: str) -> bool:
    """True when ``needle`` begins a word of ``name``.

    A partial match has to start somewhere a reader would recognise. Matching
    anywhere inside the string made ``ache`` match "S**ache**t" and turned a
    question containing that fragment into a report about one material — the
    fragment was never a name the user typed, just letters that happened to
    line up. Anchoring to a word start keeps the useful case ("Dha" finding
    Dhaka) and drops the coincidental one.
    """
    for word in _WORD_RE.findall(name.casefold()):
        if word.startswith(needle):
            return True
    return False


def _dedupe(entries: Sequence[MasterEntry]) -> list[MasterEntry]:
    seen: set[tuple[str, str]] = set()
    unique: list[MasterEntry] = []
    for entry in entries:
        key = (entry.entity_type.value, entry.code)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


__all__ = [
    "EntityResolver",
    "EntityIndex",
    "MasterEntry",
    "EntityBinding",
    "ENTITY_BINDINGS",
    "BINDING_BY_TYPE",
    "STOPWORDS",
]
