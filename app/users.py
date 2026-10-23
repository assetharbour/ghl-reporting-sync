"""Who a GHL user id is, for the Leads sheet's Admin and Opportunity Owner columns.

The static USERS map in field_mapping went stale: people added to GHL later (Ava Morris-Boundy, Hugo Sharpe ...) were not in it, so every lead
they owned showed a blank Admin on the reports. The live user list from GHL is now the source; the static map only fills in people who are no
longer in GHL (so old cases still show their former owner) and is the whole answer if GHL cannot be reached.

GrowMySME's own agency logins are deliberately never shown as a case admin (they were never in the static map, on purpose), so they are
excluded here by name.
"""

import logging

from app.field_mapping import USERS

logger = logging.getLogger(__name__)

AGENCY_USER_NAMES = frozenset({"grow mysme", "hammad sabir", "m talha zafar", "miz zafar", "abdul abdul", "ben p."})


def resolve_users(live) -> dict:
    """{user id: name}: the static map, overridden by every live GHL user who is not an agency login. `live` None/empty = static only."""
    users = dict(USERS)
    for uid, name in (live or {}).items():
        name = str(name or "").strip()
        if uid and name and name.lower() not in AGENCY_USER_NAMES:
            users[uid] = name
    return users


async def current_users(ghl) -> dict:
    """The user map for this sync. Never raises: any failure falls back to the static map, as before."""
    try:
        live = await ghl.get_users()
    except Exception as exc:
        logger.warning("Live GHL user list unavailable (%s); using the static map", type(exc).__name__)
        live = None
    if not live:
        logger.warning("Live GHL user list empty or unavailable; using the static map")
    return resolve_users(live)
