"""GHL custom field IDs, pipeline stages, and user mappings.

All IDs confirmed via live discovery session — do not modify.
"""

CUSTOM_FIELD_IDS = {
    "lead_source":                          "osRgWAYdys0ClHQSgQiG",
    "advisor_name":                         "7gGFW09JD3ByCBZW5PQQ",
    "negotiator":                           "ZGExsv8oWP45j9yMoWsE",
    "admin_handover":                       "3RhIyOFmfqsLLKMbbkCM",
    "admin_call_status":                    "64aVYbvKIcwrgnZjQt57",
    "admin_call_attempt_count":             "8ja7M6RflYbgaOza5Lx0",
    "advisor_call_attempt_count":           "9s0YPzkLjWZzYLI6VjDd",
    "advisor_recommendation_attempt_count": "BLnpuFIJhXMzGb7zJGeA",
    "docs_chase_attempt_count":             "5MAkGIS9rJ2VRPVjS7vd",
    "mortgage_case_type":                   "ZTQKRPVbMm2aw38W0gOn",
    "client_readiness_status":              "feCUoMzmjC1rJqOUehA9",
    "is_estate_agent_lead":                 "ewyFAHDVq2tRqrrovInA",
    "docs_status":                          "7vyXOzDxvdjpoFF0VpGh",
    "advisor_recommendation_status":        "mhjmZpbKiBJwSLNtAKW3",
    "property_status":                      "aLAFgHKhZYbiGrrum9Pw",
    "solicitor_status":                     "liuVRbcaTlITaegfHDUP",
    "application_submission_status":        "87p1rzIZoOfhNtqvHNwa",
    "lender_docs_status":                   "D2bAwUpGPac4QFPuyymo",
    "valuation_result":                     "gAOdpcMj7k8B5XeoLyiG",
    "valuation_impact_status":              "qwCdFCpP64jC6wLqldXH",
    "review_permission_status":             "RHzNy5vOJQSCTzJ216Ca",
    "review_status":                        "kHQ3Qd3z3PNb0digC4se",
    "solicitor_offer_confirmation":         "rRVAyrIH7SC1MPuJxrlP",
    "protection_status":                    "kyZPA9MuoEvuAXnbNyzH",
    "protection_type":                      "0Jd0Sq2SD3zxH6qvEI7G",
    "protection_provider":                  "WNb7d0xGKa7Jz3BiQO7g",
    "protection_policy_number":             "9kvR6ID5rNmaPaqDae3m",
    "protection_premium":                   "atzx9wZUJwM79EpeSQ78",
    "protection_cover_amount":              "d2fEzG3eJkdBNt4OJKeU",
    "protection_start_date":                "nFjEFeOHxSydrTlk1MjV",
    "offer_status":                         "oY3N4QT2fZBYaaL0l2IH",
    "exchange_status":                      "Z5aWbASlLV5kcoUje5mf",
    "exchange_date":                        "LTaaxoeVTBYDxhK12GMD",
    "completion_date":                      "FZONzb7c2w06R4KBWzLU",
    "mortgage_product_roll_off_date":       "borEkqefQdFOYMizo0yg",
    "erc_date":                             "9VTspyuTYzDnHyWAPI6E",
    "completion_status":                    "sDEqNnhWIBIlg6JdKF2n",
    "first_contact_time":                   "ZW67UkioiUqsXwmltIS3",
    "docs_received_time":                   "kybjmCIHf6BPxUtjL5Fh",
    "application_time":                     "TidSXu6bq8cmtYUM0Epl",
    "offer_time":                           "bqG9hROOPni13iWa5hBo",
    "completion_time":                      "R4JejS2G2uj3nTpM4Gvp",
    "client_relationship_status":           "SrYwZRuyeUNc3a4JcWR8",
    "remortgage_campaign_status":           "QrKsWrYe1NhEKMSPnvKL",
    "remortgage_engagement_status":         "P2KcAVmo7qxEuAXUX8rh",
    "pause_automations":                    "If90lxAQst0DbYqHG15k",
    "advisor_call_status":                  "USSbwwmZyvqVHHx7EM1c",
}

PIPELINE = {
    "id": "EQyj0fwCvT8vrLEGTw5I",
    "stages": {
        "42861e5d-6f4d-49ab-9b9d-8316a4da198c": "Lead Received",
        "68272d52-f2b8-47ce-b657-718b465c062a": "Admin Contacted",
        "d803ee87-fdec-46e9-a94a-35b9cb5c851b": "Appointment Booked",
        "0acbc39a-c443-461e-891e-3806632eac2e": "Interested / Not Ready",
        "fb49a23f-58c2-475d-82c1-c9f35e7b3672": "Docs Requested",
        "a333899d-f065-44e2-9c05-af4f0c9dca84": "Docs Received / Full Review",
        "2a78423f-3eb2-4852-b76f-6e116314809f": "Advisor Recommendation",
        "2e8f2e2b-f0ff-46eb-8e25-71bfbf2cb498": "Solicitor / Application Prep",
        "de8a593d-5b02-4002-a641-80296692940e": "Application Submitted",
        "06953314-657f-4e9d-be45-37c4186242a4": "Lender Processing",
        "93d6b171-c56e-46a7-aec5-52cfa98b9421": "Offer Issued",
        "78243983-c3fc-4bb6-91cf-d87f56a7eb43": "Exchange",
        "3a76fcab-1809-4f21-8b5d-e6c0cd404ce5": "Completion",
        "543f238e-3b43-48a7-9490-6eaee1ecd6d4": "Post Completion",
    },
}

# Asset Harbour users only — GrowMySME admins excluded
USERS = {
    "djtLhz7zhOJS2g4emnZo": "Anita Andrews",
    "BzqMAPMOHMDvM3PtrQ9q": "Andrew Barnes",
    "3XYQaYcaaVZtx8MrSk8O": "Alison Gulliver",
    "SyVErpzMkCdlpcOYzoCH": "Anthony Fox",
    "sJMXGhlxwpCNLVnPT0yH": "Ben Robertson",
    "DUQ34y86oR0W3luZGiOV": "Carl Gladman",
    "iPDS5X8cZxvMR0mHWrBi": "Cecilia Szabo",
    "J46T3Muh5tAZ4BYABD20": "Ed Ford",
    "uwwl2uZH0eBfYZNCpOho": "Georgie Wilde",
    "8oZX9bqviMA7a9ICBfEM": "Harry Kalsi",
    "gaAYFa0mvb73rKoyk2Bt": "Henny Lessey",
    "LHCuxEhi20OdJawn7sqO": "Jill Collin",
    "8gThpSD5du6hqSZZGrFu": "Joe Nicholls",
    "mxQuDULhaWjhKo4IPSUP": "Lewis Flude",
    "Ta50rgwP4EQJ1zbkh6tz": "Nikki Lawford",
    "poyqR2minJ3Eff0CvurS": "Sam Potts",
    "uPVbmDyIIi0xtOtZ3T2W": "Sara Almand",
    "SeafyQ6xwrNXVlmNwcex": "Sarah Bennett",
    "jz4vNOtF2dWVYqk8Bd5Z": "Xavier Collin",
}


def get_cf(custom_fields_array: list, field_name: str):
    """Extract a custom field value by name from GHL's id/value array."""
    field_id = CUSTOM_FIELD_IDS.get(field_name)
    if not field_id:
        return None
    for field in custom_fields_array:
        if field.get("id") == field_id:
            return field.get("value")
    return None
