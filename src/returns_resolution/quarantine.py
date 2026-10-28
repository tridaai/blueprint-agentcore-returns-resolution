"""Injection quarantine — customer free text is evidence, not input (R15).

Every piece of customer-controlled text that enters this system —
the free-text note on a return request, a free-text note handed to
the messaging layer, the warehouse's damage notes and the
photo-evidence metadata that rides along with a grading report —
lands in QUARANTINE first. Quarantine is an insert-only store: the
original text is preserved byte-exact, with its provenance (case,
source surface, field, received date), and NOTHING downstream reads
it. Downstream surfaces read only RELEASED VIEWS.

The screening pack (versioned data, below) states the screens,
deterministically, in code:

- **Instruction-shaped text** — imperatives aimed at the system
  ("ignore previous instructions", "approve this refund"), role
  hijacks ("you are now…"), system-prompt vocabulary, tool-call
  shaped payloads — and **encoded payloads** (long base64 runs,
  where smuggled instructions hide) are BLOCKING findings. A text
  carrying one is `instruction_risk`: it is never silently cleaned,
  and it leaves quarantine only by a NAMED staff act with a stated
  reason, and then only in ESCAPED, data-only form — markup
  escaped, figures/dates/percents neutralised — marked as quoted
  customer text wherever it is rendered.
- **PII-shaped text** (card numbers, SSNs, email addresses) and
  **link lures** are MASKING findings: the text releases
  automatically, but the released view renders those spans masked
  (`[card number]`, `[link]`, …), each masking recorded on the view
  as a named transformation. A customer's card number is never
  quoted back by a summary or a message.
- **Oversize text** is a finding, not a refusal: the released view
  renders at most the pack's stated character limit, with the
  truncation named on the view.

Clean text releases automatically at submit time, verbatim, with
its (empty) findings attached. A release is a NEW artefact — the
view — linked to the item it supersedes as the downstream-visible
representation; the item itself is never edited, and a held item
has NO view until the act exists. The release act is itself an
insert-only artefact: who, why, when.

This module is the ONLY caller of the store's raw quarantine
accessors (`quarantine_raw_*`): the raw text has exactly one reader
in the codebase, and a structural test (and an eval counter) scan
every other module for those names. The service talks to the
`QuarantineBook` bound over the store, never to the raw rows.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel

QUARANTINE_PACK_VERSION = "2026.10.1"

# Source surfaces — the closed vocabulary of where customer text
# enters. A new entry path is a pack change, not a new string
# invented at a call site.
SOURCE_INTAKE_NOTE = "intake_note"
SOURCE_MESSAGE_NOTE = "message_note"
SOURCE_GRADING_DAMAGE_NOTES = "grading_damage_notes"
SOURCE_PHOTO_METADATA = "photo_metadata"
SOURCE_SURFACES = [
    SOURCE_INTAKE_NOTE,
    SOURCE_MESSAGE_NOTE,
    SOURCE_GRADING_DAMAGE_NOTES,
    SOURCE_PHOTO_METADATA,
]

KIND_INSTRUCTION = "instruction"
KIND_ENCODED = "encoded"
KIND_PII = "pii"
KIND_LINK = "link"
KIND_OVERSIZE = "oversize"

# The finding kinds that hold a text: no automatic view exists for
# it, and no view exists at all until a named staff act releases it.
BLOCKING_KINDS = [KIND_INSTRUCTION, KIND_ENCODED]
# The finding kinds masked in an automatic release's render form.
MASKING_KINDS = [KIND_PII, KIND_LINK]

RISK_SCREENED = "screened"
RISK_INSTRUCTION = "instruction_risk"

FORM_VERBATIM = "verbatim"
FORM_MASKED = "masked"
FORM_ESCAPED = "escaped_data_only"

STATE_HELD = "held"
STATE_RELEASED = "released"

AUTO_RELEASED_BY = "auto (screening pack)"


class QuarantineError(RuntimeError):
    """A quarantine act was refused — the message names the gap (a
    held text with no act, an unnamed actor, a reason not stated, a
    second release of an already-released text, …)."""


class ScreenFinding(BaseModel):
    """One screen's outcome on one text, on the record.

    `finding_id` is the pack rule that fired; `kind` is its class
    (blocking / masking / advisory); `detail` states what matched,
    in the pack's words — never the matched text itself beyond a
    short excerpt the pack's rule allows (instruction excerpts are
    how a reviewer sees what was attempted; PII findings never carry
    the digits)."""

    finding_id: str
    kind: str  # instruction | encoded | pii | link | oversize
    detail: str


class ScreeningPack(BaseModel):
    """The screens, as versioned data: the pattern table, the size
    limit, and which finding kinds block / mask. The pattern
    STRINGS are data; the screening itself is code over them."""

    version: str
    max_text_chars: int
    blocking_kinds: list[str]
    masking_kinds: list[str]


SCREENING_PACK_V1 = ScreeningPack(
    version=QUARANTINE_PACK_VERSION,
    max_text_chars=2000,
    blocking_kinds=list(BLOCKING_KINDS),
    masking_kinds=list(MASKING_KINDS),
)


def load_screening_pack(version: str | None = None) -> ScreeningPack:
    """Fail-closed loader, like every pack in this blueprint: an
    unknown version is an error, never a silent fall back — a text
    must name the screens it was judged under."""
    if version is None or version == SCREENING_PACK_V1.version:
        return SCREENING_PACK_V1
    raise ValueError(f"unknown screening pack version: {version!r}")


# ---------------------------------------------------------------------------
# The screens. Deterministic regexes over the raw text; each rule
# states the finding it raises. Order is the record's order.
# ---------------------------------------------------------------------------

_INSTRUCTION_RULES: list[tuple[str, re.Pattern, str]] = [
    (
        "INSTR-IGNORE-INSTRUCTIONS",
        re.compile(
            r"(?i)\bignore\s+(?:all\s+|any\s+|the\s+)?"
            r"(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)"
        ),
        "an imperative to ignore prior instructions",
    ),
    (
        "INSTR-DISREGARD",
        re.compile(
            r"(?i)\bdisregard\s+(?:all\s+|the\s+)?"
            r"(?:previous|prior|above|system|instructions?)\b"
        ),
        "an imperative to disregard the system's instructions",
    ),
    (
        "INSTR-ROLE-HIJACK",
        re.compile(r"(?i)\byou are now\b|\bact as (?:a|an|the)\b|\bnew instructions?\s*:"),
        "an attempt to re-role the reader or issue new instructions",
    ),
    (
        "INSTR-APPROVE-REFUND",
        re.compile(
            r"(?i)\bapprove\s+(?:this|the|my)\s+refund\b"
            r"|\bmark\s+(?:this|the)\s+(?:refund|return)\s+(?:as\s+)?approved\b"
        ),
        "an imperative to approve a refund",
    ),
    (
        "INSTR-SYSTEM-VOCABULARY",
        re.compile(r"(?i)\bsystem prompt\b|\bjailbreak\b|\bdeveloper message\b"),
        "system-prompt vocabulary addressed to the model",
    ),
    (
        "INSTR-TOOL-CALL-SHAPE",
        re.compile(r"\{\s*\"(?:tool|function|name)\"\s*:|<\s*tool[\s_>-]", re.I),
        "a tool-call shaped payload embedded in prose",
    ),
]

_ENCODED_RULE = re.compile(r"\b[A-Za-z0-9+/]{120,}={0,2}\b")
_CARD_RULE = re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b|\b\d{15,16}\b")
_SSN_RULE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL_RULE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_LINK_RULE = re.compile(r"(?i)\bhttps?://|\bwww\.")

# Figure shapes the escaped form neutralises — the same shapes the
# figure verifier extracts (verifier.py), restated here so a held
# text's claims can never ride into a rendered view as live figures.
_AMOUNT_SHAPE = re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})|\$\d+\.\d{2}")
_DATE_SHAPE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_PERCENT_SHAPE = re.compile(r"\d+(?:\.\d+)?\s*%")


def screen_text(text: str, pack: ScreeningPack | None = None) -> list[ScreenFinding]:
    """Run every screen over the raw text. Pure and deterministic:
    the same text under the same pack yields the same findings, in
    the same order — which is what makes a stored finding set
    re-performable by a verifier."""
    pack = pack or SCREENING_PACK_V1
    findings: list[ScreenFinding] = []
    for rule_id, pattern, detail in _INSTRUCTION_RULES:
        if pattern.search(text):
            findings.append(
                ScreenFinding(finding_id=rule_id, kind=KIND_INSTRUCTION, detail=detail)
            )
    if _ENCODED_RULE.search(text):
        findings.append(
            ScreenFinding(
                finding_id="ENCODED-BASE64-BLOB",
                kind=KIND_ENCODED,
                detail="a long base64-shaped run: encoded payloads are where "
                "smuggled instructions hide",
            )
        )
    if _SSN_RULE.search(text):
        findings.append(
            ScreenFinding(
                finding_id="PII-SSN",
                kind=KIND_PII,
                detail="a US social-security-number shaped span (digits not recorded)",
            )
        )
    if _CARD_RULE.search(text):
        findings.append(
            ScreenFinding(
                finding_id="PII-CARD-NUMBER",
                kind=KIND_PII,
                detail="a payment-card-number shaped span (digits not recorded)",
            )
        )
    if _EMAIL_RULE.search(text):
        findings.append(
            ScreenFinding(
                finding_id="PII-EMAIL",
                kind=KIND_PII,
                detail="an email-address shaped span (address not recorded)",
            )
        )
    if _LINK_RULE.search(text):
        findings.append(
            ScreenFinding(
                finding_id="LINK-EXTERNAL",
                kind=KIND_LINK,
                detail="an external link: quoted customer text never carries a live lure",
            )
        )
    if len(text) > pack.max_text_chars:
        findings.append(
            ScreenFinding(
                finding_id="OVERSIZE-TEXT",
                kind=KIND_OVERSIZE,
                detail=(
                    f"the text runs {len(text)} characters, past the pack's "
                    f"{pack.max_text_chars}-character statement limit"
                ),
            )
        )
    return findings


def risk_of(findings: list[ScreenFinding], pack: ScreeningPack | None = None) -> str:
    pack = pack or SCREENING_PACK_V1
    if any(f.kind in pack.blocking_kinds for f in findings):
        return RISK_INSTRUCTION
    return RISK_SCREENED


def mask_text(text: str) -> tuple[str, list[str]]:
    """The masking render: PII and link spans replaced by stated
    placeholders, in a fixed order (SSN before card — the card
    pattern would otherwise eat an SSN's digits). Returns the
    rendered text and the transformation names applied."""
    transformations: list[str] = []
    rendered = text
    if _SSN_RULE.search(rendered):
        rendered = _SSN_RULE.sub("[social security number]", rendered)
        transformations.append("masked:social_security_number")
    if _CARD_RULE.search(rendered):
        rendered = _CARD_RULE.sub("[card number]", rendered)
        transformations.append("masked:card_number")
    if _EMAIL_RULE.search(rendered):
        rendered = _EMAIL_RULE.sub("[email address]", rendered)
        transformations.append("masked:email_address")
    if _LINK_RULE.search(rendered):
        rendered = re.sub(r"(?i)https?://\S+|www\.\S+", "[link]", rendered)
        transformations.append("masked:link")
    return rendered, transformations


def escape_text(text: str) -> tuple[str, list[str]]:
    """The escaped, data-only render for a held text released by a
    named act: PII/links masked as above, then figures, dates and
    percents neutralised (a claim's NUMBERS never survive as live
    figures), then markup escaped. The words remain — attributed,
    quoted, inert."""
    rendered, transformations = mask_text(text)
    if _AMOUNT_SHAPE.search(rendered):
        rendered = _AMOUNT_SHAPE.sub("[an amount]", rendered)
        transformations.append("neutralised:amounts")
    if _DATE_SHAPE.search(rendered):
        rendered = _DATE_SHAPE.sub("[a date]", rendered)
        transformations.append("neutralised:dates")
    if _PERCENT_SHAPE.search(rendered):
        rendered = _PERCENT_SHAPE.sub("[a percentage]", rendered)
        transformations.append("neutralised:percents")
    rendered = html.escape(rendered, quote=True)
    transformations.append("escaped:markup")
    return rendered, transformations


def _truncate(rendered: str, pack: ScreeningPack) -> tuple[str, list[str]]:
    if len(rendered) > pack.max_text_chars:
        return rendered[: pack.max_text_chars] + " […]", ["truncated:pack_limit"]
    return rendered, []


# ---------------------------------------------------------------------------
# The artefacts.
# ---------------------------------------------------------------------------


class QuarantineItem(BaseModel):
    """One customer text, in quarantine — insert-only, byte-exact.

    The original text stands here and ONLY here: this artefact is
    what the raw store accessors return, and quarantine.py is their
    only reader. `risk` is derived from the findings at screen time
    and recorded with them; a tampered finding set is detectable by
    re-screening (verify_item)."""

    quarantine_id: str
    case_id: str
    source_surface: str  # one of SOURCE_SURFACES
    field_name: str
    original_text: str
    text_sha256: str
    received_on: date
    pack_version: str
    findings: list[ScreenFinding] = []
    risk: str  # screened | instruction_risk


class ReleasedTextView(BaseModel):
    """The ONLY form in which customer text leaves quarantine.

    A new artefact per release, linked by `supersedes` to the item
    it makes downstream-visible. `form` states how `render_text`
    was produced from the original (verbatim / masked /
    escaped_data_only) and `transformations` names every change —
    a render with an unrecorded transformation does not exist.
    `released_by` is the pack for an automatic release, or the
    named staff member whose act released a held text (with the
    act's id)."""

    view_id: str
    quarantine_id: str
    case_id: str
    form: str  # verbatim | masked | escaped_data_only
    render_text: str
    transformations: list[str] = []
    findings: list[ScreenFinding] = []
    released_by: str
    release_act_id: str | None = None
    supersedes: str  # the quarantine item this view supersedes downstream
    created_on: date


class QuarantineReleaseAct(BaseModel):
    """The named staff act that releases a held text — insert-only.

    Who, why, when, and the view it minted. An act against a text
    that is not held, or by an unnamed actor, or without a stated
    reason, is refused before it exists."""

    act_id: str
    quarantine_id: str
    case_id: str
    released_by: str
    reason: str
    released_on: date
    view_id: str
    pack_version: str


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def verify_item(item: QuarantineItem, pack: ScreeningPack | None = None) -> list[str]:
    """Re-perform an item's screening; [] = intact. The text's hash
    and the recorded findings must both reproduce — a stored item
    whose findings were softened after the fact is detected, not
    believed."""
    pack = pack or load_screening_pack(item.pack_version)
    problems: list[str] = []
    digest = hashlib.sha256(item.original_text.encode("utf-8")).hexdigest()
    if digest != item.text_sha256:
        problems.append("the stored text does not hash to the recorded text_sha256")
    expected = screen_text(item.original_text, pack)
    if [f.model_dump() for f in expected] != [f.model_dump() for f in item.findings]:
        problems.append("the recorded findings differ from a fresh screening of the text")
    if risk_of(item.findings, pack) != item.risk:
        problems.append("the recorded risk does not follow from the recorded findings")
    return problems


def raw_store_access_violations(package_dir: Path) -> list[str]:
    """The structural rule, as a scan: the store's raw quarantine
    accessors (`quarantine_raw_*`) may be named ONLY by this module
    (their one reader) and by store.py (their definition). Any
    other module naming them is reading raw customer text outside
    the quarantine boundary."""
    violations: list[str] = []
    for path in sorted(package_dir.glob("*.py")):
        if path.name in ("quarantine.py", "store.py"):
            continue
        if "quarantine_raw_" in path.read_text(encoding="utf-8"):
            violations.append(
                f"{path.name} names the raw quarantine store accessors; "
                "quarantine.py is their only reader"
            )
    return violations


class QuarantineBook:
    """The quarantine boundary, bound over the store.

    The book is constructed with the whole store and is the only
    object that calls its `quarantine_raw_*` accessors. Everything
    else in the system — the service included — works with items'
    metadata, findings, views and acts through this API, and reads
    customer text ONLY as a view's `render_text`."""

    def __init__(self, store, pack: ScreeningPack | None = None):
        self._store = store
        self._pack = pack or load_screening_pack()

    @property
    def pack(self) -> ScreeningPack:
        return self._pack

    # -- submission -----------------------------------------------------
    def submit(
        self,
        *,
        case_id: str,
        source_surface: str,
        field_name: str,
        text: str,
        received_on: date,
    ) -> QuarantineItem:
        """Land a customer text in quarantine and screen it.

        Idempotent per (case, surface, field, text): re-delivering
        the same text returns the standing item — evidence is
        landed once, however many times the surface retries. A
        screened (non-blocking) text's automatic view is minted in
        the same act; a held text gets NO view here."""
        if source_surface not in SOURCE_SURFACES:
            raise QuarantineError(
                f"unknown quarantine source surface {source_surface!r}: the pack's "
                f"vocabulary is {', '.join(SOURCE_SURFACES)}"
            )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        for existing in self._items_for(case_id):
            if (
                existing.source_surface == source_surface
                and existing.field_name == field_name
                and existing.text_sha256 == digest
            ):
                return existing
        seq = (
            sum(
                1
                for i in self._items_for(case_id)
                if i.source_surface == source_surface and i.field_name == field_name
            )
            + 1
        )
        findings = screen_text(text, self._pack)
        blob = _canonical(
            {
                "case_id": case_id,
                "source_surface": source_surface,
                "field_name": field_name,
                "text_sha256": digest,
                "received_on": received_on.isoformat(),
                "seq": seq,
            }
        )
        item = QuarantineItem(
            quarantine_id="QT-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            source_surface=source_surface,
            field_name=field_name,
            original_text=text,
            text_sha256=digest,
            received_on=received_on,
            pack_version=self._pack.version,
            findings=findings,
            risk=risk_of(findings, self._pack),
        )
        self._store.quarantine_raw_put_item(item.model_dump(mode="json"))
        if item.risk != RISK_INSTRUCTION:
            self._mint_auto_view(item, created_on=received_on)
        return item

    def _mint_auto_view(self, item: QuarantineItem, *, created_on: date) -> ReleasedTextView:
        findings = item.findings
        if any(f.kind in self._pack.masking_kinds for f in findings):
            render_text, transformations = mask_text(item.original_text)
            form = FORM_MASKED
        else:
            render_text, transformations = item.original_text, []
            form = FORM_VERBATIM
        render_text, extra = _truncate(render_text, self._pack)
        transformations += extra
        blob = _canonical(
            {"quarantine_id": item.quarantine_id, "form": form, "released_by": AUTO_RELEASED_BY}
        )
        view = ReleasedTextView(
            view_id="QV-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            quarantine_id=item.quarantine_id,
            case_id=item.case_id,
            form=form,
            render_text=render_text,
            transformations=transformations,
            findings=findings,
            released_by=AUTO_RELEASED_BY,
            release_act_id=None,
            supersedes=item.quarantine_id,
            created_on=created_on,
        )
        self._store.put_quarantine_view(view.model_dump(mode="json"))
        return view

    # -- the release act --------------------------------------------------
    def release(
        self,
        quarantine_id: str,
        *,
        released_by: str,
        reason: str,
        released_on: date,
    ) -> ReleasedTextView:
        """Release a HELD text by a named staff act.

        Refusals, each naming the gap: an unknown item; an unnamed
        actor; no stated reason; an item that is not held (a
        screened text already carries its automatic view — a second
        release would mint a second truth); an item already
        released. The view produced is the escaped, data-only form:
        the act opens the text to downstream READING, never to
        downstream obedience."""
        item = self.get_item(quarantine_id)
        if item is None:
            raise LookupError(f"unknown quarantine item: {quarantine_id}")
        actor = (released_by or "").strip()
        if not actor:
            raise QuarantineError("releasing a held text requires a named staff member")
        if not (reason or "").strip():
            raise QuarantineError(
                "releasing a held text requires a stated reason — the act is the audit"
            )
        if item.risk != RISK_INSTRUCTION:
            raise QuarantineError(
                f"quarantine item {quarantine_id} is not held (risk {item.risk!r}): "
                "it already carries its automatic released view, and a release act "
                "exists only for held texts"
            )
        if self.view_for(quarantine_id) is not None:
            raise QuarantineError(
                f"quarantine item {quarantine_id} is already released; a second "
                "release would mint a second view of the same text"
            )
        act_blob = _canonical(
            {
                "quarantine_id": quarantine_id,
                "released_by": actor,
                "reason": reason.strip(),
                "released_on": released_on.isoformat(),
            }
        )
        act_id = "QA-" + hashlib.sha256(act_blob.encode()).hexdigest()[:16]
        render_text, transformations = escape_text(item.original_text)
        render_text, extra = _truncate(render_text, self._pack)
        transformations += extra
        view_blob = _canonical(
            {"quarantine_id": quarantine_id, "form": FORM_ESCAPED, "release_act_id": act_id}
        )
        view = ReleasedTextView(
            view_id="QV-" + hashlib.sha256(view_blob.encode()).hexdigest()[:16],
            quarantine_id=quarantine_id,
            case_id=item.case_id,
            form=FORM_ESCAPED,
            render_text=render_text,
            transformations=transformations,
            findings=item.findings,
            released_by=actor,
            release_act_id=act_id,
            supersedes=quarantine_id,
            created_on=released_on,
        )
        act = QuarantineReleaseAct(
            act_id=act_id,
            quarantine_id=quarantine_id,
            case_id=item.case_id,
            released_by=actor,
            reason=reason.strip(),
            released_on=released_on,
            view_id=view.view_id,
            pack_version=self._pack.version,
        )
        self._store.put_quarantine_act(act.model_dump(mode="json"))
        self._store.put_quarantine_view(view.model_dump(mode="json"))
        return view

    # -- reads ------------------------------------------------------------
    def _items_for(self, case_id: str) -> list[QuarantineItem]:
        return [
            QuarantineItem.model_validate(p)
            for p in self._store.quarantine_raw_list_items(case_id)
        ]

    def get_item(self, quarantine_id: str) -> QuarantineItem | None:
        payload = self._store.quarantine_raw_get_item(quarantine_id)
        return QuarantineItem.model_validate(payload) if payload else None

    def items_for_case(self, case_id: str) -> list[QuarantineItem]:
        return self._items_for(case_id)

    def all_items(self) -> list[QuarantineItem]:
        return [
            QuarantineItem.model_validate(p)
            for p in self._store.quarantine_raw_list_all_items()
        ]

    def view_for(self, quarantine_id: str) -> ReleasedTextView | None:
        views = [
            ReleasedTextView.model_validate(p)
            for p in self._store.list_quarantine_views(quarantine_id)
        ]
        return views[-1] if views else None

    def views_for_case(self, case_id: str) -> list[ReleasedTextView]:
        return [
            ReleasedTextView.model_validate(p)
            for p in self._store.list_quarantine_views_for_case(case_id)
        ]

    def acts_for_case(self, case_id: str) -> list[QuarantineReleaseAct]:
        return [
            QuarantineReleaseAct.model_validate(p)
            for p in self._store.list_quarantine_acts(case_id)
        ]

    def view_for_slot(
        self, case_id: str, source_surface: str, field_name: str
    ) -> ReleasedTextView | None:
        """The released view standing for one (case, surface, field)
        slot — the read the summary composer uses for the intake
        note. None when the slot has no text, or its text is held:
        a held text has no downstream representation at all."""
        for item in self._items_for(case_id):
            if item.source_surface == source_surface and item.field_name == field_name:
                return self.view_for(item.quarantine_id)
        return None

    def state_of(self, item: QuarantineItem) -> str:
        return STATE_RELEASED if self.view_for(item.quarantine_id) else STATE_HELD
