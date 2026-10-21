# PETA Dashboard

The dashboard is served by the FastAPI application. Do not open the old
standalone HTML copies directly: they are local files and are not protected by
the server-side access controls.

## Local setup

Install the dependencies and set a private admin password and session secret in
PowerShell. Do not commit either value:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PETA_ADMIN_PASSWORD = Read-Host "Set a strong admin password (14+ characters)"
$env:PETA_SESSION_SECRET = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
$env:PETA_COOKIE_SECURE = "false"
.\.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```

On startup, an empty database is seeded automatically from
`PRISMA_DATA_FINAL.xlsx` in the project folder. Unit names for Analis, Auditor,
and Asesor are loaded from `NAMA UNIT KOMPETENSI.xlsx` and supplied to the
dashboard charts. Set `PETA_SEED_WORKBOOK` or `PETA_UNIT_NAMES_WORKBOOK` to use
different local workbook paths. Seeding uses the same validation and account
creation logic as the admin import endpoint and only runs when the participant
table is empty; an existing dataset is never overwritten at startup. The unit
name workbook is loaded at every startup, including when the database already
contains participant data. A missing or invalid unit-name workbook always stops
startup; a missing or invalid participant workbook stops startup when the
database is empty.

Open `http://127.0.0.1:8000/` and sign in as `admin` with the configured
password. The server stores seeded/imported rows and password hashes in
`server_data/prisma.sqlite3`; it does not serve workbooks or the old
embedded-data HTML copies. The **Muat Excel** action remains available to admins
for future dataset updates.

Participant accounts are created from non-empty name/NIP pairs in the seeded or
admin-imported workbook. The initial password is the participant's NIP and must
be changed before any dashboard data is returned. Use a new password of at
least 12 characters. An import preserves changed passwords for NIPs that remain
in the dataset and removes accounts for NIPs no longer present.

For deployment beyond loopback, use HTTPS, set `PETA_COOKIE_SECURE=true`, use a
strong private `PETA_SESSION_SECRET` of at least 32 characters, and store
`PETA_DB_PATH` outside any public/static directory. Back up the SQLite database
securely. This application does not configure a public reverse proxy or TLS.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```
