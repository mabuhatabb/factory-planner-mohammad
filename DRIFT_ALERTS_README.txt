AUTOMATIC DRIFT ALERTS
======================
drift_job.py checks every APPROVED plan against the newest saved data set. If orders changed
(later finish, now late, ...) it emails the approver and writes a line in the audit trail.
The same change is only emailed once.

It needs a database that stays online (DATABASE_URL), so use it with hosted Postgres.
Remember: the job re-plans the NEWEST SAVED DATA SET. Save your data set in the app after changes.

Run it by hand to test (Windows, in your project folder):
    py -3.12 drift_job.py

Pick ONE way to run it every day:
 A) GitHub Actions: copy drift_alerts.yml to  .github/workflows/drift_alerts.yml  in your repo
    and add the secrets listed at the top of that file. Easiest, nothing to host.
 B) Docker self-hosting: add to cron on the server:
        0 6 * * *  cd /path/to/app && docker compose run --rm app python drift_job.py
 C) Windows Task Scheduler: a daily task that runs  py -3.12 drift_job.py  in the project folder.

Who gets the email: the person who approved (if they approved by email link, their address),
otherwise ALERT_EMAIL.
