"""Join GHL opportunities with contacts into flat rows for the Leads sheet."""

import logging
from datetime import datetime, timezone

from app.field_mapping import PIPELINE, USERS, get_cf

logger = logging.getLogger(__name__)

# Canonical column order — this is the Sheet header row. sheets_client writes
# positionally from this list, so order changes here change the sheet layout.
COLUMNS = [
    "contact_id", "opportunity_id", "client_name", "pipeline_stage",
    "case_status", "opportunity_owner", "mortgage_case_type",
    "lead_source", "negotiator", "admin_handover", "is_estate_agent_lead",
    "admin_call_status", "admin_call_attempt_count",
    "advisor_call_attempt_count", "advisor_recommendation_attempt_count",
    "docs_chase_attempt_count", "client_readiness_status",
    "docs_status", "advisor_recommendation_status", "property_status",
    "solicitor_status", "application_submission_status",
    "lender_docs_status", "valuation_result", "valuation_impact_status",
    "offer_status", "exchange_status", "exchange_date",
    "review_permission_status", "review_status",
    "solicitor_offer_confirmation", "protection_status", "protection_type",
    "protection_provider", "protection_policy_number", "protection_premium",
    "protection_cover_amount", "protection_start_date",
    "erc_date", "mortgage_product_roll_off_date", "completion_date",
    "completion_status", "first_contact_time", "docs_received_time",
    "application_time", "offer_time", "completion_time",
    "avg_days_to_completion", "has_protection",
    "client_relationship_status", "remortgage_campaign_status",
    "remortgage_engagement_status", "pause_automations",
    "created_date", "last_synced",
    # Appended (not inserted) so existing column positions never shift:
    # "opportunity_owner" above is kept as-is (opportunity.assignedTo) for
    # audit/backward-compat only — it is NOT the case admin. "admin" here
    # is the correct, contact-level owner (contact.assignedTo) that
    # reflects Anita Andrews pre-handover / one of the 3 handover admins
    # post-handover. advisor_name/advisor_call_status are separate
    # dropdown custom fields, never a GHL assignedTo user ID.
    "admin", "advisor_name", "advisor_call_status",
    # Revenue = broker_fee + expected_procuration_fee + solicitor_commission.
    # total_revenue is blank (not 0) when none of the three are set, so a
    # confirmed-zero case is distinguishable from a not-yet-recorded one.
    "broker_fee", "expected_procuration_fee", "solicitor_commission", "total_revenue",
]

DATE_FIELDS = {
    "exchange_date", "protection_start_date", "erc_date",
    "mortgage_product_roll_off_date", "completion_date",
    "first_contact_time", "docs_received_time", "application_time",
    "offer_time", "completion_time",
}


def _collect_custom_fields(opp: dict, contact: dict) -> list:
    """Merge opportunity + contact custom field arrays, normalising each
    entry to the {id, value} shape expected by get_cf. GHL uses 'value' on
    contacts but 'fieldValue' on some opportunity payloads."""
    merged = []
    for field in (opp.get("customFields") or []) + (contact.get("customFields") or []):
        if "value" not in field and "fieldValue" in field:
            field = {**field, "value": field["fieldValue"]}
        merged.append(field)
    return merged


def _to_ms(value):
    """Coerce a GHL timestamp (ms epoch int, numeric string, or ISO string)
    to milliseconds since epoch. Returns None if not parseable."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _to_date_str(value) -> str:
    """Convert a GHL timestamp to YYYY-MM-DD, or "" if null/unparseable."""
    ms = _to_ms(value)
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def parse_number(value):
    """Coerce a GHL MONETORY field value to a float. Handles plain numbers
    as well as currency-formatted strings (£, commas, whitespace) as a
    defensive fallback. Returns None on empty/unparseable input rather
    than crashing or silently defaulting to 0."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("£", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def build_row(opp: dict, contact: dict) -> dict:
    """Join one opportunity with its contact into one flat Leads row."""
    cfs = _collect_custom_fields(opp, contact)

    client_name = " ".join(
        part for part in [contact.get("firstName"), contact.get("lastName")] if part
    ) or contact.get("contactName") or opp.get("name") or ""

    row = {
        "contact_id": opp.get("contactId") or contact.get("id") or "",
        "opportunity_id": opp.get("id") or "",
        "client_name": client_name,
        "pipeline_stage": PIPELINE["stages"].get(opp.get("pipelineStageId"), ""),
        "case_status": opp.get("status") or "",
        # Deprecated: opportunity-level assignedTo. Kept only for audit —
        # this is NOT the case admin. Use "admin" below instead.
        "opportunity_owner": USERS.get(opp.get("assignedTo"), ""),
        # The actual current case admin: Anita Andrews pre-handover, or
        # whichever of the 3 handover admins the automation reassigned to.
        # Sourced from the CONTACT record, not the opportunity.
        "admin": USERS.get(contact.get("assignedTo"), ""),
    }

    for col in COLUMNS:
        if col in row or col in (
            "avg_days_to_completion", "has_protection", "created_date", "last_synced",
            "broker_fee", "expected_procuration_fee", "solicitor_commission", "total_revenue",
        ):
            continue
        value = get_cf(cfs, col)
        if col in DATE_FIELDS:
            row[col] = _to_date_str(value)
        else:
            row[col] = "" if value is None else value

    first_ms = _to_ms(get_cf(cfs, "first_contact_time"))
    completion_ms = _to_ms(get_cf(cfs, "completion_time"))
    if first_ms is not None and completion_ms is not None:
        row["avg_days_to_completion"] = round((completion_ms - first_ms) / 86400000, 1)
    else:
        row["avg_days_to_completion"] = ""

    protection_type = get_cf(cfs, "protection_type")
    row["has_protection"] = bool(protection_type not in (None, "", []))

    broker_fee = parse_number(get_cf(cfs, "broker_fee"))
    procuration_fee = parse_number(get_cf(cfs, "expected_procuration_fee"))
    solicitor_commission = parse_number(get_cf(cfs, "solicitor_commission"))
    row["broker_fee"] = "" if broker_fee is None else broker_fee
    row["expected_procuration_fee"] = "" if procuration_fee is None else procuration_fee
    row["solicitor_commission"] = "" if solicitor_commission is None else solicitor_commission

    fee_values = [v for v in (broker_fee, procuration_fee, solicitor_commission) if v is not None]
    # Blank (not 0) when none of the three are set, so a confirmed-zero
    # case stays distinguishable from a not-yet-recorded one.
    row["total_revenue"] = round(sum(fee_values), 2) if fee_values else ""

    row["created_date"] = _to_date_str(opp.get("createdAt"))
    row["last_synced"] = datetime.now(timezone.utc).isoformat()

    return row


def build_all_rows(opportunities: list, contact_map: dict) -> list:
    """Build a row per opportunity, skipping any whose contact is missing."""
    rows = []
    for opp in opportunities:
        contact = contact_map.get(opp.get("contactId"))
        if contact is None:
            logger.warning(
                "Skipping opportunity %s — contact %s not found",
                opp.get("id"), opp.get("contactId"),
            )
            continue
        rows.append(build_row(opp, contact))
    return rows
