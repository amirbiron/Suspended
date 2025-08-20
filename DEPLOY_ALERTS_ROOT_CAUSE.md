# Deploy Alerts – Root Cause Analysis and Fixes

## Summary (TL;DR)
- Primary root cause: changes in Render’s Deploys API response shape (deploy object wrapped under `deploy`) caused our parser to miss `id`/`status`, so terminal detection never fired and no alerts were sent.
- Secondary blockers: deploy-event checks were gated on live-status success; Telegram Markdown issues could silently drop messages; deploy notifications toggling didn’t upsert service docs; manual-action suppression hid deploy-finish alerts; monitor wasn’t guaranteed to run.
- Fixes: robust API parsing, unconditional deploy checks, Telegram message hardening, DB upsert, suppression exception for deploy finish, faster polling, always start monitor, active deploy watch after resume, diagnostics and better logging, and clearer alert titles.

---

## What prevented alerts (before)

1) Render API response shape changed
   - Old assumption: `/services/{id}/deploys?limit=1` returns either a list or `{ items | data }` with deploy fields at top-level.
   - Reality (per logs): response sometimes arrives as `{ deploy: { ... }, cursor: ... }`.
   - Impact: our parser read `id=None` and `status=None` (because it looked at the wrapper, not the inner object), so:
     - Terminal detection (`succeeded/failed/...`) never matched
     - No alert was sent

2) Deploy checks ran only if live status fetch succeeded
   - If `get_service_status` returned `None`, we skipped the deploy-event check entirely.
   - Impact: missed deploy-finish events during intermittent API hiccups.

3) Telegram message formatting could fail silently
   - Status-change alerts did not fully escape Markdown for service names/actions/status values.
   - Telegram may return `ok=false` on invalid Markdown, effectively dropping the alert.

4) Service not present in DB → skipped in scans
   - Toggling deploy notifications did not upsert a `service_activity` doc.
   - If the service wasn’t in DB yet, it wasn’t scanned for deploy events at all.

5) Manual-action suppression hid deploy-finish
   - After Resume/Suspend, we suppressed status-change alerts for a few minutes to avoid noise.
   - Side-effect: a deploy-finish transition could be suppressed as well.

6) Monitor lifecycle and polling
   - Monitor sometimes wasn’t started due to configuration; polling cadence didn’t accelerate reliably during deploys.
   - Result: late or missed transitions.

---

## Fixes implemented

1) Robust parsing of Render deploy responses (`render_api.py`)
   - Support `{ deploy: {...}, cursor: ... }` wrapper and legacy shapes (list, `items`, `data`).
   - Extract `id/status/commit/createdAt/updatedAt` from the inner deploy entity.
   - Select latest deploy by `updatedAt/finishedAt/completedAt/createdAt` rather than positional assumptions.

2) Always check deploy events (`status_monitor.py`)
   - Run `_check_deploy_events` even when live status is unavailable.
   - Added `_process_deploy_transition_for_notif` to emit deploy-finish alerts (deploying → online/offline) even when status monitoring is off, if deploy alerts are enabled for the service.

3) Telegram notification hardening (`notifications.py`)
   - Full Markdown escaping for names/actions/statuses + commit trimming.
   - Validate `ok` in Telegram response; log rejection reason.
   - Add short title line with the service name so previews show the target service immediately.
   - Fan out to the user who enabled monitoring (in addition to admin) when applicable.

4) DB and toggle behavior (`database.py`)
   - `toggle_deploy_notifications` now uses upsert to ensure a doc exists, so services are scanned even if they were not seen before.

5) Manual-action suppression exception (`status_monitor.py`)
   - Do not suppress deploy-finish notifications (deploying → online/offline) even if a manual action happened just now.

6) Monitor lifecycle & polling (`main.py`, `status_monitor.py`)
   - Start monitor unconditionally; faster polling while any service is deploying or deploy alerts are enabled.
   - Add active watcher (`watch_deploy_until_terminal`) after Resume to guarantee one-shot alert on finish.

7) Diagnostics & observability
   - Rich logs around deploy checks (latest snapshot, terminal detection, send outcome).
   - `/diag` command and `DIAG_ON_START` to print quick state.

---

## Operational guidance (current)
- Ensure per-service deploy alerts are enabled in the bot’s UI.
- Consider setting (temporarily for testing):
  - `STATUS_CHECK_INTERVAL_SECONDS=30`
  - `DEPLOY_CHECK_INTERVAL_SECONDS=10`
  - `DIAG_ON_START=true` (for boot diagnostics)
- Expect logs like:
  - `Latest deploy info: service=... id=... status_raw=... simplified=...`
  - `Terminal deploy detected ...` followed by `Deploy notification sent ...`

---

## Files touched
- `render_api.py`: robust deploy parsing
- `status_monitor.py`: unconditional deploy checks, deploy-transition handling, active watch, suppression exception, logging, faster polling
- `database.py`: upsert in toggle
- `notifications.py`: escaping, Telegram ok-check, preview title, fan-out to enabling user
- `main.py`: import fallback, Conflict exit, always start monitor, diagnostics

---

## Testing checklist
- Toggle deploy alerts for a service.
- Trigger a deploy; verify logs show latest snapshot and terminal detection; receive a single alert.
- Run `/test_monitor <service_id> deploy_ok` to simulate deploying → online; verify alert.
- Test Resume via bot; ensure active watcher sends a deploy-finish alert once.

---

## Impact
- Alerts now trigger reliably on deploy finish (success/failure) and on deploying → online/offline transitions.
- Messages render correctly in Telegram and previews clearly show the service in the first line.
- Diagnostics make future issues fast to pinpoint.