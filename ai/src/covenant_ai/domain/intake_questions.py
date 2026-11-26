"""Intake question bank for the Covenant owner interview.

Design
------
Questions are DATA, not prompts hidden inside an LLM call:
  - the code decides WHICH questions are still open (missing_required /
    next_questions); the LLM only phrases them naturally and asks follow-ups;
  - "is the interview complete?" is answered by is_complete(), never by the LLM.

How the questions were derived
------------------------------
Backwards from what the valuation needs: CompanyProfile -> industry/peer
selection -> normalised EBITDA -> EV bridge. Each missing input became a
question. Owner-goal questions (Stage.LATER) belong to the transaction-path
work and are out of scope for the valuation MVP.

maps_to
-------
Name of the CompanyProfile field the answer fills, or None when CompanyProfile
has no such field yet (those answers need a model extension before they can be
used downstream). Only company_name, industry, geography, revenue, ebitda and
ebitda_period are known to exist today; check models.py.

NOT VALIDATED YET
-----------------
The wording and the required/optional split have not been reviewed by anyone
with M&A experience. Test with 2-3 role-played owners (vague, wrong numbers,
missing data) and ideally have a practitioner read the list once.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from covenant_ai.domain.industry import Industry


class Stage(StrEnum):
    MVP = "mvp"  # needed for the valuation flow
    LATER = "later"  # owner goals / transaction paths


class AnswerType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    PERCENT = "percent"
    CHOICE = "choice"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class Question:
    id: str
    group: str
    prompt: str
    why: str  # shown to the owner and used in the report
    answer_type: AnswerType
    required: bool = False
    stage: Stage = Stage.MVP
    maps_to: str | None = None
    choices: tuple[str, ...] = ()
    default: str | None = None


QUESTION_BANK: tuple[Question, ...] = (
    # ---- Business -------------------------------------------------------
    Question(
        id="company_name",
        group="business",
        prompt="What is the legal or trading name of the company?",
        why="Identifies the case in the report.",
        answer_type=AnswerType.TEXT,
        required=True,
        maps_to="company_name",
    ),
    Question(
        id="business_description",
        group="business",
        prompt="In two or three sentences: what does the company sell, and to whom?",
        why="Used to propose an industry and to judge how comparable the peers are.",
        answer_type=AnswerType.TEXT,
        required=True,
        maps_to="business_description",
    ),
    Question(
        id="industry",
        group="business",
        prompt="Which of these industries is closest to your business?",
        why="Decides which listed companies are used as peers. Proposed by the "
        "assistant, confirmed by you.",
        answer_type=AnswerType.CHOICE,
        required=True,
        maps_to="industry",
        choices=tuple(i.value for i in Industry),
    ),
    Question(
        id="geography",
        group="business",
        prompt="Where does most of your revenue come from (country or region)?",
        why="Peers are compared within a similar market where possible.",
        answer_type=AnswerType.TEXT,
        required=True,
        maps_to="geography",
    ),
    Question(
        id="revenue_model",
        group="business",
        prompt="How do you earn revenue: recurring subscriptions, one-off sales, "
        "project fees, or per-transaction fees?",
        why="Recurring revenue is usually valued differently from one-off revenue.",
        answer_type=AnswerType.TEXT,
        maps_to="revenue_model",
    ),
    Question(
        id="customer_concentration",
        group="business",
        prompt="What share of your last-twelve-months revenue comes from your "
        "single largest customer?",
        why="High customer concentration is a valuation risk and is flagged in the report.",
        answer_type=AnswerType.PERCENT,
        maps_to="customer_concentration_pct",
    ),
    # ---- Financials -----------------------------------------------------
    Question(
        id="revenue",
        group="financials",
        prompt="What was your revenue in the latest completed fiscal year?",
        why="Used for the size comparison against peers.",
        answer_type=AnswerType.NUMBER,
        required=True,
        maps_to="revenue",
    ),
    Question(
        id="ebitda",
        group="financials",
        prompt="What was your EBITDA in the same completed fiscal year, as shown in your accounts?",
        why="The valuation multiplies this by peer EV/EBITDA multiples.",
        answer_type=AnswerType.NUMBER,
        required=True,
        maps_to="ebitda",
    ),
    Question(
        id="ebitda_period",
        group="financials",
        prompt="Which trailing financial basis do these figures cover (FY or LTM)?",
        why="All peer comparables are matched on the exact same period basis (annual FY or rolling LTM).",
        answer_type=AnswerType.TEXT,
        required=True,
        maps_to="ebitda_period",
        default="FY",
    ),
    Question(
        id="revenue_growth",
        group="financials",
        prompt="By what percentage did revenue grow compared with the prior twelve months?",
        why="Faster-growing businesses usually deserve higher multiples; "
        "used to rank peers.",
        answer_type=AnswerType.PERCENT,
        maps_to="revenue_growth_pct",
    ),
    Question(
        id="owner_compensation_adjustment",
        group="financials",
        prompt="Is the owner paid more or less than a hired manager would cost? "
        "By roughly how much per year?",
        why="Normalises EBITDA so the buyer sees the true earning power.",
        answer_type=AnswerType.NUMBER,
    ),
    Question(
        id="one_off_items",
        group="financials",
        prompt="Were there one-off income or cost items in this period (lawsuit, "
        "asset sale, unusual repair)? Please list them with amounts.",
        why="One-offs distort EBITDA and are removed in normalisation.",
        answer_type=AnswerType.TEXT,
    ),
    Question(
        id="capex",
        group="financials",
        prompt="How much did you spend on equipment and facilities last year?",
        why="Capital-heavy businesses are not comparable to asset-light peers.",
        answer_type=AnswerType.NUMBER,
        maps_to="capex",
    ),
    # ---- Balance sheet (needed only if equity value is requested) --------
    Question(
        id="total_debt",
        group="balance_sheet",
        prompt="What is your total outstanding debt (loans, leases, owner loans)?",
        why="Needed to convert enterprise value into equity value.",
        answer_type=AnswerType.NUMBER,
    ),
    Question(
        id="cash",
        group="balance_sheet",
        prompt="How much cash does the company hold?",
        why="Needed to convert enterprise value into equity value.",
        answer_type=AnswerType.NUMBER,
    ),
    # ---- Owner goals (transaction paths, out of MVP scope) ---------------
    Question(
        id="sale_timeline",
        group="goals",
        prompt="When would you ideally complete a transaction?",
        why="Different timelines favour different transaction paths.",
        answer_type=AnswerType.CHOICE,
        stage=Stage.LATER,
        choices=("within_12_months", "one_to_three_years", "over_three_years", "not_sure"),
    ),
    Question(
        id="retain_control",
        group="goals",
        prompt="Do you want to keep control of the company after the transaction?",
        why="Rules some transaction paths in or out.",
        answer_type=AnswerType.BOOLEAN,
        stage=Stage.LATER,
    ),
    Question(
        id="successor_available",
        group="goals",
        prompt="Is there a family member or manager who could take over?",
        why="Determines whether an internal succession path exists.",
        answer_type=AnswerType.BOOLEAN,
        stage=Stage.LATER,
    ),
    Question(
        id="motivation",
        group="goals",
        prompt="What is the main reason you are considering a transaction?",
        why="Frames which outcome matters most to you.",
        answer_type=AnswerType.TEXT,
        stage=Stage.LATER,
    ),
)

_BY_ID: dict[str, Question] = {q.id: q for q in QUESTION_BANK}
assert len(_BY_ID) == len(QUESTION_BANK), "duplicate question ids"


def questions(stage: Stage = Stage.MVP) -> list[Question]:
    return [q for q in QUESTION_BANK if q.stage == stage]


def _answered(q: Question, answers: Mapping[str, object]) -> bool:
    value = answers.get(q.id)
    if isinstance(value, str):
        value = value.strip()
    if value not in (None, ""):
        return True
    return q.default is not None  # a default counts as answered


def missing_required(answers: Mapping[str, object], stage: Stage = Stage.MVP) -> list[Question]:
    return [q for q in questions(stage) if q.required and not _answered(q, answers)]


def is_complete(answers: Mapping[str, object], stage: Stage = Stage.MVP) -> bool:
    return not missing_required(answers, stage)


def next_questions(
    answers: Mapping[str, object], limit: int = 3, stage: Stage = Stage.MVP
) -> list[Question]:
    """Open questions in bank order, required ones first."""
    open_qs = [q for q in questions(stage) if not _answered(q, answers)]
    open_qs.sort(key=lambda q: not q.required)  # stable: keeps bank order within groups
    return open_qs[:limit]
