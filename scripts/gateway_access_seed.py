"""Build the one-time Gateway access seed for calorie_counter.

Run inside the service (it reads the live allowed_emails table on the
volume) and write the payload to a file; nothing is printed or sent:

    python scripts/gateway_access_seed.py /tmp/calorie_counter_seed.json

Then, once the Gateway side is live and the operator confirms:
    POST {GATEWAY_URL}/svc/access/import  (service token, add-only)

Mapping: ADMIN_EMAILS -> admin · every allowed_emails row -> editor.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gateway_access  # noqa: E402
from app import ADMIN_EMAILS, get_db  # noqa: E402

out = Path(sys.argv[1])
conn = get_db()
rows = [r["email"] for r in conn.execute("SELECT email FROM allowed_emails")]
conn.close()
users = {e.strip().lower(): "editor" for e in rows if e}
users.update({e: "admin" for e in ADMIN_EMAILS})
payload = gateway_access.import_payload("calorie_counter", "calorie_counter.allowed_emails", list(users.items()))
out.write_text(json.dumps(payload, indent=2))
os.chmod(out, 0o600)
print(f"wrote {len(payload['users'])} users to {out}")
