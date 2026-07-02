"""Read back the Leads and Sync_Log tabs to confirm the sync landed."""

from app.sheets_client import SheetsClient
from app.join import COLUMNS

sc = SheetsClient()

leads = sc.sheet.worksheet("Leads")
values = leads.get_all_values()
print(f"Leads tab: {len(values) - 1} data rows, {len(values[0])} columns")
print(f"Headers match COLUMNS order: {values[0] == COLUMNS}")

print("\nFirst 5 data rows (first 8 columns):")
print(" | ".join(values[0][:8]))
for row in values[1:6]:
    print(" | ".join(row[:8]))

log = sc.sheet.worksheet("Sync_Log")
print("\nSync_Log tab:")
for row in log.get_all_values():
    print(" | ".join(row))
