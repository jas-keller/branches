from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import math
import re
import unicodedata

from .site_profile import profile
from .stories import STORY_PERSON_IDS, SHOWCASE_PERSON_IDS


MAX_GENERATIONS = 10

# Two map points closer than this are the same location as far as a reader is
# concerned. A gazetteer that stores a county and its county seat separately
# would otherwise land them a few km apart and render one smudged dot carrying
# two colliding labels.
MAP_CLUSTER_KM = 15

EVENT_TAGS = {"BIRT", "DEAT", "BURI", "RESI", "IMMI", "EMIG", "MARR", "BAPM", "EVEN"}
EVENT_LABELS = {
    "BIRT": "Born", "DEAT": "Died", "BURI": "Buried", "RESI": "Lived",
    "IMMI": "Arrived", "EMIG": "Departed", "MARR": "Married",
    "BAPM": "Baptized", "ARRIVAL": "Arrived", "DEPARTURE": "Departed", "EVEN": "Event",
}

# Ancestry exports mark some names with a trailing asterisk and record a few
# surnames in full caps. Neither is meaningful genealogy, so normalize both for
# display while leaving genuine roman-numeral suffixes (II, III) alone.
def is_user_tree_source(title):
    """True for the submitted-tree citations that carry no independent evidence.

    Ancestry exports these as "Ancestry Family Trees" (older files use
    "Ancestry.com Family Trees"), so match on both parts rather than one fixed
    string — matching the wrong one silently marks the whole archive Documented.
    """
    lowered = (title or "").casefold()
    return "ancestry" in lowered and "family tree" in lowered


ROMAN_NUMERAL = re.compile(r"^[IVXLCDM]+$")


def tidy_name(value):
    if not value:
        return ""
    words = []
    for word in value.replace("*", "").split():
        if word.isupper() and len(word) > 1 and not ROMAN_NUMERAL.match(word):
            word = word.capitalize()
        words.append(word)
    name = " ".join(words)
    for pattern, replacement in profile.NAME_CORRECTIONS:
        name = re.sub(pattern, replacement, name, flags=re.I)
    return name


def display_place(value):
    """Polish known import spellings for display, retaining raw event places."""
    for pattern, replacement in profile.PLACE_CORRECTIONS:
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value


def normalized_name(value):
    return re.sub(r"\s+", " ", tidy_name(value).replace("/", " ")).casefold().strip()


def name_tokens(value):
    folded = unicodedata.normalize("NFKD", value.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", folded)


NAME_STOP_WORDS = set("grandma grandpa grandmother grandfather aunt uncle great the".split())


@dataclass
class GedcomDate:
    raw: str = ""
    qualifier: str = "EXACT"
    start_year: int | None = None
    end_year: int | None = None
    precision: str = "unknown"
    reference_year: int | None = None

    @property
    def year(self):
        """Representative year retained for existing displays and aggregates."""
        return self.reference_year


DATE_YEAR = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)")


def parse_gedcom_date(value):
    """Parse GEDCOM date semantics without discarding the original value.

    Bounds are deliberately open for BEF/AFT. ABT/CAL/EST retain their qualifier
    and representative year; they are not assigned an invented uncertainty span.
    """
    raw = (value or "").strip()
    upper = raw.upper()
    years = [int(match) for match in DATE_YEAR.findall(upper)]
    qualifier = "EXACT"
    start_year = end_year = years[0] if years else None
    if upper.startswith("BET ") and " AND " in upper:
        qualifier = "BET"
        if len(years) >= 2:
            start_year, end_year = min(years[0], years[1]), max(years[0], years[1])
    elif upper.startswith("FROM ") and " TO " in upper:
        qualifier = "FROM_TO"
        if len(years) >= 2:
            start_year, end_year = min(years[0], years[1]), max(years[0], years[1])
    elif upper.startswith("BEF "):
        qualifier = "BEF"
        start_year, end_year = None, years[0] - 1 if years else None
    elif upper.startswith("AFT "):
        qualifier = "AFT"
        start_year, end_year = years[0] + 1 if years else None, None
    elif upper.startswith("ABT "):
        qualifier = "ABT"
    elif upper.startswith("CAL "):
        qualifier = "CAL"
    elif upper.startswith("EST "):
        qualifier = "EST"
    elif upper.startswith("INT "):
        qualifier = "INT"

    precision = "unknown"
    if years:
        if qualifier in ("BET", "FROM_TO"):
            precision = "range"
        elif re.search(r"\b\d{1,2}\s+[A-Z]{3}\s+\d{4}\b", upper):
            precision = "day"
        elif re.search(r"\b[A-Z]{3}\s+\d{4}\b", upper):
            precision = "month"
        else:
            precision = "year"
    return GedcomDate(
        raw=raw,
        qualifier=qualifier,
        start_year=start_year,
        end_year=end_year,
        precision=precision,
        reference_year=years[0] if years else None,
    )


@dataclass
class Event:
    kind: str
    date: str = ""
    place: str = ""
    details: str = ""
    citations: list["Citation"] = field(default_factory=list)

    @property
    def year(self):
        return self.date_value.year

    @property
    def date_value(self):
        return parse_gedcom_date(self.date)


@dataclass
class ParentFamilyLink:
    family_id: str
    pedigree: str = ""
    status: str = ""


