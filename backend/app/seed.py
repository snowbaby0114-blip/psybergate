"""Deterministic demo data for the formulary and dispensing system.

    python -m app.seed [--medicines 500] [--dispenses 6000] [--patients 2500] [--years 2]
                       [--random-seed 20260901] [--reset]

The counts are of generated rows; the demo fixtures (DEMO_* below, patient PT-DEMO) come on top.
Dates are SAST business dates and instants are stored as UTC, exactly as the API does. The
generated history ends at the start of today (SAST), so no dispense, and no log row written
shortly after it, can lie in the future whatever time of day the command runs.

Every dispense is admitted by the pure engine the API itself uses (app.domain.dispense), so the
data never holds a dispense the API would have rejected. An independent SQL self-check, which does
its own time-zone arithmetic in PostgreSQL, proves that afterwards.
"""

import argparse
import base64
import sys
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import accumulate
from random import Random
from typing import Any, NamedTuple

from sqlalchemy import Connection, Engine, insert, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.db import create_db_engine
from app.domain.dispense import Candidate, PriorDispense, evaluate_dispense
from app.domain.idempotency import request_fingerprint
from app.domain.rules import RuleVersion, rule_in_force, rule_status
from app.domain.timeutil import local_day_start_utc, to_local_date, window_start_date
from app.models import Dispense, DispenseAttempt, FormularyRule, Medicine

DEFAULT_MEDICINES = 500
DEFAULT_DISPENSES = 6000
DEFAULT_PATIENTS = 2500
DEFAULT_YEARS = 2
DEFAULT_RANDOM_SEED = 20260901

DEMO_PATIENT = "PT-DEMO"
DEMO_AUTH = "DEMO-AUTH"
DEMO_TIGHT = "DEMO-TIGHT"
DEMO_INACTIVE = "DEMO-INACTIVE"
DEMO_HISTORY = "DEMO-HISTORY"
DEMO_CODES = (DEMO_AUTH, DEMO_TIGHT, DEMO_INACTIVE, DEMO_HISTORY)

TABLES = ("medicines", "formulary_rules", "dispenses", "dispense_attempts")
CHUNK_SIZE = 2000
# Namespace of the uuid5 values that make up the idempotency keys of seeded dispenses.
SEED_NAMESPACE = uuid.UUID("0b9d5c8e-6f3a-4d57-9a4e-2f7c1d8b3e61")
# The API logs a dispense moments after it happened: a few seconds to a few minutes.
MIN_LOGGING_DELAY_SECONDS = 3
MAX_LOGGING_DELAY_SECONDS = 20 * 60

DAYS_PER_YEAR = 365
# Keeps every generated date inside the 1990 to 2100 the API accepts.
MAX_YEARS = 25
# Rule history reaches this far back before the first dispense (plus up to the extra jitter below):
# half a year, so that the 30-day window of even the oldest dispense is covered by a rule.
HISTORY_MARGIN_DAYS = 183
HISTORY_JITTER_DAYS = 180

MIN_PERIODS = 3
MAX_PERIODS = 5
MIN_PERIOD_DAYS = 30
INACTIVE_SHARE = 0.05
SCHEDULED_SHARE = 0.10
AUTHORISATION_SHARE = 0.15
# Share of periods whose 30-day cap is below the single-dispense maximum ("not always larger").
TIGHT_MONTHLY_CAP_SHARE = 0.08

PER_DISPENSE_LIMITS = (5, 10, 14, 20, 28, 30, 50, 60, 90, 100, 120)
MONTHLY_MULTIPLES = (1, 2, 2, 3, 4, 6)

# How much of a rule's limit a typical dispense uses, and when pharmacies dispense (SAST).
FILL_FRACTIONS = (0.25, 0.5, 0.5, 0.75, 1.0)
OPENING_HOUR = 8
CLOSING_HOUR = 18
NIGHT_HOURS = 2
# Share of dispenses made in the first NIGHT_HOURS of a SAST day, whose UTC date is still the
# previous one. Without them nothing in the data would tell a business date from a UTC date.
NIGHT_SHARE = 0.03
# Share of single dispenses made just after midnight on the day a rule starts: governed by the new
# rule although the UTC date still belongs to the old one.
BOUNDARY_SHARE = 0.03
# Share of dispenses that carry an authorisation reference although their rule does not need one.
OPTIONAL_AUTHORISATION_SHARE = 0.05

# Repeat dispenses come as refill chains: the same patient, the same medicine, 2 to 5 weeks apart.
REFILL_MIN_GAP_DAYS = 14
REFILL_MAX_GAP_DAYS = 35
MAX_DISPENSES_PER_PATIENT = 40
# A few chronic patients collect many of the repeat dispenses.
CHRONIC_PATIENT_SHARE = 0.05
CHRONIC_REPEAT_SHARE = 0.5
# Medicines are not equally popular: the weight of the n-th most popular is 1 / n ** exponent.
POPULARITY_EXPONENT = 0.7
# Rounds of extra visits that replace the ones the engine rejected, before giving up.
MAX_ROUNDS = 10
# PostgreSQL's SQLSTATE for "relation does not exist": the migrations have not been applied.
UNDEFINED_TABLE = "42P01"


class Limits(NamedTuple):
    max_quantity_per_dispense: int
    max_quantity_per_30_days: int
    requires_authorisation: bool


@dataclass(frozen=True)
class RulePeriod:
    effective_from: date
    effective_to: date | None
    limits: Limits
    created_at: datetime


@dataclass(frozen=True)
class MedicineSpec:
    code: str
    name: str
    form: str
    strength_value: Decimal
    strength_unit: str
    is_active: bool
    created_at: datetime
    periods: tuple[RulePeriod, ...]


class _Form(NamedTuple):
    name: str
    weight: int
    strength_unit: str
    strengths: tuple[str, ...]


# Each form has the unit it is plausibly measured in; the weights make tablets the commonest.
_FORMS = (
    _Form("tablet", 40, "mg", ("5", "10", "20", "40", "100", "250", "500", "850")),
    _Form("capsule", 20, "mg", ("20", "50", "100", "250", "500")),
    _Form("suspension", 12, "mg/5 mL", ("125", "250", "400")),
    _Form("injection", 12, "mg/mL", ("1", "10", "40", "100")),
    _Form("cream", 10, "%", ("0.5", "1", "2", "5")),
    _Form("inhaler", 6, "mcg/dose", ("50", "100", "250")),
)
_FORM_WEIGHTS = [form.weight for form in _FORMS]
_FORM_BY_NAME = {form.name: form for form in _FORMS}

