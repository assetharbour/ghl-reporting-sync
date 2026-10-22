"""The Leads sheet gained fact-find columns on 22 Oct 2026. Columns may only ever
be appended, and the header row must follow automatically."""

from app import join
from app.field_mapping import CUSTOM_FIELD_IDS
from app.sheets_client import SheetsClient

OLD_LAST_COLUMN = "total_revenue"
NEW_COLUMNS = ["fact_find_outcome", "fact_find_date", "docs_requested"]


def test_new_columns_are_appended_after_the_old_last_column():
    cols = join.COLUMNS
    assert cols.index(OLD_LAST_COLUMN) == 61                       # the 62 original columns did not move
    assert cols[62:] == NEW_COLUMNS and len(cols) == 65
    assert len(set(cols)) == len(cols)


def test_row_reads_the_fact_find_fields_from_the_contact():
    contact = {"id": "c1", "firstName": "A", "lastName": "B", "customFields": [
        {"id": CUSTOM_FIELD_IDS["fact_find_outcome"], "value": "Full Fact Find Took Place - YES"},
        {"id": CUSTOM_FIELD_IDS["fact_find_date"], "value": "2026-10-20T00:00:00.000Z"},
        {"id": CUSTOM_FIELD_IDS["docs_requested"], "value": "Yes"},
    ]}
    row = join.build_row({"id": "o1", "contactId": "c1", "createdAt": "2026-10-01T00:00:00Z"}, contact)
    assert row["fact_find_outcome"] == "Full Fact Find Took Place - YES" and row["docs_requested"] == "Yes"
    assert row["fact_find_date"].startswith("2026-10-20")
    blank = join.build_row({"id": "o2", "contactId": "c2"}, {"id": "c2", "customFields": []})
    assert (blank["fact_find_outcome"], blank["fact_find_date"], blank["docs_requested"]) == ("", "", "")


class RecordingWorksheet:
    def __init__(self, rows):
        self.rows, self.updates, self.batches, self.appended = rows, [], [], []

    def get_all_values(self):
        return self.rows

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def batch_update(self, updates):
        self.batches.append(updates)

    def append_rows(self, rows, **kwargs):
        self.appended.append(rows)


def client_with(ws):
    client = object.__new__(SheetsClient)
    client._get_or_create_ws = lambda title, cols: ws
    return client


def old_sheet():
    header = join.COLUMNS[:62]
    row = ["c1", "o1"] + [""] * 60
    return header, row


def test_the_header_row_is_extended_when_columns_were_appended():
    header, row = old_sheet()
    ws = RecordingWorksheet([header, row])
    new_row = {c: "" for c in join.COLUMNS}
    new_row.update(contact_id="c1", opportunity_id="o1", fact_find_outcome="Review - YES")
    client_with(ws).upsert_leads([new_row])
    assert ws.updates == [{"values": [join.COLUMNS], "range_name": "A1"}]              # header extended, nothing moved
    update = ws.batches[0][0]
    assert update["range"] == "A2:BM2" and update["values"][0][-3] == "Review - YES"


def test_a_header_that_is_not_a_prefix_is_left_alone():
    header = ["something", "else"]
    ws = RecordingWorksheet([header, ["c1", "o1"]])
    new_row = {c: "" for c in join.COLUMNS}
    new_row.update(contact_id="c1", opportunity_id="o1")
    client_with(ws).upsert_leads([new_row])
    assert ws.updates == []


def test_an_up_to_date_header_is_not_rewritten():
    ws = RecordingWorksheet([list(join.COLUMNS), ["c1", "o1"] + [""] * 63])
    new_row = {c: "" for c in join.COLUMNS}
    new_row.update(contact_id="c1", opportunity_id="o1")
    client_with(ws).upsert_leads([new_row])
    assert ws.updates == []


def test_the_stage_names_match_the_live_ghl_pipeline():
    """Checked against the live pipelines API on 22 Oct 2026: the stage was renamed and a new stage was added."""
    from app.field_mapping import PIPELINE
    stages = PIPELINE["stages"]
    assert stages["68272d52-f2b8-47ce-b657-718b465c062a"] == "Client Contacted"
    assert stages["629e80ae-4f0f-4334-91ae-c677d3c11648"] == "Post Appointment"
    assert "Admin Contacted" not in stages.values()
    row = join.build_row({"id": "o1", "contactId": "c1", "pipelineStageId": "629e80ae-4f0f-4334-91ae-c677d3c11648", "createdAt": "2026-10-01T00:00:00Z"},
                         {"id": "c1", "customFields": []})
    assert row["pipeline_stage"] == "Post Appointment"            # it used to be blank
