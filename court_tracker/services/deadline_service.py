"""
Procedural deadline engine.

All deadline rules are stored in DEADLINE_RULES. The engine can:
  - calculate a deadline date from a trigger date + rule
  - auto-create deadlines when certain events are saved for a case
"""
import calendar
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------

DEADLINE_RULES: dict[str, dict] = {

    # ── АПК – арбитражный процесс ─────────────────────────────────────────
    "apk_appeal": {
        "name": "Апелляционная жалоба (АПК)",
        "applicable_to": ["kad"],
        "applicable_case_types": ["all"],
        "trigger_event": "decision_date",
        "calc_type": "calendar_months",
        "calc_value": 1,
        "statute_reference": "АПК РФ, ст. 259 ч. 1",
        "statute_article": "ст. 259 АПК",
        "calculation_note": (
            "1 месяц со дня изготовления решения в полном объёме. "
            "Если истекает в выходной — переносится на ближайший рабочий день (ст. 114 АПК)."
        ),
        "auto_create": True,
    },
    "apk_appeal_simplified": {
        "name": "Апелляция (упрощённое производство, АПК)",
        "applicable_to": ["kad"],
        "applicable_case_types": ["all"],
        "trigger_event": "decision_date",
        "calc_type": "working_days",
        "calc_value": 15,
        "statute_reference": "АПК РФ, ст. 272.1",
        "statute_article": "ст. 272.1 АПК",
        "calculation_note": (
            "15 рабочих дней со дня принятия решения по упрощённому производству."
        ),
        "auto_create": False,
    },
    "apk_cassation": {
        "name": "Кассация в АС (АПК)",
        "applicable_to": ["kad"],
        "applicable_case_types": ["all"],
        "trigger_event": "appeal_ruling_date",
        "calc_type": "calendar_months",
        "calc_value": 2,
        "statute_reference": "АПК РФ, ст. 276 ч. 1",
        "statute_article": "ст. 276 АПК",
        "calculation_note": (
            "2 месяца со дня вступления в силу обжалуемого судебного акта."
        ),
        "auto_create": False,
    },
    "apk_cassation_supreme": {
        "name": "Кассация в ВС РФ (АПК)",
        "applicable_to": ["kad"],
        "applicable_case_types": ["all"],
        "trigger_event": "cassation_ruling_date",
        "calc_type": "calendar_months",
        "calc_value": 2,
        "statute_reference": "АПК РФ, ст. 291.2",
        "statute_article": "ст. 291.2 АПК",
        "calculation_note": (
            "2 месяца со дня вступления в силу обжалуемого акта."
        ),
        "auto_create": False,
    },
    "apk_bankruptcy_appeal": {
        "name": "Апелляция по определению в банкротстве",
        "applicable_to": ["kad"],
        "applicable_case_types": ["bankruptcy"],
        "trigger_event": "decision_date",
        "calc_type": "working_days",
        "calc_value": 10,
        "statute_reference": "АПК РФ, ст. 223 ч. 3; ФЗ о несостоятельности, ст. 61",
        "statute_article": "ст. 61 ФЗ о банкротстве",
        "calculation_note": (
            "10 рабочих дней со дня вынесения определения по банкротному делу."
        ),
        "auto_create": True,
    },
    "apk_bankruptcy_14days": {
        "name": "Апелляция (определение банкротство, не поимённованное)",
        "applicable_to": ["kad"],
        "applicable_case_types": ["bankruptcy"],
        "trigger_event": "decision_date",
        "calc_type": "calendar_days",
        "calc_value": 14,
        "statute_reference": "ФЗ о несостоятельности, ст. 61 п. 3",
        "statute_article": "ст. 61 п. 3 ФЗ",
        "calculation_note": (
            "14 дней для определений, не поимённованных в АПК и ФЗ о несостоятельности."
        ),
        "auto_create": False,
    },

    # ── ГПК – суды общей юрисдикции ──────────────────────────────────────
    "gpk_appeal": {
        "name": "Апелляционная жалоба (ГПК)",
        "applicable_to": ["soy"],
        "applicable_case_types": ["civil"],
        "trigger_event": "decision_date",
        "calc_type": "calendar_months",
        "calc_value": 1,
        "statute_reference": "ГПК РФ, ст. 321 ч. 2",
        "statute_article": "ст. 321 ГПК",
        "calculation_note": (
            "1 месяц со дня принятия решения в окончательной форме (мотивированного решения)."
        ),
        "auto_create": True,
    },
    "gpk_cassation": {
        "name": "Кассация в суд общей юрисдикции (ГПК)",
        "applicable_to": ["soy"],
        "applicable_case_types": ["civil"],
        "trigger_event": "appeal_ruling_date",
        "calc_type": "calendar_months",
        "calc_value": 3,
        "statute_reference": "ГПК РФ, ст. 376.1 ч. 1 (ред. 2025)",
        "statute_article": "ст. 376.1 ГПК",
        "calculation_note": (
            "3 месяца со дня вступления в силу обжалуемого постановления. "
            "Если обжаловалось в апелляции — с даты изготовления мотивированного апелляционного определения."
        ),
        "auto_create": False,
    },
    "gpk_cassation_supreme": {
        "name": "Кассация в ВС РФ (ГПК)",
        "applicable_to": ["soy"],
        "applicable_case_types": ["civil"],
        "trigger_event": "cassation_ruling_date",
        "calc_type": "calendar_months",
        "calc_value": 3,
        "statute_reference": "ГПК РФ, ст. 390.3",
        "statute_article": "ст. 390.3 ГПК",
        "calculation_note": (
            "3 месяца со дня вступления в силу обжалуемого постановления."
        ),
        "auto_create": False,
    },
    "gpk_supervisory": {
        "name": "Надзорная жалоба в Президиум ВС (ГПК)",
        "applicable_to": ["soy"],
        "applicable_case_types": ["civil"],
        "trigger_event": "cassation_ruling_date",
        "calc_type": "calendar_months",
        "calc_value": 3,
        "statute_reference": "ГПК РФ, ст. 391.2",
        "statute_article": "ст. 391.2 ГПК",
        "calculation_note": (
            "3 месяца со дня вступления постановления в законную силу."
        ),
        "auto_create": False,
    },

    # ── КАС – административное судопроизводство ───────────────────────────
    "kas_appeal": {
        "name": "Апелляция (КАС)",
        "applicable_to": ["soy"],
        "applicable_case_types": ["kas"],
        "trigger_event": "decision_date",
        "calc_type": "calendar_months",
        "calc_value": 1,
        "statute_reference": "КАС РФ, ст. 298 ч. 1",
        "statute_article": "ст. 298 КАС",
        "calculation_note": (
            "1 месяц со дня принятия решения суда в окончательной форме."
        ),
        "auto_create": True,
    },
    "kas_cassation": {
        "name": "Кассация (КАС)",
        "applicable_to": ["soy"],
        "applicable_case_types": ["kas"],
        "trigger_event": "appeal_ruling_date",
        "calc_type": "calendar_months",
        "calc_value": 6,
        "statute_reference": "КАС РФ, ст. 318 ч. 2",
        "statute_article": "ст. 318 КАС",
        "calculation_note": (
            "6 месяцев со дня вступления в силу обжалуемых судебных актов."
        ),
        "auto_create": False,
    },

    # ── КоАП ─────────────────────────────────────────────────────────────
    "koap_appeal": {
        "name": "Обжалование постановления (КоАП)",
        "applicable_to": ["all"],
        "applicable_case_types": ["koap"],
        "trigger_event": "document_date",
        "calc_type": "calendar_days",
        "calc_value": 10,
        "statute_reference": "КоАП РФ, ст. 30.3 ч. 1 (ред. 2024)",
        "statute_article": "ст. 30.3 КоАП",
        "calculation_note": (
            "10 дней со дня вручения или получения копии постановления. "
            "Срок исчисляется в календарных днях. "
            "Если последний день — выходной, переносится на первый рабочий."
        ),
        "auto_create": False,
    },
    "koap_appeal_election": {
        "name": "Обжалование (избирательные нарушения, КоАП)",
        "applicable_to": ["all"],
        "applicable_case_types": ["koap"],
        "trigger_event": "document_date",
        "calc_type": "calendar_days",
        "calc_value": 5,
        "statute_reference": "КоАП РФ, ст. 30.3 ч. 3",
        "statute_article": "ст. 30.3 ч. 3 КоАП",
        "calculation_note": (
            "5 дней для нарушений по ст. 5.1–5.25, 5.45–5.52, 5.56, 5.58, 5.69 КоАП РФ."
        ),
        "auto_create": False,
    },
}


