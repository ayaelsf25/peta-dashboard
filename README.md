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
different local workbook paths, or set `PETA_SEED_WORKBOOK_URL` and
`PETA_UNIT_NAMES_WORKBOOK_URL` to download them into `/tmp`. Downloaded files
are cached there and reused while present. Seeding uses the same validation and
account creation logic as the admin import endpoint and only runs when the
participant table is empty; an existing dataset is never overwritten at
startup. The unit name workbook is loaded at every startup, including when the
database already contains participant data. A missing or invalid unit-name
workbook always stops startup; a missing or invalid participant workbook stops
startup when the database is empty.

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

## Deploy gratis ke Render

File `render.yaml` mengonfigurasi Web Service gratis, perintah start, health
check, dan database di `/tmp/prisma.sqlite3`. Sebelum membuat service, unggah
kedua workbook ke penyimpanan yang bisa diakses oleh Render tanpa login.
Gunakan URL direct download (untuk Google Drive, format umumnya
`https://drive.google.com/uc?export=download&id=FILE_ID`) dan pastikan file bisa
diunduh tanpa halaman konfirmasi atau akses akun Google.

1. Pastikan `PRISMA_DATA_FINAL.xlsx` dan `NAMA UNIT KOMPETENSI.xlsx` tidak
   di-commit ke GitHub. Buat/cek repository GitHub dan push kode, termasuk
   `render.yaml`.
2. Di Render, pilih **New + → Blueprint** dan hubungkan repository GitHub.
   Render membaca `render.yaml` dan membuat Web Service `peta-dashboard`.
   Jika membuat service manual, pilih **New + → Web Service**, hubungkan repo,
   pilih runtime **Python**, plan **Free**, build command
   `pip install -r requirements.txt`, dan start command
   `python -m uvicorn server:app --host 0.0.0.0 --port $PORT`.
3. Di **Environment** pada service, isi:
   - `PETA_ADMIN_PASSWORD`: password admin privat, minimal 14 karakter.
   - `PETA_SESSION_SECRET`: secret acak privat, minimal 32 karakter. Contoh
     membuat nilai lokal: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   - `PETA_COOKIE_SECURE`: `true`.
   - `PETA_DB_PATH`: `/tmp/prisma.sqlite3`.
   - `PETA_SEED_WORKBOOK_URL`: direct download URL untuk
     `PRISMA_DATA_FINAL.xlsx`.
   - `PETA_UNIT_NAMES_WORKBOOK_URL`: direct download URL untuk
     `NAMA UNIT KOMPETENSI.xlsx`.
4. Simpan environment variables lalu deploy. Render mengunduh workbook saat
   startup pertama kali, memvalidasinya, lalu mengisi SQLite jika database
   belum memiliki data. Buka URL service Render dan masuk sebagai `admin`
   menggunakan password yang dikonfigurasi.
5. Jangan tambahkan password, session secret, atau URL privat ke kode maupun
   GitHub. Ubah URL environment variables di Render bila lokasi file berubah;
   file yang sudah ada di `/tmp` baru diunduh ulang bila cache tersebut hilang.

**Batasan paket gratis:** `/tmp` bersifat sementara dan SQLite akan hilang saat
instance diganti atau filesystem dibersihkan. Data hasil import, perubahan
password, serta sesi dapat hilang; pada startup berikutnya data peserta hanya
akan di-seed lagi dari Excel ketika database kosong. Paket gratis bukan pilihan
untuk menyimpan perubahan database secara permanen tanpa layanan database
eksternal atau disk persisten.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```
