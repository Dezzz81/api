# 3x-ui API Bridge + Admin

## Quick Install (Windows)
```powershell
git clone https://github.com/<user>/<repo>.git
cd <repo>
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Autostart
```

## Configure
Edit `.env` after install:
- `PANEL_HOST`, `PANEL_USERNAME`, `PANEL_PASSWORD`
- `INBOUND_ID`
- `DATABASE_URL`
- `ADMIN_USER`, `ADMIN_PASS`

If you use multiple 3x-ui servers, fill `PANEL_SERVERS_JSON`.

## Run
```powershell
.\start.bat
```

## Admin
Open:
- `http://<server>:8000/admin`
- `http://<server>:8000/admin/clients`
- `http://<server>:8000/admin/servers`
- `http://<server>:8000/admin/payments`