@dataclass
class Person:
    id: str
    name: str = "Unknown"
    sex: str = ""
    events: list[Event] = field(default_factory=list)
    parent_family_links: list[ParentFamilyLink] = field(default_factory=list)
    spouse_families: list[str] = field(default_factory=list)
    citations: list["Citation"] = field(default_factory=list)
    media: list["Media"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    curated_name_forms: list[dict] = field(default_factory=list, repr=False)

    @property
    def child_families(self):
        return [link.family_id for link in self.parent_family_links]

    @property
    def child_family(self):
        """Compatibility primary while ambiguous links await editorial review.

        Ancestry places its preferred/current FAMC last in this export. Existing
        public traversal retains that behavior, while ``parent_family_links``
        losslessly preserves every assertion for validation and future staging.
        """
        return self.child_families[-1] if self.parent_family_links else None

    def event(self, kind):
        return next((event for event in self.events if event.kind == kind), None)

    @property
    def birth_year(self):
        event = self.event("BIRT")
        return event.year if event else None

    @property
    def death_year(self):
        event = self.event("DEAT")
        return event.year if event else None

    @property
    def surname(self):
        match = re.search(r"/([^/]+)/", self.name)
        return tidy_name(match.group(1)) if match else ""

    @property
    def clean_name(self):
        return tidy_name(self.name.replace("/", " "))

    @property
    def formal_surname(self):
        return tidy_name(re.sub(r"\([^)]*\)", "", self.surname))

    @property
    def name_forms(self):
        formal = tidy_name(re.sub(r'\([^)]*\)|["“][^"”]*["”]', "", self.name).replace("/", " "))
        forms = [{"type": "formal", "text": formal}]
        forms.extend({"type": "informal", "text": tidy_name(text)}
                     for text in re.findall(r'["“]([^"”]+)["”]', self.name))
        # In Ancestry exports parentheses can be inside or outside the slashes.
        surname_start = self.name.find("/")
        for match in re.finditer(r"\(([^)]+)\)", self.name):
            before = self.name[max(surname_start, 0):match.start()].replace("/", "").strip()
            kind = "married" if surname_start >= 0 and before else "variant"
            forms.append({"type": kind, "text": tidy_name(match.group(1))})
        if self.curated_name_forms and not self.is_living:
            curated_types = {row["type"] for row in self.curated_name_forms}
            forms = [row for row in forms if row["type"] not in curated_types]
            forms.extend(self.curated_name_forms)
        return forms

    @property
    def formal_name(self):
        return next(row["text"] for row in self.name_forms if row["type"] == "formal")

    @property
    def display_name(self):
        given = next((row["text"] for row in self.name_forms if row["type"] == "informal"),
                     self.formal_name.split()[0] if self.formal_name else "Unknown")
        return tidy_name(f"{given} {self.formal_surname}")

    @property
    def name_notes(self):
        labels = {"married": "married name", "variant": "also spelled", "maiden": "birth surname"}
        return " · ".join(f"{labels[row['type']]} {row['text']}" for row in self.name_forms if row["type"] in labels)

    @property
    def is_living(self):
        if self.event("DEAT"):
            return False
        # A missing death event is common in historical trees. With a known birth
        # year, treat someone as historical once they would be at least 100 today.
        # Unknown birth years remain private because there is no safe age basis.
        return self.birth_year is None or self.birth_year > date.today().year - 100


@dataclass
class Family:
    id: str
    husband: str | None = None
    wife: str | None = None
    children: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    citations: list["Citation"] = field(default_factory=list)


@dataclass
class Citation:
    source_id: str = ""
    page: str = ""
    apid: str = ""
    text: str = ""


@dataclass
class Source:
    id: str
    title: str = ""
    author: str = ""
    publication: str = ""
    apid: str = ""


@dataclass
class Media:
    file: str = ""
    format: str = ""
    title: str = ""


class GedcomRepository:
    def __init__(self, path, show_living=False):
        self.path = Path(path)
        self.show_living = show_living
        self.people: dict[str, Person] = {}
        self.families: dict[str, Family] = {}
        self.sources: dict[str, Source] = {}
        self._branch_context_cache = {}
        self._anchor_tree_cache = {}
        self._anchor_ancestor_cache = {}
        self._anchor_people_cache = None
        self._people_index_cache = None
        self._frozen = False
        self._stream_cache = {}
        self._research_review_cache = None
        self._parse()
        self._name_search_index = None

    def _parse(self):
        if not self.path.exists():
            raise FileNotFoundError(f"GEDCOM file not found: {self.path}")
        lines = self.path.read_text(encoding="utf-8-sig").splitlines()
        current = None
        current_type = None
        current_event = None
        current_citation = None
        citation_level = None
        current_media = None
        current_parent_link = None
        for raw in lines:
            match = re.match(r"^(\d+)\s+(?:@([^@]+)@\s+)?(\S+)(?:\s+(.*))?$", raw)
            if not match:
                continue
            level, xref, tag, value = int(match.group(1)), match.group(2), match.group(3), match.group(4) or ""
            value = value.strip()
            if level == 0:
                current_event = None
                current_citation = None
                citation_level = None
                current_media = None
                current_parent_link = None
                if xref and tag == "INDI":
                    current = self.people.setdefault(xref, Person(xref))
                    current_type = "INDI"
                elif xref and tag == "FAM":
                    current = self.families.setdefault(xref, Family(xref))
                    current_type = "FAM"
                elif xref and tag == "SOUR":
                    current = self.sources.setdefault(xref, Source(xref))
                    current_type = "SOUR"
                else:
                    current = None
                    current_type = None
                continue
            if current is None:
                continue
            if level <= 1 and not (level == 1 and tag == "FAMC"):
                current_parent_link = None
            if current_media and level <= 1 and tag != "OBJE":
                current_media = None
            if current_citation and citation_level is not None and level <= citation_level:
                current_citation = None
                citation_level = None
            if level == 1 and tag == "OBJE" and current_type == "INDI":
                current_event = None
                current_media = Media()
                current.media.append(current_media)
                continue
            if current_media and level >= 2:
                if tag == "FILE": current_media.file = value
                elif tag == "FORM": current_media.format = value
                elif tag in ("TITL", "TITLE"): current_media.title = value
                continue
            if tag == "SOUR" and current_type in ("INDI", "FAM"):
                current_citation = Citation(value.strip("@"))
                citation_level = level
                if current_event:
                    current_event.citations.append(current_citation)
                else:
                    current.citations.append(current_citation)
                continue
            if current_citation and level > (citation_level or 0):
                if tag == "PAGE": current_citation.page = value
                elif tag == "_APID": current_citation.apid = value
                elif tag == "TEXT": current_citation.text = value
                continue
            if current_type == "SOUR":
                if tag in ("TITL", "TITLE"): current.title = value
                elif tag == "AUTH": current.author = value
                elif tag == "PUBL": current.publication = value
                elif tag == "_APID": current.apid = value
                continue
            if current_type == "INDI" and level == 1 and tag == "FAMC":
                current_event = None
                current_parent_link = ParentFamilyLink(value.strip("@"))
                current.parent_family_links.append(current_parent_link)
                continue
            if current_parent_link and level == 2:
                if tag == "PEDI": current_parent_link.pedigree = value
                elif tag == "STAT": current_parent_link.status = value
                continue
            if level == 1 and tag in EVENT_TAGS:
                current_event = Event(tag)
                current.events.append(current_event)
            elif level == 2 and current_event and tag == "DATE":
                current_event.date = value
            elif level == 2 and current_event and tag == "PLAC":
                current_event.place = value
            elif level == 2 and current_event and tag == "TYPE":
                current_event.kind = value.upper()
            elif level == 2 and current_event and tag in ("NOTE", "DESC"):
                current_event.details = value
            elif level == 1:
                current_event = None
                ref = value.strip("@")
                if current_type == "INDI":
                    if tag == "NAME": current.name = value
                    elif tag == "SEX": current.sex = value
                    elif tag == "FAMS": current.spouse_families.append(ref)
                    elif tag == "NOTE": current.notes.append(value)
                elif current_type == "FAM":
                    if tag == "HUSB": current.husband = ref
                    elif tag == "WIFE": current.wife = ref
                    elif tag == "CHIL": current.children.append(ref)

    def visible(self, person):
        return self.show_living or not person.is_living

    def label(self, person):
        return person.display_name if self.visible(person) else "Living person"

    def _person_by_name(self, name):
        target = normalized_name(name)
        return next((person for person in self.people.values()
                     if normalized_name(person.clean_name) == target), None)

    def _reference_person(self):
        """Best-effort reference person for branch orientation.

        New Ancestry exports can rewrite every ID, so the named branch anchors
        in the site profile are the durable source of truth for public
        navigation. The profile's REFERENCE_PERSON_ID is only a fallback.
        """
        reference_id = getattr(profile, "REFERENCE_PERSON_ID", None)
        return self.people.get(reference_id) if reference_id else None

    def _legacy_grandparent_anchors(self):
        reference = self._reference_person()
        reference_family = self.families.get(reference.child_family) if reference else None
        grandparents = []
        for parent_id in (reference_family.husband, reference_family.wife) if reference_family else ():
            parent = self.people.get(parent_id)
            parent_family = self.families.get(parent.child_family) if parent and parent.child_family else None
            if parent_family:
                grandparents.extend(
                    self.people[person_id] for person_id in (parent_family.husband, parent_family.wife)
                    if person_id in self.people
                )
        return grandparents

    def _grandparent_anchors(self):
        # Matching a name scans the whole tree, and this runs once per person on
        # directory pages, so remember the result for the current anchor list.
        key = tuple(profile.BRANCH_ANCHORS)
        if self._anchor_people_cache and self._anchor_people_cache[0] == key:
            return list(self._anchor_people_cache[1])
        named = [person for name in key if (person := self._person_by_name(name))]
        anchors = named or self._legacy_grandparent_anchors()
        self._anchor_people_cache = (key, tuple(anchors))
        return list(anchors)

    def branch_slots(self):
        """Stable colour/layout slot for each branch label, in anchor order."""
        slots = {}
        for anchor in self._grandparent_anchors():
            label = f"{anchor.formal_surname or anchor.display_name} branch"
            slots.setdefault(label, len(slots))
        return slots

    def default_root(self):
        # Begin at the nearest public grandparent generation, not at an arbitrary
        # early record. This gives relatives a familiar point of orientation while
        # keeping the living reference person and their parents private.
        primary = profile.PRIMARY_SURNAME.casefold()
        grandparents = [person for person in self._grandparent_anchors() if self.visible(person)]
        if grandparents:
            return next((person for person in grandparents if person.formal_surname.casefold() == primary), grandparents[0])
        namesakes = [p for p in self.people.values() if p.surname.casefold() == primary and self.visible(p)]
        return min(namesakes, key=lambda p: p.birth_year or 9999, default=next(iter(self.people.values())))

    def starting_branches(self):
        """Return familiar, public entry points for the anchored branches."""
        anchors = self._grandparent_anchors()
        branches = []
        for anchor in anchors:
            summary = self.person_summary(anchor) if self.visible(anchor) else {
                **self.person_summary(anchor, force_visible=True),
                "name": anchor.display_name,
            }
            branches.append({
                "label": f"{anchor.formal_surname or 'Family'} branch",
                "surname": anchor.formal_surname,
                "root": summary,
            })
        primary = profile.PRIMARY_SURNAME.casefold()
        branches.sort(key=lambda item: (item["surname"].casefold() != primary, item["label"]))
        slots = self.branch_slots()
        for branch in branches:
            branch["slot"] = slots.get(branch["label"], 0)
        return branches

    def person_summary(self, person, force_visible=False):
        if not force_visible and not self.visible(person):
            return {"id": "", "name": "Living person", "surname": "", "years": "",
                    "birthPlace": "", "deathPlace": "", "living": True}
        birth = person.event("BIRT")
        death = person.event("DEAT")
        years = "–".join(str(x) if x else "?" for x in (person.birth_year, person.death_year))
        return {
            "id": person.id,
            "name": self.label(person),
            "formalName": person.formal_name if self.visible(person) else "",
            "nameNotes": person.name_notes if self.visible(person) else "",
            "surname": person.formal_surname if self.visible(person) else "",
            "years": years,
            "birthPlace": display_place(birth.place) if birth and self.visible(person) else "",
            "deathPlace": display_place(death.place) if death and self.visible(person) else "",
            "living": person.is_living,
            "researchReview": self.research_review(person.id),
            "hasStory": person.id in STORY_PERSON_IDS,
        }

    def research_review(self, person_id):
        """Whether automated checks found an assertion worth reviewing."""
        if self._research_review_cache is None:
            # Validate lazily so parsing and non-person views pay no extra cost.
            from .validation import validate_repository

            report = validate_repository(self)
            self._research_review_cache = {
                flagged_id
                for finding in report.findings
                if finding.severity in ("fatal", "error", "warning")
                for flagged_id in finding.people
            }
        return person_id in self._research_review_cache

    def apply_name_forms(self, forms_by_id):
        changed = False
        for person in self.people.values():
            forms = forms_by_id.get(person.id, []) if not person.is_living else []
            if person.curated_name_forms != forms:
                person.curated_name_forms = forms
                changed = True
        if changed:
            self._name_search_index = None
            self._branch_context_cache.clear()
            self._anchor_tree_cache.clear()
            self._anchor_ancestor_cache.clear()
            self._anchor_people_cache = None
            self._people_index_cache = None
            self._stream_cache.clear()

    def _search_names(self):
        if self._name_search_index is None:
            self._name_search_index = {}
            for person in self.people.values():
                if not self.visible(person):
                    continue
                forms = person.name_forms
                tokens = set(name_tokens(" ".join(row["text"] for row in forms)))
                # Places were explicitly included in the forgiving-search brief.
                tokens.update(name_tokens(" ".join(event.place for event in person.events)))
                self._name_search_index[person.id] = (tokens, forms)
        return self._name_search_index

    def search(self, query, limit=12, include_surnames=True):
        query_tokens = [token for token in name_tokens(query) if token not in NAME_STOP_WORDS]
        if not query_tokens:
            return []
        matches = []
        for person_id, (tokens, forms) in self._search_names().items():
            if not all(any(token.startswith(q) for token in tokens) for q in query_tokens):
                continue
            person = self.people[person_id]
            tier = 3
            if query_tokens == name_tokens(person.display_name):
                tier = 0
            elif any(row["type"] == "informal" and any(
                    token.startswith(query_tokens[0]) for token in name_tokens(row["text"])) for row in forms):
                tier = 1
            elif all(any(token.startswith(q) for token in name_tokens(person.formal_name)) for q in query_tokens):
                tier = 2
            matches.append((tier, person.birth_year or 9999, person.display_name, person))
        matches.sort(key=lambda row: row[:3])
        people = [dict(self.person_summary(row[3]), kind="person") for row in matches[:limit]]
        return self.surname_matches(query) + people if include_surnames else people

    def surname_matches(self, query, limit=3):
        query_tokens = [token for token in name_tokens(query) if token not in NAME_STOP_WORDS]
        if not query_tokens:
            return []
        aliases = defaultdict(set)
        for person in self.people.values():
            if self.visible(person):
                aliases[person.formal_surname].update(name_tokens(person.formal_surname))
                for row in person.name_forms:
                    if row["type"] in {"variant", "married", "maiden"}:
                        aliases[person.formal_surname].update(name_tokens(row["text"]))
        groups = []
        for group in self.people_index()["groups"]:
            if not all(any(token.startswith(q) for token in aliases[group["surname"]]) for q in query_tokens):
                continue
            groups.append({"kind": "surname", "name": group["surname"], "slug": group["slug"],
                           "count": group["count"], "span": group["span"]})
        groups.sort(key=lambda item: (name_tokens(item["name"]) != query_tokens, -item["count"]))
        return groups[:limit]

    def tree(self, root_id=None, direction="descendants", generations=6):
        root = self.people.get(root_id) or self.default_root()
        orientation_root = root.id in {person.id for person in self._grandparent_anchors()}
        root_summary = self.person_summary(root, force_visible=orientation_root)
        if orientation_root and not self.visible(root):
            root_summary["name"] = root.display_name
        generations = max(1, min(int(generations), MAX_GENERATIONS))
        depth = {root.id: 0}
        links = set()
        queue = deque([root.id])
        # Walk to the hard cap once, then trim. The full walk is what lets us tell
        # the client how deep this branch actually goes, so the generations control
        # can stop offering steps that cannot change anything.
        while queue:
            person_id = queue.popleft()
            person = self.people.get(person_id)
            if not person or depth[person_id] >= MAX_GENERATIONS:
                continue
            neighbors = []
            if direction == "ancestors":
                if person.child_family:
                    family = self.families.get(person.child_family)
                    if family:
                        neighbors = [x for x in (family.husband, family.wife) if x]
                        for parent_id in neighbors:
                            links.add((parent_id, person_id, "parent"))
            else:
                for family_id in person.spouse_families:
                    family = self.families.get(family_id)
                    if not family: continue
                    spouse = family.wife if family.husband == person_id else family.husband
                    if spouse and spouse in self.people and self.visible(self.people[spouse]):
                        if spouse not in depth:
                            depth[spouse] = depth[person_id]
                        links.add((person_id, spouse, "spouse"))
                    for child in family.children:
                        links.add((person_id, child, "parent"))
                        neighbors.append(child)
            for neighbor in neighbors:
                target = self.people.get(neighbor)
                if target and self.visible(target) and neighbor not in depth:
                    depth[neighbor] = depth[person_id] + 1
                    queue.append(neighbor)
        reachable = {
            pid: gen for pid, gen in depth.items()
            if self.visible(self.people[pid]) or (pid == root.id and orientation_root)
        }
        available = max(reachable.values(), default=0)
        kept = {pid for pid, gen in reachable.items() if gen <= generations}
        nodes = [dict(
            root_summary if pid == root.id else self.person_summary(self.people[pid]),
            generation=reachable[pid]
        ) for pid in kept]
        edges = [{"source": a, "target": b, "kind": kind} for a, b, kind in links if a in kept and b in kept]
        earlier = []
        if root.child_family:
            family = self.families.get(root.child_family)
            if family:
                earlier = [
                    self.person_summary(self.people[person_id])
                    for person_id in (family.husband, family.wife)
                    if person_id in self.people and self.visible(self.people[person_id])
                ]
        later_ids = []
        for family_id in root.spouse_families:
            family = self.families.get(family_id)
            if family:
                later_ids.extend(family.children)
        later = [
            self.person_summary(self.people[person_id])
            for person_id in dict.fromkeys(later_ids)
            if person_id in self.people and self.visible(self.people[person_id])
        ]
        return {
            "root": root.id,
            "rootPerson": root_summary,
            "rootRecordAvailable": self.visible(root),
            "direction": direction,
            "generations": generations,
            "availableDepth": available,
            "rootConnections": {"earlier": earlier, "later": later},
            "maxGenerations": MAX_GENERATIONS,
            "nodes": nodes,
            "links": edges,
        }

    def person_detail(self, person_id):
        person = self.people.get(person_id)
        if not person or not self.visible(person):
            return None
        data = self.person_summary(person)
        data["branch"] = self.branch_context(person.id)
        all_citations = person.citations + [citation for event in person.events for citation in event.citations]
        data["evidence"] = self.evidence_summary(all_citations)
        values_by_kind = defaultdict(set)
        for event in person.events:
            values_by_kind[event.kind].add((event.date, event.place))
        data["events"] = []
        for event in person.events:
            if not (event.date or event.place or event.details):
                continue
            data["events"].append({
                "kind": event.kind,
                "label": EVENT_LABELS.get(event.kind, event.kind.title()),
                "date": event.date,
                "year": event.year,
                "dateQualifier": event.date_value.qualifier,
                "dateStartYear": event.date_value.start_year,
                "dateEndYear": event.date_value.end_year,
                "datePrecision": event.date_value.precision,
                "place": display_place(event.place),
                "placeSlug": slugify(locate(event.place)[2]) if event.place and locate(event.place) else "",
                "details": event.details,
                "conflict": len(values_by_kind[event.kind]) > 1,
                "evidence": self.evidence_summary(event.citations),
                "citations": [self.citation_detail(citation) for citation in event.citations],
            })
        relatives = []
        if person.child_family and (family := self.families.get(person.child_family)):
            for role, pid in (("Father", family.husband), ("Mother", family.wife)):
                if pid and pid in self.people:
                    relatives.append({"role": role, **self.person_summary(self.people[pid])})
            for sibling_id in family.children:
                if sibling_id != person.id and sibling_id in self.people:
                    relatives.append({"role": "Sibling", **self.person_summary(self.people[sibling_id])})
        for family_id in person.spouse_families:
            if family := self.families.get(family_id):
                spouse_id = family.wife if family.husband == person.id else family.husband
                if spouse_id and spouse_id in self.people:
                    relatives.append({"role": "Spouse", **self.person_summary(self.people[spouse_id])})
                for child_id in family.children:
                    if child_id in self.people:
                        relatives.append({"role": "Child", **self.person_summary(self.people[child_id])})
        data["relatives"] = relatives
        locatable = [event for event in person.events if event.place and locate(event.place)]
        data["lifePath"] = [{
            "label": EVENT_LABELS.get(event.kind, event.kind.title()), "date": event.date,
            "place": display_place(event.place), "lat": locate(event.place)[0], "lng": locate(event.place)[1],
        } for event in locatable]
        # A map of one dot tells the visitor nothing the chronology does not already
        # say, so the page only gives it room when it can actually show movement.
        data["distinctPlaces"] = len({(round(point["lat"], 3), round(point["lng"], 3))
                                      for point in data["lifePath"]})
        birth = person.event("BIRT")
        peers = []
        if birth and birth.year and birth.place and locate(birth.place):
            normalized_birthplace = locate(birth.place)[2]
            for candidate in self.people.values():
                candidate_birth = candidate.event("BIRT")
                if candidate.id == person.id or not self.visible(candidate) or not candidate_birth or not candidate_birth.year or not candidate_birth.place:
                    continue
                located = locate(candidate_birth.place)
                if located and located[2] == normalized_birthplace and abs(candidate_birth.year - birth.year) <= 10:
                    peers.append(self.person_summary(candidate))
        data["contemporaries"] = sorted(peers, key=lambda item: item["years"])[:8]
        data["media"] = [{"title": item.title or "External media", "url": item.file}
                         for item in person.media if item.file.startswith(("http://", "https://"))]
        gaps = []
        if not person.event("BIRT"): gaps.append("Birth date or birthplace")
        if not person.event("DEAT") and not person.is_living: gaps.append("Death date or place")
        if not all_citations: gaps.append("Supporting records")
        if person.event("BIRT") and not person.event("BIRT").place: gaps.append("Birthplace")
        data["researchGaps"] = gaps
        return data

    def citation_detail(self, citation):
        source = self.sources.get(citation.source_id)
        return {
            "sourceId": citation.source_id,
            "title": source.title if source else "Unidentified source",
            "page": citation.page,
            "recordId": citation.apid,
            "text": citation.text,
        }

    def evidence_summary(self, citations):
        details = [self.citation_detail(citation) for citation in citations]
        substantive = [item for item in details if not is_user_tree_source(item["title"])]
        if substantive:
            return {"level": "documented", "label": "Record-supported", "count": len(substantive),
                    "explanation": "Supported by one or more records beyond the imported family tree."}
        if details:
            return {"level": "tree", "label": "Family tree record", "count": len(details),
                    "explanation": "Present in the imported tree; an independent record has not yet been linked here."}
        return {"level": "inferred", "label": "Inferred", "count": 0,
                "explanation": "Derived from family relationships or context and not directly sourced."}

    def places(self):
        grouped = defaultdict(lambda: {"count": 0, "years": [], "people": set(), "kinds": Counter()})
        for person in self.people.values():
            if not self.visible(person): continue
            for event in person.events:
                if not event.place: continue
                coords = locate(event.place)
                if not coords: continue
                key = coords[2]
                item = grouped[key]
                item["lat"], item["lng"] = coords[0], coords[1]
                item["count"] += 1
                item["people"].add(person.id)
                item["kinds"][event.kind] += 1
                if event.year: item["years"].append(event.year)
        return [{
            "name": name, "lat": values["lat"], "lng": values["lng"], "count": values["count"],
            "slug": slugify(name),
            "people": len(values["people"]), "firstYear": min(values["years"], default=None),
            "lastYear": max(values["years"], default=None), "kinds": dict(values["kinds"]),
        } for name, values in grouped.items()]

    def place_detail(self, place_slug):
        place = next((item for item in self.places() if item["slug"] == place_slug), None)
        if not place:
            return None
        events = []
        decades = Counter()
        surnames = Counter()
        roles = Counter()
        people = {}
        for person in self.people.values():
            if not self.visible(person):
                continue
            for event in person.events:
                located = locate(event.place) if event.place else None
                if not located or slugify(located[2]) != place_slug:
                    continue
                summary = self.person_summary(person)
                summary["branch"] = self.branch_context(person.id)
                people[person.id] = summary
                surnames[person.formal_surname or "Unknown"] += 1
                roles[EVENT_LABELS.get(event.kind, event.kind.title())] += 1
                if event.year:
                    decades[(event.year // 10) * 10] += 1
                events.append({
                    "person": summary, "kind": event.kind,
                    "label": EVENT_LABELS.get(event.kind, event.kind.title()),
                    "date": event.date, "year": event.year, "rawPlace": event.place,
                    "evidence": self.evidence_summary(event.citations),
                })
        events.sort(key=lambda item: (item["year"] or 9999, item["person"]["name"]))
        place.update({
            "events": events,
            "peopleList": sorted(people.values(), key=lambda item: item["name"]),
            "decades": [{"decade": decade, "count": count} for decade, count in sorted(decades.items())],
            "surnames": surnames.most_common(10),
            "roles": roles.most_common(),
        })
        return place

    def branch_context(self, person_id):
        """Return the modern family branch and a surname-only relationship trail."""
        if person_id in self._branch_context_cache:
            return self._branch_context_cache[person_id]
        reference = self._reference_person()
        anchors = [anchor.id for anchor in self._grandparent_anchors()]
        if not anchors:
            self._branch_context_cache[person_id] = None
            return None

        # The shortest path from the nearest anchor; on a tie the earlier anchor
        # in BRANCH_ANCHORS wins.
        best = None
        for anchor_id in anchors:
            path = self._anchor_path(anchor_id, person_id)
            if path and (best is None or len(path) < len(best)):
                best = path
        if not best:
            self._branch_context_cache[person_id] = None
            return None
        anchor = self.people[best[0]]
        surnames = []
        for node_id in best:
            surname = self.people[node_id].formal_surname
            if surname and (not surnames or surnames[-1].casefold() != surname.casefold()):
                surnames.append(surname)
        if len(surnames) > 4:
            surnames = surnames[:2] + ["…"] + surnames[-1:]
        steps = len(best) - 1
        result = {
            "label": f"{anchor.formal_surname or anchor.display_name} branch",
            "trail": " → ".join(surnames),
            "connections": steps,
            # Distance is meaningless without saying who it is measured from. It is
            # counted from this branch's anchor - one of the named grandparents the
            # archive's branch names come from - not from the reference person.
            "anchor": anchor.display_name,
            "anchorId": anchor.id,
            "reference": reference.display_name if reference else "",
            "distance": f"the start of the {anchor.formal_surname or anchor.display_name} branch" if steps == 0
                        else f"{steps} family {'connection' if steps == 1 else 'connections'} from {anchor.display_name}",
        }
        self._branch_context_cache[person_id] = result
        return result

    # A branch trail is at most this many people long, anchor included.
    _BRANCH_PATH_LIMIT = 24

    def _family_neighbors(self, node_id):
        """Parents, then each family's children and spouse, in GEDCOM order."""
        person = self.people.get(node_id)
        if not person:
            return []
        result = []
        if person.child_family and (family := self.families.get(person.child_family)):
            result.extend(parent_id for parent_id in (family.husband, family.wife) if parent_id)
        for family_id in person.spouse_families:
            if family := self.families.get(family_id):
                result.extend(child_id for child_id in family.children if child_id)
                spouse_id = family.wife if family.husband == node_id else family.husband
                if spouse_id:
                    result.append(spouse_id)
        return result

    def _anchor_tree(self, anchor_id):
        """Breadth-first predecessor map from one anchor, computed once.

        Searching from every anchor for every person made directory pages
        quadratic in tree size. One search per anchor gives every person's
        shortest path, and the first-discovered predecessor reproduces the path
        a per-person search would have found.
        """
        tree = self._anchor_tree_cache.get(anchor_id)
        if tree is not None:
            return tree
        tree = {anchor_id: None}
        depth = {anchor_id: 1}
        queue = deque([anchor_id])
        while queue:
            node_id = queue.popleft()
            if depth[node_id] >= self._BRANCH_PATH_LIMIT:
                continue
            for neighbor in self._family_neighbors(node_id):
                if neighbor not in tree:
                    tree[neighbor] = node_id
                    depth[neighbor] = depth[node_id] + 1
                    queue.append(neighbor)
        self._anchor_tree_cache[anchor_id] = tree
        return tree

    def _anchor_path(self, anchor_id, person_id):
        tree = self._anchor_tree(anchor_id)
        if person_id not in tree:
            return None
        path = [person_id]
        while (previous := tree[path[-1]]) is not None:
            path.append(previous)
        path.reverse()
        return path

    def _anchor_ancestor_depths(self, anchor_id):
        """Generations from an anchor to each of their direct ancestors."""
        depths = self._anchor_ancestor_cache.get(anchor_id)
        if depths is not None:
            return depths
        depths = {anchor_id: 0}
        queue = deque([anchor_id])
        while queue:
            node_id = queue.popleft()
            for parent_id in self._parent_ids(node_id):
                if parent_id not in depths:
                    depths[parent_id] = depths[node_id] + 1
                    queue.append(parent_id)
        self._anchor_ancestor_cache[anchor_id] = depths
        return depths

    def anchor_relationship(self, person_id, branch=None):
        """Describe a person's relationship to the grandparent anchoring a branch.

        Direct ancestors receive standard genealogical language. Collateral
        relatives and other connected people get an intentionally broader label
        rather than a confidently wrong aunt/cousin relationship.
        """
        branch = branch or self.branch_context(person_id)
        if not branch:
            return ""
        anchor = self.people.get(branch["anchorId"])
        person = self.people.get(person_id)
        if not anchor or not person:
            return ""

        familiar_name = anchor.display_name
        generations = self._anchor_ancestor_depths(anchor.id).get(person_id)

        if generations is None:
            return f"Connected to {familiar_name}'s {anchor.formal_surname or 'family'} branch"
        if generations == 0:
            return f"The {anchor.formal_surname or familiar_name} branch begins with {familiar_name}"

        gendered = {
            "M": ("father", "grandfather"),
            "F": ("mother", "grandmother"),
        }.get(person.sex, ("parent", "grandparent"))
        if generations == 1:
            relationship = gendered[0]
        elif generations == 2:
            relationship = gendered[1]
        else:
            greats = generations - 2
            prefix = "great" if greats == 1 else f"{self._ordinal(greats)} great"
            relationship = f"{prefix}-{gendered[1]}"
        return f"{familiar_name}'s {relationship}"

    @staticmethod
    def _ordinal(number):
        if 10 <= number % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
        return f"{number}{suffix}"

    @staticmethod
    def person_geography(person):
        """Return a compact origin-to-later-life label for directory cards."""
        placed_events = [event for event in person.events if event.place]
        if not placed_events:
            return "Place not recorded"

        birth = person.event("BIRT")
        death = person.event("DEAT")
        origin = birth if birth and birth.place else min(
            placed_events, key=lambda event: (event.year is None, event.year or 9999)
        )
        destination = death if death and death.place else max(
            placed_events, key=lambda event: (event.year is not None, event.year or -1)
        )

        def label(event):
            located = locate(event.place)
            return located[2] if located else event.place

        origin_label = label(origin)
        destination_label = label(destination)
        return origin_label if origin_label == destination_label else f"{origin_label} \u2192 {destination_label}"

    def place_hierarchy(self):
        """Shape normalized places into story layers rather than a flat gazetteer."""
        region_meta = profile.PLACE_REGIONS
        geo_meta = profile.PLACE_TIERS

        buckets = defaultdict(lambda: defaultdict(list))
        for item in self.places():
            geo_id, region_id = classify_place(item["name"], item["lng"])
            buckets[geo_id][region_id].append(item)

        people_by_place = defaultdict(set)
        for person in self.people.values():
            if not self.visible(person):
                continue
            for event in person.events:
                located = locate(event.place) if event.place else None
                if located:
                    people_by_place[located[2]].add(person.id)

        def natural_list(values):
            values = list(values)
            if len(values) < 2:
                return "".join(values)
            if len(values) == 2:
                return " and ".join(values)
            return ", ".join(values[:-1]) + f", and {values[-1]}"

        def family_context(items):
            person_ids = set()
            for item in items:
                person_ids.update(people_by_place[item["name"]])
            branch_counts = Counter()
            branch_anchors = {}
            surnames = Counter()
            for person_id in person_ids:
                person = self.people[person_id]
                if person.formal_surname:
                    surnames[person.formal_surname] += 1
                branch = self.branch_context(person_id)
                if branch:
                    branch_counts[branch["label"]] += 1
                    branch_anchors[branch["label"]] = branch["anchor"]
            branches = [
                {"label": label, "anchor": branch_anchors[label], "people": count}
                for label, count in sorted(branch_counts.items(), key=lambda item: (-item[1], item[0]))
            ]
            family_names = [name for name, _ in surnames.most_common(5)]
            if branches:
                branch_names = [branch["label"].removesuffix(" branch") for branch in branches]
                if len(branch_names) == 1:
                    summary = f"In this tree, relatives recorded here connect forward to the {branch_names[0]} branch."
                else:
                    summary = f"In this tree, relatives recorded here connect forward to the {natural_list(branch_names)} branches."
            else:
                summary = "These records connect several family lines in the tree."
            if family_names:
                summary += f" Recurring names include {natural_list(family_names)}."
            return {
                "familySummary": summary,
                "familyBranches": branches,
                "familyNames": family_names,
                "peopleCount": len(person_ids),
            }

        def aggregate(items):
            events = sum(item["count"] for item in items)
            weight = max(events, 1)
            return {
                "events": events,
                "communityCount": len(items),
                "lat": sum(item["lat"] * item["count"] for item in items) / weight,
                "lng": sum(item["lng"] * item["count"] for item in items) / weight,
                **family_context(items),
            }

        result = []
        for geo_id in geo_meta:
            regions = []
            all_items = []
            for region_id, items in buckets[geo_id].items():
                items = sorted(items, key=lambda item: (-item["people"], item["name"]))
                title, summary = region_meta[region_id]
                regions.append({"id": region_id, "title": title, "summary": summary,
                                "communities": items, **aggregate(items)})
                all_items.extend(items)
            regions.sort(key=lambda item: -item["events"])
            title, summary, preface = geo_meta[geo_id]
            result.append({"id": geo_id, "title": title, "summary": summary, "preface": preface,
                           "regions": regions, **aggregate(all_items)})
        return result

    def stats(self):
        visible = [p for p in self.people.values() if self.visible(p)]
        years = [e.year for p in visible for e in p.events if e.year]
        countries = Counter()
        for p in visible:
            birth = p.event("BIRT")
            if birth and birth.place:
                countries[country_for(birth.place)] += 1
        countries.pop("Unknown", None)
        return {
            "people": len(visible), "families": len(self.families),
            "events": sum(len(p.events) for p in visible),
            "firstYear": min(years, default=None), "lastYear": max(years, default=None),
            "countries": countries.most_common(8),
        }

    def person_evidence(self, person):
        """Evidence level for a person, using the same rule as their detail page."""
        citations = person.citations + [c for event in person.events for c in event.citations]
        return self.evidence_summary(citations)

    def people_index(self):
        """Every public relative grouped by surname, with facets for browsing.

        Search only helps a visitor who already knows a name. This is the way in
        for someone who does not. A frozen repository builds it once (several
        pages and the homepage read it); callers must treat it as read-only.
        """
        if not self._frozen:
            return self._build_people_index()
        if self._people_index_cache is None:
            self._people_index_cache = self._build_people_index()
        return self._people_index_cache

    def freeze(self):
        """Promise no further edits, so whole-tree views can be built once.

        The web app freezes the repository it serves. Code that edits people or
        events in place (imports, tests) leaves it unfrozen and always sees
        fresh results.
        """
        self._frozen = True
        return self

    def _build_people_index(self):
        groups = defaultdict(list)
        branches = Counter()
        countries = Counter()
        for person in self.people.values():
            if not self.visible(person):
                continue
            branch = self.branch_context(person.id)
            birth = person.event("BIRT")
            death = person.event("DEAT")
            country = country_for(birth.place) if birth and birth.place else "Unknown"
            evidence = self.person_evidence(person)
            entry = {
                "id": person.id,
                "name": person.display_name,
                "formalName": person.formal_name,
                "nameNotes": person.name_notes,
                "years": "–".join(str(x) if x else "?" for x in (person.birth_year, person.death_year)),
                "birthYear": person.birth_year,
                "birthPlace": display_place(birth.place) if birth else "",
                "deathPlace": display_place(death.place) if death else "",
                "branch": branch["label"] if branch else "",
                "relationship": self.anchor_relationship(person.id, branch),
                "geography": self.person_geography(person),
                "country": country,
                "evidence": evidence["level"],
                "evidenceLabel": evidence["label"],
                "researchReview": self.research_review(person.id),
                "hasStory": person.id in STORY_PERSON_IDS,
                "events": len(person.events),
            }
            groups[person.formal_surname or "Surname not recorded"].append(entry)
            if entry["branch"]:
                branches[entry["branch"]] += 1
            if country != "Unknown":
                countries[country] += 1

        letters = []
        for surname in sorted(groups, key=lambda name: (re.sub(r"^[^A-Za-z]+", "", name).casefold(), name)):
            people = sorted(groups[surname], key=lambda item: (item["birthYear"] or 9999, item["name"]))
            letters.append({
                "surname": surname,
                "slug": slugify(surname),
                "initial": surname[0].upper() if surname[:1].isalpha() else "#",
                "count": len(people),
                "people": people,
                "span": self._surname_span(people),
            })
        return {
            "groups": letters,
            "initials": sorted({group["initial"] for group in letters}),
            "total": sum(group["count"] for group in letters),
            "surnameCount": len(letters),
            "branches": branches.most_common(),
            "countries": countries.most_common(8),
            "documented": sum(1 for group in letters for person in group["people"] if person["evidence"] == "documented"),
        }

    def outlier_feed(self, limit=8):
        """Show editorially selected destination stories, never lifespan anomalies."""
        def endpoint_country(place):
            country = country_for(place)
            return "" if country == "Unknown" else country

        candidates = []
        seen = set()
        for family in self.families.values():
            siblings = [self.people[child_id] for child_id in family.children
                        if child_id in self.people and self.visible(self.people[child_id])]
            endpoints = []
            for sibling in siblings:
                death = sibling.event("DEAT")
                country = endpoint_country(death.place) if death and death.place else ""
                if country:
                    endpoints.append((sibling, country))
            counts = Counter(country for _, country in endpoints)
            if len(endpoints) < 4 or len(counts) < 2:
                continue
            majority, majority_count = counts.most_common(1)[0]
            for sibling, country in endpoints:
                if (sibling.id not in SHOWCASE_PERSON_IDS or self.research_review(sibling.id)
                        or sibling.id in seen or counts[country] != 1 or majority_count < 3):
                    continue
                peers = [self.person_summary(peer) for peer, peer_country in endpoints
                         if peer.id != sibling.id and peer_country == majority][:4]
                candidates.append({
                    "kind": "destination",
                    "person": self.person_summary(sibling),
                    "title": f"The sibling whose life ended in {country}",
                    "context": f"At least {majority_count} siblings died in {majority}; this life ends somewhere different.",
                    "siblings": peers,
                    "review": False,
                })
                seen.add(sibling.id)
        candidates.sort(key=lambda item: (
            0 if item["kind"] == "destination" else 1,
            item["person"]["name"],
        ))
        return candidates[:limit]

    def branch_character_cards(self, minimum_people=5):
        """Derived surname profiles; labels describe the published tree, not people."""
        index = self.people_index()
        cards = []
        for group in index["groups"]:
            if group["count"] < minimum_people:
                continue
            countries = Counter(person["country"] for person in group["people"]
                                if person["country"] != "Unknown")
            documented = sum(person["evidence"] == "documented" for person in group["people"])
            mobile = sum(
                bool(person["birthPlace"] and person["deathPlace"]
                     and country_for(person["birthPlace"]) != "Unknown"
                     and country_for(person["deathPlace"]) != "Unknown"
                     and country_for(person["birthPlace"]) != country_for(person["deathPlace"]))
                for person in group["people"]
            )
            homeland = countries.most_common(1)[0][0] if countries else "Place not established"
            earliest = next((person for person in group["people"] if person["birthYear"]), None)
            introduction = (
                f"The earliest dated birth in this surname group is {('an unnamed ' + group['surname']) if earliest['formalName'] == group['surname'] else earliest['formalName']} "
                f"({earliest['birthYear']})"
                + (f", recorded in {earliest['birthPlace']}. " if earliest["birthPlace"] else ". ")
            ) if earliest else "This surname group's birth dates are still incomplete. "
            introduction += (
                f"Across the group, {'the United States' if homeland == 'United States' else homeland} "
                "is the most frequently recorded birth country."
            ) if countries else "Birthplaces are not yet established."
            cards.append({
                "surname": group["surname"], "slug": group["slug"],
                "people": group["count"], "span": group["span"],
                "homeland": homeland,
                "intro": introduction,
                "documentedPercent": round(100 * documented / group["count"]),
                "movementPercent": round(100 * mobile / group["count"]),
            })
        return sorted(cards, key=lambda item: (-item["people"], item["surname"]))

    @staticmethod
    def _surname_span(people):
        years = [person["birthYear"] for person in people if person["birthYear"]]
        if not years:
            return ""
        return f"{min(years)}–{max(years)}" if min(years) != max(years) else str(min(years))

    # The site profile names two migration streams by birth country. Where they
    # finally marry into each other is often the strongest story in the data, so
    # it is derived here rather than asserted in copy.
    @staticmethod
    def _stream_for_country(country):
        return next((stream["key"] for stream in profile.STREAMS
                     if country in stream["countries"]), None)

    @staticmethod
    def _stream_keys():
        return tuple(stream["key"] for stream in profile.STREAMS[:2])

    def _parent_ids(self, person_id):
        person = self.people.get(person_id)
        family = self.families.get(person.child_family) if person and person.child_family else None
        return [x for x in (family.husband, family.wife) if x] if family else []

    def ancestral_streams(self, person_id, _seen=None):
        """Which migration streams a person descends from."""
        if person_id in self._stream_cache:
            return self._stream_cache[person_id]
        _seen = _seen or set()
        if person_id in _seen:
            return frozenset()
        _seen = _seen | {person_id}
        found = set()
        person = self.people.get(person_id)
        if person:
            birth = person.event("BIRT")
            if birth and birth.place:
                stream = self._stream_for_country(country_for(birth.place))
                if stream:
                    found.add(stream)
        for parent_id in self._parent_ids(person_id):
            found |= self.ancestral_streams(parent_id, _seen)
        result = frozenset(found)
        if not _seen - {person_id}:
            self._stream_cache[person_id] = result
        return result

    def convergence(self):
        """The marriages where the two streams first join, and who descends from both."""
        keys = self._stream_keys()
        if len(keys) < 2:
            return {"marriages": [], "descendants": 0, "firstYear": None, "lastYear": None}
        first, second = keys
        both = [pid for pid in self.people
                if {first, second} <= self.ancestral_streams(pid)]
        # Tie each historical merge to the modern family branch it eventually
        # becomes. These are the same branch anchors used throughout the site
        # for branch context, so the label stays consistent.
        anchor_ancestry = []
        for anchor in self._grandparent_anchors():
            ancestors = set()
            stack = [anchor.id]
            while stack:
                ancestor_id = stack.pop()
                if ancestor_id in ancestors:
                    continue
                ancestors.add(ancestor_id)
                stack.extend(self._parent_ids(ancestor_id))
            anchor_ancestry.append((anchor, ancestors))
        joins = []
        for family in self.families.values():
            if not (family.husband and family.wife):
                continue
            husband, wife = self.ancestral_streams(family.husband), self.ancestral_streams(family.wife)
            already_mixed = any({first, second} <= side for side in (husband, wife))
            crosses = (first in husband and second in wife) or (second in husband and first in wife)
            if not crosses or already_mixed:
                continue
            children = [c for c in family.children if c in self.people]
            if not children:
                continue  # a crossing that left no descendants is not a merge
            marriage = next((e for e in family.events if e.kind == "MARR"), None)
            child_years = [self.people[c].birth_year for c in children if self.people[c].birth_year]
            year = marriage.year if marriage and marriage.year else (min(child_years) - 1 if child_years else None)
            present_branches = [{
                "label": f"{anchor.formal_surname or anchor.display_name} family",
                # Anchors are grandparents and may still be living, so fall back to
                # the familiar name rather than printing "Living person".
                "anchor": dict(
                    self.person_summary(anchor, force_visible=True),
                    name=anchor.display_name,
                ),
            } for anchor, ancestors in anchor_ancestry
                if family.husband in ancestors or family.wife in ancestors]
            joins.append({
                "year": year,
                "estimated": not (marriage and marriage.year),
                "husband": self.person_summary(self.people[family.husband]),
                "wife": self.person_summary(self.people[family.wife]),
                "husbandLine": self.ancestral_line(family.husband),
                "wifeLine": self.ancestral_line(family.wife),
                "children": len(children),
                "descendants": self.descendant_count(children),
                "place": display_place((marriage.place if marriage and marriage.place else "") or self._first_child_place(children)),
                "presentBranches": present_branches,
            })
        joins.sort(key=lambda item: (item["year"] is None, item["year"]))
        years = [j["year"] for j in joins if j["year"]]
        return {
            "marriages": joins,
            "descendants": len(both),
            "firstYear": min(years) if years else None,
            "lastYear": max(years) if years else None,
        }

    def ancestral_line(self, person_id):
        """The line a person carries into a marriage: surname, homeland, stream."""
        person = self.people.get(person_id)
        if not person:
            return None
        countries = Counter()
        earliest = None
        seen = set()
        stack = [person_id]
        while stack:
            current_id = stack.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            relative = self.people.get(current_id)
            if not relative:
                continue
            birth = relative.event("BIRT")
            if birth and birth.place:
                country = country_for(birth.place)
                if self._stream_for_country(country):
                    countries[country] += 1
                    if birth.year and (earliest is None or birth.year < earliest[0]):
                        earliest = (birth.year, relative)
            stack.extend(self._parent_ids(current_id))
        if not countries:
            return None
        homeland, _ = countries.most_common(1)[0]
        streams = self.ancestral_streams(person_id)
        keys = self._stream_keys()
        stream = next((key for key in keys if key in streams), keys[0] if keys else "")
        return {
            "surname": person.formal_surname or person.clean_name,
            "homeland": homeland,
            "stream": stream,
            "streamSlot": keys.index(stream) if stream in keys else 0,
            "earliest": self.person_summary(earliest[1]) if earliest else None,
            "earliestYear": earliest[0] if earliest else None,
            "generations": max(1, len(seen) and self._depth_above(person_id)),
        }

    def _depth_above(self, person_id):
        depth = 0
        frontier = [person_id]
        seen = {person_id}
        while frontier:
            nxt = []
            for current in frontier:
                for parent in self._parent_ids(current):
                    if parent not in seen:
                        seen.add(parent)
                        nxt.append(parent)
            if nxt:
                depth += 1
            frontier = nxt
        return depth

    def descendant_count(self, seed_ids):
        """Everyone descending from these children, the couple's actual footprint."""
        seen = set()
        stack = list(seed_ids)
        while stack:
            current = stack.pop()
            if current in seen or current not in self.people:
                continue
            seen.add(current)
            for family_id in self.people[current].spouse_families:
                family = self.families.get(family_id)
                if family:
                    stack.extend(family.children)
        return len(seen)

    def _first_child_place(self, children):
        for child_id in children:
            birth = self.people[child_id].event("BIRT")
            if birth and birth.place:
                return birth.place
        return ""

    def home_timeline(self):
        events = sorted(
            ((event.year, person, event) for person in self.people.values() if self.visible(person)
             for event in person.events if event.year),
            key=lambda item: (item[0], item[1].clean_name),
        )
        if not events:
            return []
        copy = profile.TIMELINE
        home_term = profile.HOME_REGION["term"]
        earliest_year, earliest_person, earliest_event = events[0]
        first_us = next((item for item in events if item[2].place and country_for(item[2].place) == profile.HOME_COUNTRY), None)
        first_home = next((item for item in events if item[2].place and home_term in item[2].place.casefold()), None)
        latest_year = events[-1][0]
        merge = self.convergence()
        couples = len(merge["marriages"])
        places = []
        for marriage in merge["marriages"]:
            head = (marriage["place"] or "").split(",")[0].strip()
            if head and head not in places:
                places.append(head)
        merge_copy = copy["merge"]
        merge_text = merge_copy["text"].format(
            first_year=merge["firstYear"], last_year=merge["lastYear"],
            weddings=couples, descendants=merge["descendants"],
        ) if couples else merge_copy["text_fallback"]

        def with_context(entry, block):
            for key in ("context", "contextSource", "contextUrl"):
                if block.get(key):
                    entry[key] = block[key]
            return entry

        entries = [
            {"year": earliest_year, "title": copy["earliest"]["title"],
             "text": copy["earliest"]["text"].format(name=earliest_person.display_name),
             "place": display_place(earliest_event.place) or copy["earliest"]["fallback_place"]},
        ]
        # Both of these describe a place the tree may simply not reach. A dataset
        # with no record there must not take the page down with it.
        if first_us:
            block = copy["first_foothold"]
            entries.append(with_context(
                {"year": first_us[0], "title": block["title"], "text": block["text"],
                 "place": display_place(first_us[2].place)}, block))
        if first_home:
            block = copy["first_home"]
            entries.append(with_context(
                {"year": first_home[0], "title": block["title"], "text": block["text"],
                 "place": block.get("place") or display_place(first_home[2].place)}, block))
        growth, present = copy["growth"], copy["present"]
        entries.extend([
            {"year": merge["firstYear"] or merge_copy["fallback_year"], "title": merge_copy["title"],
             "text": merge_text,
             "place": " · ".join(places) if places else merge_copy["fallback_place"]},
            {"year": growth["year"], "title": growth["title"], "text": growth["text"], "place": growth["place"]},
            {"year": latest_year, "title": present["title"], "text": present["text"], "place": present["place"]},
        ])
        return entries

    def geography_stages(self):
        """Return evidence-aware map layers for origins, footholds, and home-region roots."""
        people = [person for person in self.people.values() if self.visible(person)]
        home_country = profile.HOME_COUNTRY
        home_term = profile.HOME_REGION["term"]
        home_label = profile.HOME_REGION["label"]

        def add(groups, person, event, evidence="record"):
            coords = locate(event.place)
            if not coords:
                return
            key = coords[2]
            item = groups[key]
            item["name"] = key
            item["lat"], item["lng"] = coords[0], coords[1]
            item["records"] += 1
            item["people"].add(person.id)
            item["evidence"][evidence] += 1
            if event.year:
                item["years"].append(event.year)

        def new_groups():
            return defaultdict(lambda: {
                "name": "", "lat": 0.0, "lng": 0.0, "records": 0,
                "people": set(), "years": [], "evidence": Counter(),
            })

        def cluster(groups, exclude=()):
            """Fold co-located places into one map point, largest name winning.

            People are merged as sets, not sums: someone with events in both a
            town and its surrounding county is one relative, not two.
            """
            ordered = sorted(
                (values for name, values in groups.items() if name not in exclude),
                key=lambda values: (-len(values["people"]), -values["records"], values["name"]),
            )
            clusters = []
            for entry in ordered:
                host = next(
                    (c for c in clusters
                     if distance_km(c["lat"], c["lng"], entry["lat"], entry["lng"]) <= MAP_CLUSTER_KM),
                    None,
                )
                if host is None:
                    clusters.append({
                        "name": entry["name"], "lat": entry["lat"], "lng": entry["lng"],
                        "records": entry["records"], "people": set(entry["people"]),
                        "years": list(entry["years"]), "evidence": Counter(entry["evidence"]),
                        "merged": [],
                    })
                    continue
                host["records"] += entry["records"]
                host["people"] |= entry["people"]
                host["years"] += entry["years"]
                host["evidence"].update(entry["evidence"])
                host["merged"].append(entry["name"])
            return clusters

        def serialize(groups, exclude=()):
            items = []
            for values in cluster(groups, exclude):
                branch_counts = Counter()
                for person_id in values["people"]:
                    branch = self.branch_context(person_id)
                    if branch:
                        branch_counts[branch["label"]] += 1
                items.append({
                    "name": values["name"], "lat": values["lat"], "lng": values["lng"],
                    "slug": slugify(values["name"]),
                    "records": values["records"], "people": len(values["people"]),
                    "alsoKnown": values["merged"],
                    "firstYear": min(values["years"], default=None),
                    "lastYear": max(values["years"], default=None),
                    "evidence": dict(values["evidence"]),
                    "familyBranches": [
                        {"label": label, "people": count}
                        for label, count in sorted(
                            branch_counts.items(), key=lambda entry: (-entry[1], entry[0])
                        )
                    ],
                })
            return sorted(items, key=lambda item: (-item["people"], -item["records"], item["name"]))

        origins = new_groups()
        gateways = new_groups()
        homesteads = new_groups()
        arrival_events = []
        unnamed_home_records = 0
        family_stream_people = defaultdict(set)
        family_stream_surnames = defaultdict(set)

        for person in people:
            birth = person.event("BIRT")
            if birth and birth.place and country_for(birth.place) not in (home_country, "Unknown"):
                add(origins, person, birth, "birth")
                candidates = [
                    event for event in person.events
                    if event.place
                    and event.kind != "BIRT"
                    and country_for(event.place) == home_country
                    and event.year
                ]
                if candidates:
                    first_us = min(candidates, key=lambda event: event.year)
                    add(gateways, person, first_us, "first-us-record")
                    destination = migration_region(first_us.place)
                    origin = profile.CORRIDOR_ORIGINS.get(country_for(birth.place))
                    branch = self.branch_context(person.id)
                    if destination and origin and branch:
                        broad = profile.CORRIDOR_DESTINATIONS.get(destination[0])
                        if broad:
                            key = (branch["label"], origin, broad)
                            family_stream_people[key].add(person.id)
                            if person.formal_surname:
                                family_stream_surnames[key].add(person.formal_surname)

            for event in person.events:
                if event.kind in ("ARRIVAL", "IMMI"):
                    arrival_events.append(event)
                    if event.place:
                        add(gateways, person, event, "documented-arrival")
                if event.place and home_term in event.place.casefold():
                    coords = locate(event.place)
                    if coords and coords[2] == home_label:
                        unnamed_home_records += 1
                    else:
                        add(homesteads, person, event, "home-record")

        origin_items = serialize(origins)
        gateway_items = serialize(gateways, exclude=(home_label,))
        homestead_items = serialize(homesteads, exclude=(home_label,))
        slots = self.branch_slots()
        junctions = getattr(profile, "BRANCH_MAP_JUNCTIONS", ())
        family_streams = []
        for (branch, origin, destination), people_ids in family_stream_people.items():
            family_streams.append({
                "branch": branch,
                "slot": slots.get(branch, 0),
                "familyNames": sorted(family_stream_surnames[(branch, origin, destination)]),
                "origin": origin[0], "destination": destination[0],
                "people": len(people_ids),
                "fromLat": origin[1], "fromLng": origin[2],
                "toLat": destination[1], "toLng": destination[2],
            })
        family_streams.sort(key=lambda item: (
            item["branch"], -item["people"], item["origin"], item["destination"]
        ))

        merge = self.convergence()
        chapters = profile.MAP_CHAPTERS
        gateway_copy = chapters["gateways"]
        merge_line = gateway_copy["anecdote"].format(
            weddings=len(merge["marriages"]), first_year=merge["firstYear"],
            last_year=merge["lastYear"], descendants=merge["descendants"],
        ) if merge.get("marriages") else gateway_copy["anecdote_fallback"]

        def chapter(key, **extra):
            block = chapters[key]
            return {
                "title": block["title"], "eyebrow": block["eyebrow"],
                "summary": block["summary"], "anecdote": block["anecdote"],
                "method": block["method"], **extra,
            }

        home_center = profile.HOME_REGION.get("center")
        return {
            "origins": chapter("origins", items=origin_items[:18]),
            "gateways": chapter(
                "gateways", anecdote=merge_line, items=gateway_items[:18],
                familyStreams=family_streams, highlights=[],
                branches=[
                    {"label": label, "slot": slot,
                     "junction": list(junctions[slot]) if slot < len(junctions) else None}
                    for label, slot in slots.items()
                ],
            ),
            "homesteads": chapter(
                "homesteads", items=homestead_items[:18],
                center={"label": home_label,
                        "lat": home_center[0] if home_center else None,
                        "lng": home_center[1] if home_center else None,
                        "outline": profile.HOME_REGION.get("outline")},
            ),
        }


def classify_place(name, lng):
    """Assign a mapped place to a (chapter, region) pair using the profile rules."""
    text = name.casefold()
    for rule in profile.PLACE_RULES:
        if rule.get("all") and not all(term in text for term in rule["all"]):
            continue
        if rule.get("any") and not any(term in text for term in rule["any"]):
            continue
        if "lng_below" in rule and not lng < rule["lng_below"]:
            continue
        if "lng_above" in rule and not lng > rule["lng_above"]:
            continue
        return rule["tier"], rule["region"]
    last = profile.PLACE_RULES[-1]
    return last["tier"], last["region"]


def migration_region(place):
    """Narrative region (id, label, lat, lng) for a first record in the home country."""
    text = place.casefold()
    for region in profile.MIGRATION_REGIONS:
        if any(term in text for term in region["any"]):
            return region["id"], region["label"], region["lat"], region["lng"]
    return None


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def locate(place):
    """Deliberately conservative offline lookup in the profile gazetteer.

    Similar historical labels collapse into a readable story cluster; raw GEDCOM
    place text remains available on person records.
    """
    text = place.casefold()
    for aliases, lat, lng, label in profile.GAZETTEER:
        if any(alias in text for alias in aliases):
            return lat, lng, label
    return None


def distance_km(lat1, lng1, lat2, lng2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    dy = (lat1 - lat2) * 110.574
    dx = (lng1 - lng2) * 111.320 * math.cos(mean_lat)
    return math.hypot(dx, dy)


def country_for(place):
    text = place.casefold()
    return next((label for aliases, label in profile.COUNTRIES if any(a in text for a in aliases)), "Unknown")