# ---------------------------------------------------------------------------
# Date calculation helpers
# ---------------------------------------------------------------------------

def _add_calendar_months(d: date, months: int) -> date:
    """Add N calendar months, clamping to end-of-month when needed."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _add_working_days(d: date, days: int) -> date:
    """Add N working days (Mon–Fri), skipping weekends."""
    current = d
    count = 0
    while count < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return current


def _next_working_day(d: date) -> date:
    """If d falls on a weekend, advance to the next Monday."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def calculate_deadline(trigger_date: date, rule_key: str) -> Optional[date]:
    """Return the deadline date for rule_key triggered on trigger_date."""
    rule = DEADLINE_RULES.get(rule_key)
    if not rule:
        logger.warning("Unknown rule key: %s", rule_key)
        return None

    ct = rule["calc_type"]
    cv = rule["calc_value"]

    if ct == "calendar_months":
        result = _add_calendar_months(trigger_date, cv)
        result = _next_working_day(result)
    elif ct == "calendar_days":
        result = trigger_date + timedelta(days=cv)
        result = _next_working_day(result)
    elif ct == "working_days":
        result = _add_working_days(trigger_date, cv)
    else:
        logger.error("Unknown calc_type: %s", ct)
        return None

    return result


# ---------------------------------------------------------------------------
# Auto-create deadlines after events are saved
# ---------------------------------------------------------------------------

