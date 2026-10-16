import hashlib
import hmac
import json
import logging
from datetime import datetime

import gspread
import pytest
from fastapi.testclient import TestClient

from api import evonex as evonex_api
from app import config, evonex

ORIGINAL_APPEND = evonex._append_to_sheet  # captured before the autouse fixture stubs it

TOKEN = "dummy-evonex-token"
BASE = "https://assetharbour-reports.vercel.app"

# Fictional samples from Evonex Webhooks Developer Guide v1.8, Appendix C.
RECORDINGREADY = '{"ts":"2026-04-20T12:00:00Z","event":"RECORDINGREADY","subevent":null,"cid":"call-a1b2-c3d4","lpi":"1001","rpi":"+442075551234","rpn":"Customer Support","opi":null,"context":"Duration:245;FileSize:1024000;StartTime:2026-04-20T11:55:00;Url:https%3A%2F%2Fcontent.evonex.com%2Fcontent%2Frecording%2F67890","sub":"1001","mac":null,"pn":null,"fn":null,"ua":null,"on":"Jane Smith","un":"jsmith","oa":"Acme Corp","od":"Sales","oj":"Account Manager","or":"User","os":null,"oe":"jsmith@acme.com","om":"+447700900123","sbn":"Evonex","lsn":"UK-DC1"}'
VOICEMAIL = '{"ts":"2026-04-20T10:45:00Z","event":"VOICEMAIL","subevent":"NEWVM","cid":"call-c4e2-7d8b","lpi":"1001","rpi":"+442075559876","rpn":"Client A","opi":null,"context":"Duration:18;LeftAt:2026-04-20T10:44:30;Url:https%3A%2F%2Fcontent.evonex.com%2Fcontent%2Fvm%2F12345","sub":"1001","mac":null,"pn":null,"fn":null,"ua":null,"on":"Jane Smith","un":"jsmith","oa":"Acme Corp","od":"Sales","oj":"Account Manager","or":"User","os":null,"oe":"jsmith@acme.com","om":"+447700900123","sbn":"Evonex","lsn":"UK-DC1"}'
MISSEDCALL = '{"ts":"2026-04-20T09:22:30Z","event":"MISSEDCALL","subevent":null,"cid":"call-8f3a-4b2c","lpi":"1001","rpi":"+442075551234","rpn":"Customer Support","opi":null,"context":null,"sub":"1001","mac":null,"pn":null,"fn":null,"ua":null,"on":"Jane Smith","un":"jsmith","oa":"Acme Corp","od":"Sales","oj":"Account Manager","or":"User","os":null,"oe":"jsmith@acme.com","om":"+447700900123","sbn":"Evonex","lsn":"UK-DC1"}'


def outbound(subevent):
    """No OUTBOUNDCALL sample exists in the guide: built from the field list."""
    return json.dumps(
        {
            "ts": "2026-04-20T13:00:00Z", "event": "OUTBOUNDCALL", "subevent": subevent,
            "cid": "call-out-0001", "lpi": "1001", "rpi": "+442075550000", "rpn": None,
            "opi": None, "context": None, "sub": "1001", "mac": None, "pn": None,
            "fn": None, "ua": None, "on": "Jane Smith", "un": "jsmith", "oa": "Acme Corp",
            "od": "Sales", "oj": "Account Manager", "or": "User", "os": None,
            "oe": "jsmith@acme.com", "om": "+447700900123", "sbn": "Evonex", "lsn": "UK-DC1",
        }
    )


def sign(body, url, token=TOKEN):
    """Independent re-implementation of the guide's formula."""
    msg = (url + body).encode("utf-8")
    return "sha256=" + hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def state(monkeypatch):
    evonex._worksheet_cache.clear()
    monkeypatch.setattr(config, "EVONEX_WEBHOOK_TOKENS", [TOKEN])
    monkeypatch.setattr(config, "EVONEX_SHEET_ID", "dummy-evonex-sheet")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", BASE)
    s = type("S", (), {})()
    s.rows, s.sleeps = [], []
    monkeypatch.setattr(evonex, "_append_to_sheet", lambda values: s.rows.append(values))
    monkeypatch.setattr(evonex.time, "sleep", lambda secs: s.sleeps.append(secs))
    return s