# Invented syllables, chosen so that the combinations sound like medicines without being any.
# fmt: off
_NAME_STARTS = (
    "Bra", "Cel", "Dor", "Fen", "Gal", "Hel", "Jun", "Kal", "Lum", "Mar", "Nov", "Orn",
    "Pyr", "Qui", "Rav", "Sol", "Tev", "Ulm", "Vex", "Wen", "Xan", "Yor", "Zen",
)
_NAME_MIDDLES = ("", "a", "o", "i", "ta", "vi", "ro", "mi")
_NAME_ENDS = (
    "pine", "zole", "pril", "mide", "tin", "vir", "dine", "xan", "lol", "fen", "ride", "cin",
)
# fmt: on


def _drug_name(rng: Random) -> str:
    return rng.choice(_NAME_STARTS) + rng.choice(_NAME_MIDDLES) + rng.choice(_NAME_ENDS)


def _limits(rng: Random) -> Limits:
    per_dispense = rng.choice(PER_DISPENSE_LIMITS)
    if rng.random() < TIGHT_MONTHLY_CAP_SHARE:
        per_30_days = max(1, per_dispense // 2)
    else:
        per_30_days = per_dispense * rng.choice(MONTHLY_MULTIPLES)
    return Limits(per_dispense, per_30_days, rng.random() < AUTHORISATION_SHARE)


def _entered_before(rng: Random, start: date, today: date) -> datetime:
    """When a rule starting on `start` was entered: ahead of that day, and never after today
    (a scheduled rule was entered in the past too)."""
    lead = timedelta(days=rng.randint(1, 14), seconds=rng.randrange(86_400))
    return local_day_start_utc(min(start, today)) - lead


def _timeline(
    rng: Random, today: date, steps: Sequence[tuple[date, Limits]]
) -> tuple[RulePeriod, ...]:
    """Tile `steps` (start date and limits of each period, ascending) into contiguous periods:
    each ends on the day the next one starts, and the last one is open-ended."""
    ends = [*(start for start, _ in steps[1:]), None]
    return tuple(
        RulePeriod(start, end, limits, _entered_before(rng, start, today))
        for (start, limits), end in zip(steps, ends, strict=True)
    )


def _period_starts(rng: Random, first: date, last: date, count: int) -> list[date]:
    """`count` start dates from `first` to `last`; interior cuts sit on whole multiples of
    MIN_PERIOD_DAYS after `first`, which keeps every period at least that long."""
    slots = (last - first).days // MIN_PERIOD_DAYS
    cuts = sorted(rng.sample(range(1, slots), count - 2))
    return [first, *(first + timedelta(days=cut * MIN_PERIOD_DAYS) for cut in cuts), last]


def _generated_timeline(
    rng: Random, today: date, years: int, *, scheduled: bool
) -> tuple[RulePeriod, ...]:
    """3 to 5 periods. A scheduled medicine has exactly one period in the future, its last, so
    its timeline reads expired ... in force, scheduled."""
    history = years * DAYS_PER_YEAR + HISTORY_MARGIN_DAYS + rng.randint(0, HISTORY_JITTER_DAYS)
    first = today - timedelta(days=history)
    in_force_since = today - timedelta(days=rng.randint(20, 400))
    count = rng.randint(MIN_PERIODS, MAX_PERIODS)
    starts = _period_starts(rng, first, in_force_since, count - 1 if scheduled else count)
    if scheduled:
        starts.append(today + timedelta(days=rng.randint(10, 90)))
    return _timeline(rng, today, [(start, _limits(rng)) for start in starts])


def _medicine(
    rng: Random, code: str, periods: tuple[RulePeriod, ...], *, is_active: bool
) -> MedicineSpec:
    form = rng.choices(_FORMS, weights=_FORM_WEIGHTS)[0]
    return MedicineSpec(
        code=code,
        name=_drug_name(rng),
        form=form.name,
        strength_value=Decimal(rng.choice(form.strengths)),
        strength_unit=form.strength_unit,
        is_active=is_active,
        created_at=periods[0].created_at - timedelta(days=rng.randint(1, 30)),
        periods=periods,
    )


def _demo_medicines(rng: Random, today: date, years: int) -> list[MedicineSpec]:
    """The four fixtures that each demonstrate one behaviour of the rules."""
    span = years * DAYS_PER_YEAR
    window_start = today - timedelta(days=span)
    first = today - timedelta(days=span + HISTORY_MARGIN_DAYS + 20)

    def ago(days: int) -> date:
        return today - timedelta(days=days)

    def into_window(percent: int) -> date:
        return window_start + timedelta(days=span * percent // 100)

    def demo(
        code: str,
        name: str,
        form: str,
        strength: str,
        steps: Sequence[tuple[date, Limits]],
        *,
        is_active: bool = True,
    ) -> MedicineSpec:
        periods = _timeline(rng, today, steps)
        return MedicineSpec(
            code=code,
            name=name,
            form=form,
            strength_value=Decimal(strength),
            strength_unit=_FORM_BY_NAME[form].strength_unit,
            is_active=is_active,
            created_at=periods[0].created_at - timedelta(days=7),
            periods=periods,
        )

    return [
        demo(
            DEMO_AUTH,
            "Demo Authorisation Required",
            "tablet",
            "20",
            [
                (first, Limits(30, 60, True)),
                (ago(300), Limits(60, 120, True)),
                (ago(100), Limits(20, 40, True)),
            ],
        ),
        demo(
            DEMO_TIGHT,
            "Demo Tight Limits",
            "capsule",
            "50",
            [
                (first, Limits(30, 90, False)),
                (ago(300), Limits(20, 40, False)),
                (ago(150), Limits(10, 20, False)),
            ],
        ),
        demo(
            DEMO_INACTIVE,
            "Demo Inactive Medicine",
            "tablet",
            "10",
            [
                (first, Limits(50, 100, False)),
                (ago(400), Limits(30, 60, False)),
                (ago(90), Limits(30, 60, False)),
            ],
            is_active=False,
        ),
        demo(
            DEMO_HISTORY,
            "Demo Rule History",
            "suspension",
            "250",
            [
                (first, Limits(100, 300, False)),
                (into_window(15), Limits(60, 120, False)),
                (into_window(40), Limits(60, 120, True)),
                (into_window(65), Limits(30, 90, False)),
                (today + timedelta(days=30), Limits(20, 60, False)),
            ],
        ),
    ]


def generate_catalogue(rng: Random, count: int, today: date, years: int) -> list[MedicineSpec]:
    """`count` generated medicines (codes MED-00001..) followed by the four demo medicines.

    The inactive and the scheduled medicines are drawn without replacement, so the shares hold
    exactly instead of on average, however small the catalogue.
    """
    inactive = set(rng.sample(range(count), round(count * INACTIVE_SHARE)))
    scheduled = set(rng.sample(range(count), round(count * SCHEDULED_SHARE)))
    generated = [
        _medicine(
            rng,
            f"MED-{number + 1:05d}",
            _generated_timeline(rng, today, years, scheduled=number in scheduled),
            is_active=number not in inactive,
        )
        for number in range(count)
    ]
    return [*generated, *_demo_medicines(rng, today, years)]


class SeedError(Exception):
    """The requested dataset cannot be generated."""


@dataclass(frozen=True)
class CatalogueEntry:
    """A stored medicine: its database id and its rules as the engine sees them."""

    id: int
    code: str
    is_active: bool
    rules: tuple[RuleVersion, ...]


def build_entries(
    specs: Sequence[MedicineSpec],
    medicine_ids: Mapping[str, int],
    rule_ids: Mapping[tuple[int, date], int],
) -> list[CatalogueEntry]:
    """Attach the ids the database assigned. `rule_ids` is keyed by (medicine id, effective_from),
    which identifies a rule because the periods of a medicine never overlap."""
    entries = []
    for spec in specs:
        medicine_id = medicine_ids[spec.code]
        rules = tuple(
            RuleVersion(
                id=rule_ids[(medicine_id, period.effective_from)],
                effective_from=period.effective_from,
                effective_to=period.effective_to,
                max_quantity_per_dispense=period.limits.max_quantity_per_dispense,
                max_quantity_per_30_days=period.limits.max_quantity_per_30_days,
                requires_authorisation=period.limits.requires_authorisation,
            )
            for period in spec.periods
        )
        entries.append(CatalogueEntry(medicine_id, spec.code, spec.is_active, rules))
    return entries


def _rule_on(medicine: CatalogueEntry, day: date) -> RuleVersion:
    rule = rule_in_force(medicine.rules, day)
    if rule is None:
        raise SeedError(f"{medicine.code} has no rule in force on {day.isoformat()}")
    return rule


def _opaque(rng: Random, length: int) -> str:
    """`length` (at most 8) random base32 characters, meaningless by construction."""
    return base64.b32encode(rng.randbytes(5)).decode()[:length]


def generate_patient_refs(rng: Random, count: int) -> list[str]:
    """Distinct opaque references: PT- and 8 random base32 characters, nothing personal."""
    refs: dict[str, None] = {}  # an ordered set, so that the result is reproducible
    while len(refs) < count:
        refs[f"PT-{_opaque(rng, 8)}"] = None
    return list(refs)


@dataclass(frozen=True)
class DispenseWindow:
    """The business days a seeded dispense may be dated: `days` days ending the day before today."""

    today: date
    days: int

    @property
    def start(self) -> date:
        return self.today - timedelta(days=self.days)


@dataclass(frozen=True)
class Visit:
    """A candidate dispense. It suits its rule, but only the engine decides whether it stands."""

    patient_ref: str
    medicine: CatalogueEntry
    dispensed_at: datetime
    quantity: int
    authorisation_ref: str | None


@dataclass(frozen=True)
class AdmittedDispense:
    index: int
    patient_ref: str
    medicine: CatalogueEntry
    rule_id: int
    dispensed_at: datetime
    quantity: int
    authorisation_ref: str | None


class Ledger:
    """The dispenses admitted so far. A visit is admitted only if the pure engine accepts it
    against what is already recorded for the same patient and medicine; otherwise it is skipped."""

    def __init__(self) -> None:
        self.admitted: list[AdmittedDispense] = []
        self.skipped = 0
        self.skipped_reasons: Counter[str] = Counter()
        self._history: dict[tuple[str, int], list[PriorDispense]] = {}

    def admit(self, visit: Visit) -> AdmittedDispense | None:
        medicine = visit.medicine
        history = self._history.setdefault((visit.patient_ref, medicine.id), [])
        evaluation = evaluate_dispense(
            medicine_active=medicine.is_active,
            rules=medicine.rules,
            history=history,
            candidate=Candidate(visit.dispensed_at, visit.quantity, visit.authorisation_ref),
        )
        rule = evaluation.rule
        # An accepted evaluation always names its rule; testing both keeps the type narrow.
        if not evaluation.accepted or rule is None:
            self.skipped += 1
            self.skipped_reasons.update(violation.code for violation in evaluation.violations)
            return None
        dispense = AdmittedDispense(
            index=len(self.admitted),
            patient_ref=visit.patient_ref,
            medicine=medicine,
            rule_id=rule.id,
            dispensed_at=visit.dispensed_at,
            quantity=visit.quantity,
            authorisation_ref=visit.authorisation_ref,
        )
        self.admitted.append(dispense)
        history.append(PriorDispense(dispense.index, dispense.dispensed_at, dispense.quantity))
        return dispense


class _VisitPlanner:
    """Draws plausible visits from a seeded stream.

    Quantities and authorisation references suit the rule in force on the day, so the engine
    rejects a visit only when earlier dispenses of the same patient and medicine have already
    used up the 30-day allowance. A patient's dispenses come in refill chains (one medicine,
    2 to 5 weeks apart), which is what makes that allowance matter.
    """

    def __init__(
        self, rng: Random, medicines: Sequence[CatalogueEntry], window: DispenseWindow
    ) -> None:
        self._rng = rng
        self._window = window
        # Shuffled first, so that popularity is not tied to the order of the codes.
        self._medicines = rng.sample(medicines, len(medicines))
        self._cumulative_popularity = list(
            accumulate(1 / rank**POPULARITY_EXPONENT for rank in range(1, len(self._medicines) + 1))
        )

    def visits_for(self, patient_ref: str, count: int) -> list[Visit]:
        visits: list[Visit] = []
        for length in _refill_chain_lengths(count):
            visits.extend(self._refill_chain(patient_ref, self._popular_medicine(), length))
        return visits

    def extra_visits(self, patients: Sequence[str], count: int) -> list[Visit]:
        return [
            self._single_visit(self._rng.choice(patients), self._popular_medicine())
            for _ in range(count)
        ]

    def _popular_medicine(self) -> CatalogueEntry:
        return self._rng.choices(self._medicines, cum_weights=self._cumulative_popularity)[0]

    def _is_night(self) -> bool:
        return self._rng.random() < NIGHT_SHARE

    def _refill_chain(self, patient_ref: str, medicine: CatalogueEntry, length: int) -> list[Visit]:
        if length == 1:
            return [self._single_visit(patient_ref, medicine)]
        rng, window = self._rng, self._window
        # The chain must fit the window, whatever its length.
        longest_gap = min(REFILL_MAX_GAP_DAYS, (window.days - 1) // length)
        shortest_gap = min(REFILL_MIN_GAP_DAYS, longest_gap)
        gaps = [rng.randint(shortest_gap, longest_gap) for _ in range(length - 1)]
        day = window.start + timedelta(days=rng.randrange(window.days - sum(gaps)))
        visits = [self._visit(patient_ref, medicine, day, night=self._is_night())]
        for gap in gaps:
            day += timedelta(days=gap)
            visits.append(self._visit(patient_ref, medicine, day, night=self._is_night()))
        return visits

    def _single_visit(self, patient_ref: str, medicine: CatalogueEntry) -> Visit:
        rng, window = self._rng, self._window
        rule_starts = [
            rule.effective_from
            for rule in medicine.rules
            if window.start < rule.effective_from < window.today
        ]
        if rule_starts and rng.random() < BOUNDARY_SHARE:
            return self._visit(patient_ref, medicine, rng.choice(rule_starts), night=True)
        day = window.start + timedelta(days=rng.randrange(window.days))
        return self._visit(patient_ref, medicine, day, night=self._is_night())

    def _visit(
        self, patient_ref: str, medicine: CatalogueEntry, day: date, *, night: bool
    ) -> Visit:
        rng = self._rng
        rule = _rule_on(medicine, day)
        allowance = min(rule.max_quantity_per_dispense, rule.max_quantity_per_30_days)
        if night:
            seconds = rng.randrange(NIGHT_HOURS * 3600)
        else:
            seconds = rng.randrange(OPENING_HOUR * 3600, CLOSING_HOUR * 3600)
        authorised = rule.requires_authorisation or rng.random() < OPTIONAL_AUTHORISATION_SHARE
        return Visit(
            patient_ref=patient_ref,
            medicine=medicine,
            # SAST has no daylight saving, so a fixed offset from local midnight is wall-clock time.
            dispensed_at=local_day_start_utc(day) + timedelta(seconds=seconds),
            quantity=max(1, round(allowance * rng.choice(FILL_FRACTIONS))),
            authorisation_ref=f"AUTH-{_opaque(rng, 6)}" if authorised else None,
        )


def _refill_chain_lengths(count: int) -> list[int]:
    """Split a patient's dispenses into one chain per medicine: one medicine for up to three
    dispenses, two from four, three from ten."""
    chains = 1 + (count >= 4) + (count >= 10)
    return [count // chains + (index < count % chains) for index in range(chains)]


def _dispenses_per_patient(rng: Random, patients: int, dispenses: int) -> list[int]:
    """Everybody gets one dispense; the remaining ones go half to a few chronic patients and
    half to anybody. The plan gives no patient more than MAX_DISPENSES_PER_PATIENT (visits that
    replace rejected ones can add a few)."""
    counts = [1] * patients
    chronic = max(1, round(patients * CHRONIC_PATIENT_SHARE))
    for _ in range(dispenses - patients):
        if rng.random() < CHRONIC_REPEAT_SHARE:
            index = rng.randrange(chronic)
        else:
            index = rng.randrange(patients)
        while counts[index] >= MAX_DISPENSES_PER_PATIENT:
            index = rng.randrange(patients)
        counts[index] += 1
    return counts


def demo_visits(entries: Mapping[str, CatalogueEntry], window: DispenseWindow) -> list[Visit]:
    """The dispenses of PT-DEMO, one story per demo medicine:

    - DEMO-TIGHT: 18 of the 20 units allowed per 30 days used in the last 30 days.
    - DEMO-AUTH: dispenses that carry the authorisation reference its rules require.
    - DEMO-HISTORY: one dispense in each of its first three periods, and two around the midnight
      on which the fourth begins: 23:30 SAST on the last day of the third and 00:30 SAST on the
      first day of the fourth, which is 22:30 UTC on the same UTC date as the 23:30 one.

    The earliest one opens the dispense window and the latest is two days old.
    """

    def ago(days: int) -> date:
        return window.today - timedelta(days=days)

    def visit(
        code: str, day: date, clock: tuple[int, int], quantity: int, reference: str | None = None
    ) -> Visit:
        hour, minute = clock
        return Visit(
            patient_ref=DEMO_PATIENT,
            medicine=entries[code],
            dispensed_at=local_day_start_utc(day) + timedelta(hours=hour, minutes=minute),
            quantity=quantity,
            authorisation_ref=reference,
        )

    history = entries[DEMO_HISTORY].rules
    fourth_begins = history[3].effective_from
    return [
        visit(DEMO_HISTORY, window.start + timedelta(days=1), (10, 0), 40),
        visit(DEMO_HISTORY, history[1].effective_from + timedelta(days=20), (10, 30), 30),
        visit(DEMO_HISTORY, history[2].effective_from + timedelta(days=20), (9, 45), 20, "AUTH-D1"),
        visit(DEMO_HISTORY, fourth_begins - timedelta(days=1), (23, 30), 10, "AUTH-D2"),
        visit(DEMO_HISTORY, fourth_begins, (0, 30), 10),
        visit(DEMO_AUTH, ago(200), (11, 0), 30, "AUTH-D3"),
        visit(DEMO_AUTH, ago(60), (15, 20), 15, "AUTH-D4"),
        visit(DEMO_TIGHT, ago(24), (9, 15), 6),
        visit(DEMO_TIGHT, ago(12), (14, 40), 6),
        visit(DEMO_TIGHT, ago(2), (11, 5), 6),
    ]


def generate_dispenses(
    rng: Random,
    entries: Sequence[CatalogueEntry],
    patients: Sequence[str],
    dispenses: int,
    window: DispenseWindow,
) -> Ledger:
    """Admit the PT-DEMO fixtures and then `dispenses` dispenses over `patients`, each of whom
    gets at least one.

    Round one plans exactly `dispenses` visits, a first one per patient plus refill chains, and
    admits them in chronological order, so every (patient, medicine) pair is validated the way a
    pharmacy would have recorded it. A patient's earliest visit meets an empty ledger and is never
    rejected. The visits the engine rejects are skipped and replaced by extra visits for random
    patients, round after round, until the requested number has been admitted.
    """
    ledger = Ledger()
    by_code = {entry.code: entry for entry in entries}
    for visit in demo_visits(by_code, window):
        if ledger.admit(visit) is None:
            raise SeedError(
                f"the engine rejected a demo dispense of {visit.medicine.code}: "
                f"{dict(ledger.skipped_reasons)}"
            )
    target = len(ledger.admitted) + dispenses

    on_offer = [entry for entry in entries if entry.is_active and entry.code not in DEMO_CODES]
    planner = _VisitPlanner(rng, on_offer, window)
    counts = _dispenses_per_patient(rng, len(patients), dispenses)
    visits = [
        visit
        for patient_ref, count in zip(patients, counts, strict=True)
        for visit in planner.visits_for(patient_ref, count)
    ]
    missing = dispenses
    for _ in range(MAX_ROUNDS):
        for visit in sorted(visits, key=lambda planned: planned.dispensed_at):
            ledger.admit(visit)
        missing = target - len(ledger.admitted)
        if missing == 0:
            return ledger
        visits = planner.extra_visits(patients, missing)
    raise SeedError(
        f"{missing} of {dispenses} dispenses could not be placed after {MAX_ROUNDS} rounds; "
        "use more --medicines or --patients"
    )


class DatabaseNotEmpty(Exception):
    """The database already holds data and --reset was not given."""


@dataclass(frozen=True)
class Expected:
    """What the dataset was generated to contain, for the self-check to hold the database to."""

    medicines: int
    dispenses: int
    patients: int
    today: date
    years: int


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DatabaseStats:
    """What the database holds, counted by SQL."""

    medicines: int
    inactive_medicines: int
    rule_periods: int
    scheduled_periods: int
    dispenses: int
    patients: int
    attempts: int
    oldest: date | None
    newest: date | None


@dataclass(frozen=True)
class Verification:
    stats: DatabaseStats
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


# Business dates in these queries come from PostgreSQL's own time-zone database, not from the
# Python helpers that generated the data, so that the two implementations check each other.
_SAST_DATE = "(d.dispensed_at AT TIME ZONE 'Africa/Johannesburg')::date"

# Each query counts the rows that break the invariant, so it must return 0.
_ZERO_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "every medicine has 3 to 5 rule periods",
        """
        SELECT count(*) FROM (
            SELECT m.id FROM medicines m LEFT JOIN formulary_rules r ON r.medicine_id = m.id
            GROUP BY m.id HAVING count(r.id) NOT BETWEEN 3 AND 5
        ) AS offenders
        """,
    ),
    (
        "periods tile each medicine's timeline and the last one is open-ended",
        """
        SELECT count(*) FROM (
            SELECT effective_to,
                   lead(effective_from) OVER (
                       PARTITION BY medicine_id ORDER BY effective_from) AS next_from
            FROM formulary_rules
        ) AS ordered WHERE effective_to IS DISTINCT FROM next_from
        """,
    ),
    (
        "no two periods of a medicine overlap",
        """
        SELECT count(*) FROM formulary_rules a JOIN formulary_rules b
          ON a.medicine_id = b.medicine_id AND a.id < b.id
         AND daterange(a.effective_from, a.effective_to, '[)')
             && daterange(b.effective_from, b.effective_to, '[)')
        """,
    ),
    (
        "every rule history starts early enough to cover the whole window",
        """
        SELECT count(*) FROM (
            SELECT min(effective_from) AS first_from FROM formulary_rules GROUP BY medicine_id
        ) AS firsts WHERE first_from > :latest_first_start
        """,
    ),
    (
        "every dispense's rule covers its SAST business date",
        f"""
        SELECT count(*) FROM dispenses d WHERE NOT EXISTS (
            SELECT 1 FROM formulary_rules r
            WHERE r.id = d.rule_id AND r.medicine_id = d.medicine_id
              AND daterange(r.effective_from, r.effective_to, '[)') @> {_SAST_DATE}
        )
        """,
    ),
    (
        "no dispense exceeds its rule's per-dispense limit",
        """
        SELECT count(*) FROM dispenses d JOIN formulary_rules r ON r.id = d.rule_id
        WHERE d.quantity > r.max_quantity_per_dispense
        """,
    ),
    (
        "no 30-business-day window exceeds its rule's 30-day limit",
        f"""
        WITH daily AS (
            SELECT d.patient_ref, d.medicine_id, {_SAST_DATE} AS business_date,
                   sum(d.quantity) AS quantity
            FROM dispenses d GROUP BY d.patient_ref, d.medicine_id, business_date
        ), windowed AS (
            SELECT patient_ref, medicine_id, business_date,
                   sum(quantity) OVER (
                       PARTITION BY patient_ref, medicine_id ORDER BY business_date
                       RANGE BETWEEN INTERVAL '29 days' PRECEDING AND CURRENT ROW) AS total
            FROM daily
        )
        SELECT count(*) FROM dispenses d
        JOIN windowed w ON w.patient_ref = d.patient_ref AND w.medicine_id = d.medicine_id
                       AND w.business_date = {_SAST_DATE}
        JOIN formulary_rules r ON r.id = d.rule_id
        WHERE w.total > r.max_quantity_per_30_days
        """,
    ),
    (
        "every dispense has the authorisation reference its rule requires",
        """
        SELECT count(*) FROM dispenses d JOIN formulary_rules r ON r.id = d.rule_id
        WHERE r.requires_authorisation
          AND (d.authorisation_ref IS NULL OR btrim(d.authorisation_ref) = '')
        """,
    ),
    (
        "no dispense is of an inactive medicine",
        """
        SELECT count(*) FROM dispenses d JOIN medicines m ON m.id = d.medicine_id
        WHERE NOT m.is_active
        """,
    ),
    (
        "every dispense has a matching accepted attempt",
        """
        SELECT count(*) FROM dispenses d WHERE NOT EXISTS (
            SELECT 1 FROM dispense_attempts a
            WHERE a.dispense_id = d.id AND a.outcome = 'accepted'
              AND a.idempotency_key = d.idempotency_key AND a.medicine_id = d.medicine_id
              AND a.patient_ref = d.patient_ref AND a.quantity = d.quantity
              AND a.dispensed_at = d.dispensed_at
              AND a.authorisation_ref IS NOT DISTINCT FROM d.authorisation_ref
        )
        """,
    ),
    (
        "no dispense is logged before it happened or in the future",
        "SELECT count(*) FROM dispenses WHERE created_at < dispensed_at OR created_at > now()",
    ),
)

_STATS = f"""
    SELECT
        (SELECT count(*) FROM medicines) AS medicines,
        (SELECT count(*) FROM medicines WHERE NOT is_active) AS inactive_medicines,
        (SELECT count(*) FROM formulary_rules) AS rule_periods,
        (SELECT count(*) FROM formulary_rules WHERE effective_from > :today) AS scheduled_periods,
        (SELECT count(*) FROM dispenses) AS dispenses,
        (SELECT count(DISTINCT patient_ref) FROM dispenses) AS patients,
        (SELECT count(*) FROM dispense_attempts) AS attempts,
        (SELECT min({_SAST_DATE}) FROM dispenses d) AS oldest,
        (SELECT max({_SAST_DATE}) FROM dispenses d) AS newest
"""


def verify(connection: Connection, expected: Expected) -> Verification:
    """Hold the database to what was generated and to the invariants that make the data valid."""
    window_start = expected.today - timedelta(days=expected.years * DAYS_PER_YEAR)
    parameters = {
        "today": expected.today,
        "latest_first_start": window_start - timedelta(days=HISTORY_MARGIN_DAYS),
    }
    checks = []
    for name, sql in _ZERO_CHECKS:
        offenders = connection.execute(text(sql), parameters).scalar_one()
        checks.append(CheckResult(name, offenders == 0, f"offending rows: {offenders:,}"))

    stats = DatabaseStats(**connection.execute(text(_STATS), parameters).mappings().one())
    week = timedelta(days=7)
    opens = stats.oldest is not None and stats.oldest <= window_start + week
    ends = stats.newest is not None and expected.today - week <= stats.newest < expected.today
    checks += [
        CheckResult(
            "medicines stored",
            stats.medicines == expected.medicines,
            f"expected {expected.medicines:,}, found {stats.medicines:,}",
        ),
        CheckResult(
            "dispenses stored",
            stats.dispenses == expected.dispenses,
            f"expected {expected.dispenses:,}, found {stats.dispenses:,}",
        ),
        CheckResult(
            "one accepted attempt per dispense, and no others",
            stats.attempts == stats.dispenses,
            f"{stats.dispenses:,} dispenses, {stats.attempts:,} attempts",
        ),
        CheckResult(
            "no fewer patient refs than requested",
            stats.patients >= expected.patients,
            f"expected at least {expected.patients:,}, found {stats.patients:,}",
        ),
        CheckResult(
            "history opens within a week of the window start",
            opens,
            f"oldest dispense {stats.oldest}, the window starts {window_start}",
        ),
        CheckResult(
            "history ends within the last week, before today",
            ends,
            f"newest dispense {stats.newest}, today is {expected.today}",
        ),
        CheckResult(
            "some rule period is scheduled to start after today",
            stats.scheduled_periods > 0,
            f"{stats.scheduled_periods:,} scheduled",
        ),
    ]
    return Verification(stats, tuple(checks))


@dataclass(frozen=True)
class Timings:
    generate: float
    insert: float
    verify: float

    @property
    def total(self) -> float:
        return self.generate + self.insert + self.verify


@dataclass(frozen=True)
class SeedSummary:
    random_seed: int
    expected: Expected
    skipped_candidates: int
    demo_notes: tuple[tuple[str, str], ...]
    verification: Verification
    timings: Timings

    @property
    def ok(self) -> bool:
        return self.verification.ok


def _validate_parameters(*, medicines: int, dispenses: int, patients: int, years: int) -> None:
    if medicines < 1 or patients < 1:
        raise ValueError("--medicines and --patients must be at least 1")
    if not 1 <= years <= MAX_YEARS:
        raise ValueError(f"--years must be between 1 and {MAX_YEARS}")
    if dispenses < patients:
        raise ValueError(
            f"--dispenses ({dispenses}) must be at least --patients ({patients}): "
            "every patient ref has at least one dispense"
        )
    if dispenses > patients * MAX_DISPENSES_PER_PATIENT:
        raise ValueError(
            f"--dispenses ({dispenses}) allows more than {MAX_DISPENSES_PER_PATIENT} per patient "
            f"for --patients ({patients}); use more patients"
        )


def _chunks[T](items: Sequence[T]) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), CHUNK_SIZE):
        yield items[start : start + CHUNK_SIZE]