# Map event_type values → trigger_event keys in DEADLINE_RULES
_TRIGGER_MAP = {
    "decision":          "decision_date",
    "решение":           "decision_date",
    "appeal_ruling":     "appeal_ruling_date",
    "апелляция":         "appeal_ruling_date",
    "cassation_ruling":  "cassation_ruling_date",
    "кассация":          "cassation_ruling_date",
}

# Bankruptcy case_type keywords
_BANKRUPTCY_KEYWORDS = {"банкрот", "несостоятельн", "bankruptcy"}


def _is_bankruptcy(case: dict) -> bool:
    ct = (case.get("case_type") or "").lower()
    return any(kw in ct for kw in _BANKRUPTCY_KEYWORDS)


def auto_create_deadlines_for_case(conn, case_id: int) -> int:
    """
    Scan events for this case and create auto-calculated deadlines
    where auto_create=True and no deadline of that type already exists.
    Returns number of deadlines created.
    """
    from court_tracker.db.queries import _rows, _now

    # Load case
    cur = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,))
    case_row = cur.fetchone()
    if not case_row:
        return 0
    cols = [c[0] for c in cur.description]
    case = dict(zip(cols, case_row))
    source = case.get("source", "kad")

    # Load events that could trigger deadlines
    events = _rows(conn.execute(
        "SELECT * FROM events WHERE case_id=? AND is_future=0 ORDER BY event_date",
        (case_id,),
    ))

    # Load existing auto deadlines so we don't duplicate
    existing_types = {
        r[0] for r in conn.execute(
            "SELECT deadline_type FROM deadlines WHERE case_id=? AND is_auto_calculated=1",
            (case_id,),
        ).fetchall()
    }

    created = 0
    now_str = _now()

    for ev in events:
        ev_type = (ev.get("event_type") or "").lower()
        trigger_key = _TRIGGER_MAP.get(ev_type)
        if not trigger_key:
            continue

        ev_date_str = ev.get("event_date")
        if not ev_date_str:
            continue
        try:
            trigger_date = date.fromisoformat(ev_date_str[:10])
        except ValueError:
            continue

        for rule_key, rule in DEADLINE_RULES.items():
            if not rule.get("auto_create"):
                continue
            if rule["trigger_event"] != trigger_key:
                continue

            # Source match
            at = rule["applicable_to"]
            if at != ["all"] and source not in at:
                continue

            # Case type match
            act = rule.get("applicable_case_types", ["all"])
            if act != ["all"] and not _is_bankruptcy(case):
                # For bankruptcy rules skip non-bankruptcy cases
                if any(k in ("bankruptcy",) for k in act):
                    continue

            # Skip if already exists
            dl_type = rule["name"]
            if dl_type in existing_types:
                continue

            dl_date = calculate_deadline(trigger_date, rule_key)
            if not dl_date:
                continue

            conn.execute(
                """INSERT INTO deadlines
                    (case_id, deadline_type, deadline_date, trigger_date,
                     trigger_event, statute_reference, statute_article,
                     calculation_note, is_auto_calculated, created_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?)""",
                (
                    case_id,
                    dl_type,
                    dl_date.isoformat(),
                    ev_date_str[:10],
                    trigger_key,
                    rule["statute_reference"],
                    rule["statute_article"],
                    rule["calculation_note"],
                    now_str,
                ),
            )
            existing_types.add(dl_type)
            created += 1

    if created:
        conn.commit()
    return created


# ---------------------------------------------------------------------------
# Urgency helper (shared with templates)
# ---------------------------------------------------------------------------

def urgency_level(deadline_date_str: Optional[str]) -> str:
    """Return 'crit' | 'soon' | 'warn' | 'safe'."""
    if not deadline_date_str:
        return "safe"
    try:
        d = date.fromisoformat(deadline_date_str[:10])
    except ValueError:
        return "safe"
    days = (d - date.today()).days
    if days <= 1:
        return "crit"
    if days <= 3:
        return "soon"
    if days <= 7:
        return "warn"
    return "safe"