@pytest.fixture
def client():
    return TestClient(evonex_api.app, base_url=BASE, raise_server_exceptions=False)


def send(client, path, body, token=TOKEN, signed=True, base=BASE, headers=None, signed_body=None):
    h = {"Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = token
    if signed:
        h["X-Hub-Signature-256"] = sign(signed_body if signed_body is not None else body, base + path)
    h.update(headers or {})
    return client.post(path, content=body.encode("utf-8"), headers=h)


def as_dict(row):
    assert len(row) == len(evonex.COLUMNS)
    return dict(zip(evonex.COLUMNS, row))


# ---------------------------------------------------------------- happy paths

def test_recordingready_row_is_exact(client, state):
    r = send(client, "/api/evonex/recordingready", RECORDINGREADY)
    assert r.status_code == 200 and r.json() == {}
    row = as_dict(state.rows[0])
    assert row["path"] == "recordingready"
    assert (row["event"], row["subevent"]) == ("RECORDINGREADY", "")
    assert (row["ts"], row["cid"]) == ("2026-04-20T12:00:00Z", "call-a1b2-c3d4")
    assert (row["lpi"], row["rpi"], row["rpn"], row["opi"], row["sub"]) == ("1001", "+442075551234", "Customer Support", "", "1001")
    assert row["context"].startswith("Duration:245;FileSize:1024000;StartTime:2026-04-20T11:55:00;Url:https%3A")
    assert row["ctx_duration"] == "245"
    assert row["ctx_file_size"] == "1024000"
    assert row["ctx_start_time"] == "2026-04-20T11:55:00"  # colons survive the first-colon split
    assert row["ctx_left_at"] == ""
    assert row["ctx_url"] == "https://content.evonex.com/content/recording/67890"  # URL-decoded
    assert (row["on"], row["un"], row["oa"], row["od"], row["oj"]) == ("Jane Smith", "jsmith", "Acme Corp", "Sales", "Account Manager")
    assert (row["or"], row["os"], row["oe"], row["om"]) == ("User", "", "jsmith@acme.com", "+447700900123")
    assert (row["oref"], row["sbn"], row["lsn"]) == ("", "Evonex", "UK-DC1")
    assert (row["mac"], row["pn"], row["fn"], row["ua"]) == ("", "", "", "")
    assert row["extra_fields"] == ""
    assert row["signature_url_variant"] == "a"
    assert row["raw_json"] == RECORDINGREADY
    assert len(row["dedup_key"]) == 64
    datetime.fromisoformat(row["received_at_utc"])


def test_voicemail_context_parsed(client, state):
    assert send(client, "/api/evonex/voicemail", VOICEMAIL).status_code == 200
    row = as_dict(state.rows[0])
    assert row["subevent"] == "NEWVM"
    assert row["ctx_duration"] == "18"
    assert row["ctx_left_at"] == "2026-04-20T10:44:30"
    assert row["ctx_url"] == "https://content.evonex.com/content/vm/12345"
    assert (row["ctx_file_size"], row["ctx_start_time"]) == ("", "")


def test_missedcall_null_context(client, state):
    assert send(client, "/api/evonex/missedcall", MISSEDCALL).status_code == 200
    row = as_dict(state.rows[0])
    assert row["event"] == "MISSEDCALL" and row["context"] == ""
    assert [row[c] for c in ("ctx_duration", "ctx_file_size", "ctx_start_time", "ctx_left_at", "ctx_url")] == [""] * 5


@pytest.mark.parametrize("subevent", ["RINGCALL", "STARTCALL", "ENDCALL", "CANCELCALL"])
def test_outbound_call_subevents(client, state, subevent):
    assert send(client, "/api/evonex/call", outbound(subevent)).status_code == 200
    row = as_dict(state.rows[0])
    assert (row["path"], row["event"], row["subevent"]) == ("call", "OUTBOUNDCALL", subevent)
    assert (row["rpn"], row["context"], row["extra_fields"]) == ("", "", "")


def test_context_not_key_value_is_kept_raw_only(client, state):
    body = json.loads(outbound(None))
    body.update({"event": "REGISTER", "context": "3600"})
    assert send(client, "/api/evonex/register", json.dumps(body)).status_code == 200
    row = as_dict(state.rows[0])
    assert row["context"] == "3600"
    assert [row[c] for c in ("ctx_duration", "ctx_file_size", "ctx_start_time", "ctx_left_at", "ctx_url")] == [""] * 5


def test_context_keys_case_insensitive_and_addrbook_name_ignored():
    assert evonex.parse_context("duration:5;URL:https%3A%2F%2Fx.test%2Fa") == {
        "ctx_duration": "5", "ctx_url": "https://x.test/a"}
    assert evonex.parse_context("ADD;Name:xxx") == {}
    assert evonex.parse_context(None) == {} and evonex.parse_context(123) == {}


def test_bearer_prefix_accepted(client, state):
    assert send(client, "/api/evonex/call", outbound("STARTCALL"), token="Bearer " + TOKEN).status_code == 200
    assert send(client, "/api/evonex/call", outbound("STARTCALL"), token="bearer  " + TOKEN).status_code == 200
    assert len(state.rows) == 2


def test_oref_filled_and_not_in_extra(client, state):
    body = json.loads(outbound("STARTCALL"))
    body["oref"] = "REF-9"
    assert send(client, "/api/evonex/call", json.dumps(body)).status_code == 200
    row = as_dict(state.rows[0])
    assert row["oref"] == "REF-9" and row["extra_fields"] == ""


def test_unknown_key_goes_to_extra_fields(client, state):
    body = json.loads(outbound("STARTCALL"))
    body.update({"newfield": "x", "nested": {"a": 1}})
    assert send(client, "/api/evonex/call", json.dumps(body)).status_code == 200
    assert json.loads(as_dict(state.rows[0])["extra_fields"]) == {"newfield": "x", "nested": {"a": 1}}


def test_unknown_path_still_stored_and_200(client, state):
    assert send(client, "/api/evonex/somethingnew/deeper", outbound("STARTCALL")).status_code == 200
    assert as_dict(state.rows[0])["path"] == "somethingnew/deeper"


def test_query_string_is_part_of_signed_url(client, state):
    h = {"X-Hub-Signature-256": sign(outbound("STARTCALL"), BASE + "/api/evonex/call?a=1&b=2")}
    r = client.post("/api/evonex/call?a=1&b=2", content=outbound("STARTCALL").encode(),
                    headers={"Authorization": TOKEN, "Content-Type": "application/json", **h})
    assert r.status_code == 200 and as_dict(state.rows[0])["signature_url_variant"] == "a"


def test_variant_b_when_signed_url_is_the_request_url(state):
    other = "https://preview-abc.vercel.app"
    c = TestClient(evonex_api.app, base_url=other, raise_server_exceptions=False)
    assert send(c, "/api/evonex/call", outbound("STARTCALL"), base=other).status_code == 200
    assert as_dict(state.rows[0])["signature_url_variant"] == "b"


def test_second_allowed_token_works(client, state, monkeypatch):
    monkeypatch.setattr(config, "EVONEX_WEBHOOK_TOKENS", ["old-token", "new-token"])
    assert send(client, "/api/evonex/call", outbound("STARTCALL"), token="new-token").status_code == 403  # signed with TOKEN
    body, url = outbound("STARTCALL"), BASE + "/api/evonex/call"
    for tok in ("old-token", "new-token"):
        r = client.post("/api/evonex/call", content=body.encode(),
                        headers={"Authorization": tok, "X-Hub-Signature-256": sign(body, url, tok)})
        assert r.status_code == 200


def test_health_needs_no_auth(client):
    r = client.get("/api/evonex/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ------------------------------------------------------------------- rejection

def test_missing_authorization_is_401(client, state):
    assert send(client, "/api/evonex/call", outbound("STARTCALL"), token=None).status_code == 401
    assert state.rows == []


def test_wrong_token_is_403(client, state):
    # Correctly signed WITH the wrong token, so only the token check can reject it.
    body, url = outbound("STARTCALL"), BASE + "/api/evonex/call"
    r = client.post("/api/evonex/call", content=body.encode(),
                    headers={"Authorization": "wrong", "X-Hub-Signature-256": sign(body, url, "wrong")})
    assert r.status_code == 403
    assert send(client, "/api/evonex/call", outbound("STARTCALL"), token="").status_code in (401, 403)
    assert state.rows == []


def test_no_configured_tokens_fails_closed(client, state, monkeypatch):
    monkeypatch.setattr(config, "EVONEX_WEBHOOK_TOKENS", [])
    assert send(client, "/api/evonex/call", outbound("STARTCALL")).status_code == 403
    assert state.rows == []


def test_missing_signature_is_403(client, state):
    assert send(client, "/api/evonex/call", outbound("STARTCALL"), signed=False).status_code == 403
    assert state.rows == []


def test_signature_without_prefix_is_403(client, state):
    bare = sign(outbound("STARTCALL"), BASE + "/api/evonex/call").removeprefix("sha256=")
    r = send(client, "/api/evonex/call", outbound("STARTCALL"), signed=False,
             headers={"X-Hub-Signature-256": bare})
    assert r.status_code == 403


def test_wrong_signature_is_403(client, state):
    r = send(client, "/api/evonex/call", outbound("STARTCALL"), signed=False,
             headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert r.status_code == 403 and state.rows == []


def test_signature_for_other_url_is_403(client, state):
    h = {"X-Hub-Signature-256": sign(outbound("STARTCALL"), BASE + "/api/evonex/voicemail")}
    r = send(client, "/api/evonex/call", outbound("STARTCALL"), signed=False, headers=h)
    assert r.status_code == 403


def test_body_tampered_after_signing_is_403(client, state):
    r = send(client, "/api/evonex/call", outbound("ENDCALL"), signed_body=outbound("STARTCALL"))
    assert r.status_code == 403 and state.rows == []


@pytest.mark.parametrize("body", ["{not json", "", "[1,2]", '"text"', "123", "null"])
def test_invalid_json_or_non_object_is_400(client, state, body):
    r = send(client, "/api/evonex/call", body)
    assert r.status_code == 400 and state.rows == []
    assert "Traceback" not in r.text


# ---------------------------------------------------------------------- storage

def test_sheet_failure_after_retries_is_500(client, state, monkeypatch):
    calls = []

    def boom(values):
        calls.append(1)
        raise RuntimeError("sheets down")

    monkeypatch.setattr(evonex, "_append_to_sheet", boom)
    r = send(client, "/api/evonex/call", outbound("STARTCALL"))
    assert r.status_code == 500 and "Traceback" not in r.text and "sheets down" not in r.text
    assert len(calls) == 4  # 1 attempt + 3 retries
    assert state.sleeps == [0.5, 1.0, 2.0]


def test_transient_failure_then_success_is_200(client, state, monkeypatch):
    n = {"calls": 0}

    def flaky(values):
        n["calls"] += 1
        if n["calls"] < 3:
            raise RuntimeError("blip")
        state.rows.append(values)

    monkeypatch.setattr(evonex, "_append_to_sheet", flaky)
    assert send(client, "/api/evonex/call", outbound("STARTCALL")).status_code == 200
    assert n["calls"] == 3 and len(state.rows) == 1


def test_429_retry_after_is_honoured(state, monkeypatch):
    class Resp:
        status_code = 429
        headers = {"Retry-After": "3"}

    class Err(Exception):
        response = Resp()

    n = {"calls": 0}

    def limited(values):
        n["calls"] += 1
        if n["calls"] == 1:
            raise Err()

    monkeypatch.setattr(evonex, "_append_to_sheet", limited)
    assert evonex.write_row_with_retry(["x"]) is True
    assert state.sleeps == [3.0]  # larger than the 0.5 s default backoff


def test_sheet_not_configured_is_500(client, state, monkeypatch):
    monkeypatch.setattr(config, "EVONEX_SHEET_ID", "")
    assert send(client, "/api/evonex/call", outbound("STARTCALL")).status_code == 500
    assert state.rows == []


def test_redelivery_same_dedup_key_but_both_rows_appended(client, state):
    send(client, "/api/evonex/call", outbound("STARTCALL"))
    send(client, "/api/evonex/call", outbound("STARTCALL"))
    a, b = (as_dict(r) for r in state.rows)
    assert len(state.rows) == 2 and a["dedup_key"] == b["dedup_key"]


def test_dedup_key_differs_by_ts_subevent_and_path():
    p = json.loads(outbound("STARTCALL"))
    base = evonex.make_dedup_key("call", p)
    assert base == hashlib.sha256("call-out-0001|OUTBOUNDCALL|STARTCALL|2026-04-20T13:00:00Z|jsmith|call".encode()).hexdigest()
    assert evonex.make_dedup_key("call", {**p, "ts": "2026-04-20T13:00:01Z"}) != base
    assert evonex.make_dedup_key("call", {**p, "subevent": "ENDCALL"}) != base
    assert evonex.make_dedup_key("other", p) != base
    assert evonex.make_dedup_key("call", {}) == hashlib.sha256(b"|||||call").hexdigest()


def test_oversized_raw_json_truncated_and_flagged(client, state):
    body = json.loads(outbound("STARTCALL"))
    body["pad"] = "A" * 60000
    assert send(client, "/api/evonex/call", json.dumps(body)).status_code == 200
    row = as_dict(state.rows[0])
    assert len(row["raw_json"]) == 45000
    assert row["extra_fields"].startswith('{"raw_json_truncated":true')
    assert all(len(c) <= 50000 for c in state.rows[0])


def test_logs_contain_no_personal_data_or_secrets(client, state, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("evonex"), "propagate", True)
    caplog.set_level(logging.INFO, logger="evonex")
    sig = sign(RECORDINGREADY, BASE + "/api/evonex/recordingready")
    client.post("/api/evonex/recordingready", content=RECORDINGREADY.encode(),
                headers={"Authorization": TOKEN, "X-Hub-Signature-256": sig})
    client.post("/api/evonex/recordingready", content=RECORDINGREADY.encode(),
                headers={"Authorization": "wrong", "X-Hub-Signature-256": sig})
    out = caplog.text
    assert "path=recordingready" in out and "event=RECORDINGREADY" in out and "status=200" in out
    for secret in ("+442075551234", "Customer Support", "Jane Smith", "jsmith@acme.com",
                   "+447700900123", TOKEN, "wrong", sig, "content.evonex.com"):
        assert secret not in out


# ------------------------------------------------- worksheet setup (mocked gspread)

class FakeWS:
    def __init__(self, header):
        self.header, self.updates, self.appended = header, [], []

    def row_values(self, n):
        return self.header

    def update(self, values, range_name):
        self.updates.append((values, range_name))

    def append_row(self, values, **kwargs):
        self.appended.append((values, kwargs))


class FakeSpreadsheet:
    def __init__(self, ws, exists=True):
        self.ws, self.exists, self.created = ws, exists, []

    def worksheet(self, title):
        if not self.exists:
            raise gspread.WorksheetNotFound(title)
        return self.ws

    def add_worksheet(self, title, rows, cols):
        self.created.append((title, rows, cols))
        return self.ws


def fake_client(spreadsheet, constructed):
    class FakeGC:
        def set_timeout(self, t):
            pass

    class FakeSheetsClient:
        def __init__(self, sheet_id=None):
            constructed.append(sheet_id)
            self.gc, self.sheet = FakeGC(), spreadsheet

    return FakeSheetsClient


def test_missing_tab_and_header_are_created_once_and_cached(monkeypatch):
    ws, constructed = FakeWS([]), []
    sp = FakeSpreadsheet(ws, exists=False)
    monkeypatch.setattr(evonex, "SheetsClient", fake_client(sp, constructed))
    assert evonex._worksheet() is ws and evonex._worksheet() is ws
    assert sp.created == [("Evonex_Events", 1000, len(evonex.COLUMNS))]
    assert ws.updates == [([evonex.COLUMNS], "A1")]
    assert constructed == ["dummy-evonex-sheet"]  # cached: only one client built


def test_existing_header_is_not_rewritten(monkeypatch):
    ws = FakeWS(list(evonex.COLUMNS))
    monkeypatch.setattr(evonex, "SheetsClient", fake_client(FakeSpreadsheet(ws), []))
    evonex._worksheet()
    assert ws.updates == []


def test_append_is_raw_insert_rows_anchored(monkeypatch):
    ws = FakeWS(list(evonex.COLUMNS))
    monkeypatch.setattr(evonex, "_append_to_sheet", ORIGINAL_APPEND)  # the autouse fixture stubs it
    monkeypatch.setattr(evonex, "SheetsClient", fake_client(FakeSpreadsheet(ws), []))
    evonex._append_to_sheet(["+442075551234"])
    values, kwargs = ws.appended[0]
    assert values == ["+442075551234"]
    assert kwargs["value_input_option"].value == "RAW"
    assert kwargs["insert_data_option"].value == "INSERT_ROWS"
    assert kwargs["table_range"] == "A1"


def test_signature_matches_guides_formula():
    assert evonex.compute_signature("k", "https://x.test/a", b'{"a":1}') == sign('{"a":1}', "https://x.test/a", "k")


# ------------------------------------------------ the live-test script's requests

def _load_script():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "send_test_evonex_event.py"
    spec = importlib.util.spec_from_file_location("send_test_evonex_event", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("event,subevent", [("OUTBOUNDCALL", "STARTCALL"), ("RECORDINGREADY", None)])
def test_script_requests_are_accepted_by_the_receiver(client, state, event, subevent):
    script = _load_script()
    url = BASE + "/api/evonex/call"
    body = json.dumps(script.sample_payload(event, subevent, "test-abc123"), separators=(",", ":"))
    headers = script.build_headers(TOKEN, url, body)
    r = client.post("/api/evonex/call", content=body.encode(), headers=headers)
    assert r.status_code == 200
    row = as_dict(state.rows[0])
    assert row["cid"] == "test-abc123" and row["event"] == event
    if event == "RECORDINGREADY":
        assert row["ctx_url"] == "https://content.evonex.com/content/recording/test"


def test_script_unsigned_and_bearer_variants(client, state):
    script = _load_script()
    url, body = BASE + "/api/evonex/call", json.dumps(script.sample_payload("OUTBOUNDCALL", "STARTCALL", "test-x"))
    unsigned = script.build_headers(TOKEN, url, body, signed=False)
    assert "X-Hub-Signature-256" not in unsigned
    assert client.post("/api/evonex/call", content=body.encode(), headers=unsigned).status_code == 403
    bearer = script.build_headers(TOKEN, url, body, bearer=True)
    assert client.post("/api/evonex/call", content=body.encode(), headers=bearer).status_code == 200