def _is_empty(connection: Connection) -> bool:
    return not any(
        connection.execute(text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one()
        for table in TABLES
    )


def _truncate(connection: Connection) -> None:
    connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


def _insert_catalogue(
    connection: Connection, specs: Sequence[MedicineSpec]
) -> list[CatalogueEntry]:
    """Insert the medicines and their rules and read back the ids PostgreSQL assigned."""
    medicine_rows = [
        {
            "code": spec.code,
            "name": spec.name,
            "form": spec.form,
            "strength_value": spec.strength_value,
            "strength_unit": spec.strength_unit,
            "is_active": spec.is_active,
            "created_at": spec.created_at,
        }
        for spec in specs
    ]
    medicine_ids: dict[str, int] = {}
    for chunk in _chunks(medicine_rows):
        stored = connection.execute(insert(Medicine).returning(Medicine.code, Medicine.id), chunk)
        medicine_ids.update(stored.all())

    rule_rows = [
        {
            "medicine_id": medicine_ids[spec.code],
            "effective_from": period.effective_from,
            "effective_to": period.effective_to,
            "max_quantity_per_dispense": period.limits.max_quantity_per_dispense,
            "max_quantity_per_30_days": period.limits.max_quantity_per_30_days,
            "requires_authorisation": period.limits.requires_authorisation,
            "created_at": period.created_at,
        }
        for spec in specs
        for period in spec.periods
    ]
    rule_ids: dict[tuple[int, date], int] = {}
    for chunk in _chunks(rule_rows):
        stored = connection.execute(
            insert(FormularyRule).returning(
                FormularyRule.id, FormularyRule.medicine_id, FormularyRule.effective_from
            ),
            chunk,
        )
        rule_ids.update({(medicine_id, start): rule_id for rule_id, medicine_id, start in stored})
    return build_entries(specs, medicine_ids, rule_ids)


def _idempotency_key(random_seed: int, index: int) -> str:
    return f"seed-{uuid.uuid5(SEED_NAMESPACE, f'{random_seed}:{index}')}"


def _rows_for(
    dispense: AdmittedDispense, key: str, created_at: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The dispense row, and the accepted attempt row the API would have logged for it. The API
    stamps both in one transaction, so they share `created_at`."""
    dispense_row = {
        "medicine_id": dispense.medicine.id,
        "rule_id": dispense.rule_id,
        "patient_ref": dispense.patient_ref,
        "quantity": dispense.quantity,
        "dispensed_at": dispense.dispensed_at,
        "authorisation_ref": dispense.authorisation_ref,
        "idempotency_key": key,
        "created_at": created_at,
    }
    attempt_row = {
        "idempotency_key": key,
        # The fingerprint the API computes for the same request with the instant given
        # explicitly, so that replaying a seeded key is a replay and not a key reuse.
        "request_hash": request_fingerprint(
            medicine_code=dispense.medicine.code,
            patient_ref=dispense.patient_ref,
            quantity=dispense.quantity,
            dispensed_at=dispense.dispensed_at,
            authorisation_ref=dispense.authorisation_ref,
        ),
        "outcome": "accepted",
        "medicine_code": dispense.medicine.code,
        "medicine_id": dispense.medicine.id,
        "patient_ref": dispense.patient_ref,
        "quantity": dispense.quantity,
        "dispensed_at": dispense.dispensed_at,
        "authorisation_ref": dispense.authorisation_ref,
        "violations": [],
        "created_at": created_at,
    }
    return dispense_row, attempt_row


def _insert_dispenses(
    connection: Connection, admitted: Sequence[AdmittedDispense], *, random_seed: int, rng: Random
) -> None:
    """Insert the dispenses oldest first, so that ids grow with time as they would in real life,
    each followed by its attempt row."""
    ordered = sorted(admitted, key=lambda dispense: (dispense.dispensed_at, dispense.index))
    for chunk in _chunks(ordered):
        dispense_rows: list[dict[str, Any]] = []
        attempt_rows: list[dict[str, Any]] = []
        for dispense in chunk:
            delay = rng.randint(MIN_LOGGING_DELAY_SECONDS, MAX_LOGGING_DELAY_SECONDS)
            dispense_row, attempt_row = _rows_for(
                dispense,
                _idempotency_key(random_seed, dispense.index),
                dispense.dispensed_at + timedelta(seconds=delay),
            )
            dispense_rows.append(dispense_row)
            attempt_rows.append(attempt_row)
        stored = connection.execute(
            insert(Dispense).returning(Dispense.idempotency_key, Dispense.id), dispense_rows
        )
        dispense_ids = dict(stored.all())
        for attempt_row in attempt_rows:
            attempt_row["dispense_id"] = dispense_ids[attempt_row["idempotency_key"]]
        connection.execute(insert(DispenseAttempt), attempt_rows)


def describe_demo(
    entries: Sequence[CatalogueEntry], ledger: Ledger, window: DispenseWindow
) -> tuple[tuple[str, str], ...]:
    """One line per demo fixture, worked out from the data rather than written down."""
    by_code = {entry.code: entry for entry in entries}
    tight = _rule_on(by_code[DEMO_TIGHT], window.today)
    first_day = window_start_date(window.today)
    used = sum(
        dispense.quantity
        for dispense in ledger.admitted
        if dispense.patient_ref == DEMO_PATIENT
        and dispense.medicine.code == DEMO_TIGHT
        and to_local_date(dispense.dispensed_at) >= first_day
    )
    history = by_code[DEMO_HISTORY].rules
    states = Counter(rule_status(rule, window.today) for rule in history)
    demo_dispenses = sum(1 for d in ledger.admitted if d.patient_ref == DEMO_PATIENT)
    limits = (
        f"{tight.max_quantity_per_dispense} per dispense, "
        f"{tight.max_quantity_per_30_days} per 30 days"
    )
    timeline = (
        f"{len(history)} rule periods: {states['expired']} expired, "
        f"{states['in_force']} in force, {states['scheduled']} scheduled"
    )
    return (
        (DEMO_AUTH, "requires an authorisation reference in every period"),
        (DEMO_TIGHT, f"{limits}; {DEMO_PATIENT} has used {used} of them in the last 30 days"),
        (DEMO_INACTIVE, "inactive, so every dispense is rejected"),
        (DEMO_HISTORY, timeline),
        (DEMO_PATIENT, f"{demo_dispenses} dispenses of the medicines above"),
    )


@contextmanager
def _timed(spent: dict[str, float], phase: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        spent[phase] += time.perf_counter() - started


def run_seed(
    engine: Engine,
    *,
    medicines: int,
    dispenses: int,
    patients: int,
    years: int,
    random_seed: int,
    reset: bool,
    today: date | None = None,
) -> SeedSummary:
    """Fill an empty database (or, with `reset`, replace what it holds) in one transaction.

    `today` is the SAST business date the history ends on; it defaults to the current one and is
    injectable so that a fixed `random_seed` and `today` reproduce the data exactly.
    """
    _validate_parameters(medicines=medicines, dispenses=dispenses, patients=patients, years=years)
    if today is None:
        today = to_local_date(datetime.now(UTC))
    window = DispenseWindow(today, years * DAYS_PER_YEAR)

    spent = dict.fromkeys(("generate", "insert", "verify"), 0.0)
    with engine.connect() as connection:
        with _timed(spent, "insert"):
            if reset:
                _truncate(connection)
            elif not _is_empty(connection):
                raise DatabaseNotEmpty(
                    "The database already contains data, so nothing was changed. "
                    "Run with --reset to replace it."
                )
        # One random stream per kind of data, so that asking for more dispenses leaves the
        # catalogue and the patient refs exactly as they were.
        with _timed(spent, "generate"):
            specs = generate_catalogue(Random(f"{random_seed}/catalogue"), medicines, today, years)
            patient_refs = generate_patient_refs(Random(f"{random_seed}/patients"), patients)
        with _timed(spent, "insert"):
            entries = _insert_catalogue(connection, specs)

        rng = Random(f"{random_seed}/dispenses")
        with _timed(spent, "generate"):
            ledger = generate_dispenses(rng, entries, patient_refs, dispenses, window)
        with _timed(spent, "insert"):
            _insert_dispenses(connection, ledger.admitted, random_seed=random_seed, rng=rng)
            connection.execute(text(f"ANALYZE {', '.join(TABLES)}"))

        expected = Expected(
            medicines=len(specs),
            dispenses=len(ledger.admitted),
            patients=len(patient_refs) + 1,
            today=today,
            years=years,
        )
        with _timed(spent, "verify"):
            verification = verify(connection, expected)
        # Data that fails its own check is never saved: leaving the connection uncommitted
        # rolls the whole run back, truncation included.
        if verification.ok:
            with _timed(spent, "insert"):
                connection.commit()

    return SeedSummary(
        random_seed=random_seed,
        expected=expected,
        skipped_candidates=ledger.skipped,
        demo_notes=describe_demo(entries, ledger, window),
        verification=verification,
        timings=Timings(spent["generate"], spent["insert"], spent["verify"]),
    )


def format_summary(summary: SeedSummary) -> str:
    """One screen: what is in the database, the demo fixtures, the self-check and the timings."""
    expected, stats, timings = summary.expected, summary.verification.stats, summary.timings
    medicines = (
        f"includes the {len(DEMO_CODES)} demo medicines; {stats.inactive_medicines:,} inactive"
    )
    periods = f"3 to 5 per medicine; {stats.scheduled_periods:,} scheduled to start after today"
    skipped = f"{summary.skipped_candidates:,} candidates skipped: the engine rejected them"
    span = f"{stats.oldest} to {stats.newest} (SAST business dates)"
    failed = [check for check in summary.verification.checks if not check.ok]
    total = len(summary.verification.checks)
    if failed:
        outcome = [
            f"Self-check FAILED, {len(failed)} of {total} checks; nothing was saved:",
            *(f"  {check.name}: {check.detail}" for check in failed),
        ]
    else:
        outcome = [f"Self-check: all {total} checks passed"]
    spent = f"generate {timings.generate:.2f} s, insert {timings.insert:.2f} s"
    lines = [
        f"Seeded with random seed {summary.random_seed}; history ends before {expected.today}",
        "",
        f"  medicines     {stats.medicines:>7,}  ({medicines})",
        f"  rule periods  {stats.rule_periods:>7,}  ({periods})",
        f"  dispenses     {stats.dispenses:>7,}  ({skipped})",
        f"  attempts      {stats.attempts:>7,}  (all accepted, so seeded keys can be replayed)",
        f"  patient refs  {stats.patients:>7,}  (includes {DEMO_PATIENT})",
        f"  span          {span}",
        "",
        "Demo fixtures",
        *(f"  {code:<14}{note}" for code, note in summary.demo_notes),
        "",
        *outcome,
        f"Timings: {spent}, verify {timings.verify:.2f} s, total {timings.total:.2f} s",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description=(
            "Seed the database with deterministic demo data and verify it. The demo fixtures "
            f"({', '.join(DEMO_CODES)}, {DEMO_PATIENT}) come on top of the counts below."
        ),
    )
    for flag, default, what in (
        ("--medicines", DEFAULT_MEDICINES, "medicines to generate"),
        ("--dispenses", DEFAULT_DISPENSES, "dispenses to generate"),
        ("--patients", DEFAULT_PATIENTS, "distinct patient refs to generate"),
        ("--years", DEFAULT_YEARS, "years of dispense history to generate"),
    ):
        parser.add_argument(flag, type=int, default=default, help=f"{what} (default: %(default)s)")
    parser.add_argument(
        "--random-seed",
        type=int,
        default=DEFAULT_RANDOM_SEED,
        help="the same seed on the same day gives the same data (default: %(default)s)",
    )
    parser.add_argument(
        "--reset", action="store_true", help="delete all existing data and seed again"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_parameters(
            medicines=args.medicines,
            dispenses=args.dispenses,
            patients=args.patients,
            years=args.years,
        )
    except ValueError as error:
        parser.error(str(error))

    engine = create_db_engine()
    try:
        summary = run_seed(
            engine,
            medicines=args.medicines,
            dispenses=args.dispenses,
            patients=args.patients,
            years=args.years,
            random_seed=args.random_seed,
            reset=args.reset,
        )
    except DatabaseNotEmpty as error:
        print(error)
        return 0
    except OperationalError as error:
        print(f"Cannot use the database: {error.orig}", file=sys.stderr)
        return 1
    except ProgrammingError as error:
        if getattr(error.orig, "sqlstate", None) != UNDEFINED_TABLE:
            raise
        print("The database has no tables yet: run `alembic upgrade head` first.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(format_summary(summary))
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
