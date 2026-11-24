# Meals (calorie_counter) · Gateway SSO user migration · 2026-11-23

Pilots 488 (SSO) + 489 (DDUUI chrome). This note covers how existing Meals
users move onto Gudmundr Gateway subjects without losing meal history.

## What changes for a user

- Sign-in is **Sign in with Gudmundr** (Gateway `/auth/login`, Google at the
  Gateway). The old Google button stays under "Other ways to sign in" as a
  legacy fallback; existing Google sessions keep working until they expire.
- A local PIN fallback exists for operators only: it is off unless
  `CC_FALLBACK_PIN` is set on the service, and it accepts only addresses in
  `ADMIN_EMAILS` or `allowed_emails`. No default PIN exists in source.

## How history carries over (no data rewrite)

All user data is keyed by email: `entries.user_email`, `blood_sugar.user_email`,
`journal_entry_email.email`, `outbound_sync` payloads. Gateway tokens carry the
same Google email, so the history follows the person automatically.

On every Gateway sign-in the callback:

1. verifies the RS256 token against `{GATEWAY_URL}/jwks.json` (falls back to
   `/auth/userinfo` only if the JWKS cannot be fetched);
2. applies per-app authorization: Gateway access claim v1 (`gw_app`,
   `gw_access`, `gw_enforced`). While `gw_enforced` is false for
   `calorie_counter`, the app's own rule applies (ADMIN_EMAILS, or listed in
   `allowed_emails`, or the table is empty);
3. resolves the **canonical email**: the exact address (case-insensitive match)
   already used by that person's `entries` / `blood_sugar` rows, so a
   capitalisation difference between Google-era rows and the Gateway email
   never splits history;
4. upserts `gateway_subjects (sub, email, name, first_seen, last_seen)`, the
   Gateway-subject ↔ email map (new table, created by `init_db`, idempotent).

No existing row is updated, moved or deleted.

## Moving the allow-list into the Gateway (operator step)

`scripts/gateway_access_seed.py` (already in the repo) builds the add-only
payload for `POST {GATEWAY_URL}/svc/access/import` from the live
`allowed_emails` table (ADMIN_EMAILS → admin, others → editor). Run it inside
the service, review the file, then import with the Gateway service token.
After the import, flip enforcement for `calorie_counter` in the Gateway admin
(`/svc/access/apps/calorie_counter/enforce`); from then on the Gateway access
list is authoritative and is re-polled every 60 s for revocation.

## Verify after deploy

```sql
-- every Gateway subject maps to an email that owns history (or is new)
SELECT g.sub, g.email,
       (SELECT COUNT(*) FROM entries e WHERE e.user_email = g.email) AS meals
FROM gateway_subjects g ORDER BY g.last_seen DESC;
```

## Rollback

The change is additive (one new table, no rewrites). Redeploying the previous
image restores the Google-only sign-in; `gateway_subjects` is ignored by it.
