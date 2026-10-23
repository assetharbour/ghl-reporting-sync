"""The case admin on the Leads sheet comes from the LIVE GHL user list, so new staff are named (Ava and Hugo showed a blank Admin before)."""

import asyncio

from api import sync as sync_api
from app import incremental, join
from app.field_mapping import USERS
from app.users import AGENCY_USER_NAMES, current_users, resolve_users

AVA, HUGO = "uAvaNotInTheOldList", "uHugoNotInTheOldList"
ANITA = "djtLhz7zhOJS2g4emnZo"          # in the static map
LIVE = {AVA: "Ava Morris-Boundy", HUGO: "Hugo Sharpe", ANITA: "Anita Andrews", "uAgency1": "Hammad Sabir", "uAgency2": "Grow mysme", "uBlank": "  ", "": "Nobody"}


def run(coro):
    return asyncio.run(coro)


def test_live_users_are_added_and_the_static_map_still_covers_people_who_left():
    users = resolve_users(LIVE)
    assert users[AVA] == "Ava Morris-Boundy" and users[HUGO] == "Hugo Sharpe" and users[ANITA] == "Anita Andrews"
    assert all(users[uid] == name for uid, name in USERS.items() if uid not in LIVE)           # a former colleague is still named on old cases


def test_a_live_name_wins_over_the_static_one_for_the_same_id():
    assert resolve_users({ANITA: "Anita Andrews-Smith"})[ANITA] == "Anita Andrews-Smith"


def test_agency_logins_blank_names_and_empty_ids_are_never_added():
    users = resolve_users(LIVE)
    assert "uAgency1" not in users and "uAgency2" not in users and "uBlank" not in users and "" not in users
    assert {"hammad sabir", "grow mysme"} <= AGENCY_USER_NAMES
    assert resolve_users({"u9": "GROW MYSME", "u8": " hammad sabir "}) == dict(USERS)         # matched case-insensitively and trimmed


def test_without_a_live_list_it_is_exactly_the_static_map():
    assert resolve_users(None) == dict(USERS) == resolve_users({})


class GHL:
    def __init__(self, users=None, boom=None):
        self.users, self.boom = users, boom

    async def get_users(self):
        if self.boom:
            raise self.boom
        return self.users


def test_current_users_never_raises():
    assert run(current_users(GHL(LIVE)))[AVA] == "Ava Morris-Boundy"
    assert run(current_users(GHL(None))) == dict(USERS)                                        # GHL returned nothing
    assert run(current_users(GHL(boom=RuntimeError("down")))) == dict(USERS)
    assert run(current_users(object())) == dict(USERS)                                          # a client without the method


def opportunity(owner):
    return {"id": "o1", "contactId": "c1", "name": "Maria", "pipelineStageId": next(iter(join.PIPELINE["stages"])), "status": "open",
            "assignedTo": owner, "createdAt": "2026-10-16T13:59:03.891Z", "customFields": []}


def test_the_row_names_the_contact_owner_as_admin_and_the_opportunity_owner_from_the_live_list():
    contact = {"id": "c1", "firstName": "Maria", "lastName": "Mukhtar", "assignedTo": AVA, "customFields": []}
    users = resolve_users(LIVE)
    row = join.build_row(opportunity(AVA), contact, users)
    assert (row["admin"], row["opportunity_owner"]) == ("Ava Morris-Boundy", "Ava Morris-Boundy")
    old = join.build_row(opportunity(AVA), contact)                                              # no user map given: the old behaviour, blank
    assert (old["admin"], old["opportunity_owner"]) == ("", "")
    assert join.build_all_rows([opportunity(AVA)], {"c1": contact}, users)[0]["admin"] == "Ava Morris-Boundy"
    assert join.build_row(opportunity(None), {**contact, "assignedTo": None}, users)["admin"] == ""    # genuinely unassigned stays blank


def test_a_changed_owner_triggers_an_incremental_rebuild_with_the_live_names():
    opp = opportunity(AVA)
    existing = {"opportunity_owner": "", "pipeline_stage": join.PIPELINE["stages"][opp["pipelineStageId"]], "case_status": "open", "created_date": join._to_date_str(opp["createdAt"])}
    since = incremental.parse_ts("2026-10-23T00:00:00Z")
    assert not incremental.opportunity_changed(opp, existing, since)                             # the old map cannot see this owner at all: nothing looks changed
    assert not incremental.opportunity_changed(opp, {**existing, "opportunity_owner": "Ava Morris-Boundy"}, since, resolve_users(LIVE))
    assert incremental.opportunity_changed(opp, existing, since, resolve_users(LIVE))           # the sheet still has a blank owner: rebuild it


def test_the_full_sync_writes_the_admin_of_new_staff(monkeypatch):
    contact = {"id": "c1", "firstName": "Maria", "lastName": "Mukhtar", "assignedTo": AVA, "customFields": []}
    written = []

    class Sheets:
        def upsert_leads(self, rows, existing=None):
            written.extend(rows)
            return {"updated": 0, "inserted": len(rows), "total": len(rows)}

        def remove_stale_rows(self, ids, values=None):
            return {"removed": 0}

    class FullGHL:
        last_fetch_complete = True

        async def get_all_opportunities(self):
            return [opportunity(AVA)]

        async def get_all_contacts(self, ids):
            return {"c1": contact}

        async def get_users(self):
            return LIVE

    run(sync_api._full_cycle(FullGHL(), Sheets()))
    assert [r["admin"] for r in written] == ["Ava Morris-Boundy"]


def test_the_incremental_sync_also_names_new_staff_and_heals_a_blank_owner():
    from tests.test_incremental import FakeGHL as IncGHL, FakeSheets, NOW, contact as inc_contact, opp as inc_opp, sheet_values
    first = inc_opp("o1", "c1")
    second = inc_opp("o2", "c2")
    stored_pairs = [(first, inc_contact("c1")), (second, inc_contact("c2"))]
    values = sheet_values(stored_pairs)                                   # the sheet was written when Ava's id was unknown: blank owners
    owned = {**second, "assignedTo": AVA}                                  # the opportunity is now (or always was) owned by Ava
    ava_contact = {**inc_contact("c2"), "assignedTo": AVA}
    ghl = IncGHL([first, owned], changed=[ava_contact])

    async def get_users():
        return LIVE

    ghl.get_users = get_users
    sheets = FakeSheets(values)
    run(incremental.run(ghl, sheets, NOW))
    rows = sheets.upserts[0][0]
    assert [(r["opportunity_id"], r["admin"], r["opportunity_owner"]) for r in rows] == [("o2", "Ava Morris-Boundy", "Ava Morris-Boundy")]
