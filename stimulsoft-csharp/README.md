# Stimulsoft Dashboards viewer (.NET / C#)

ASP.NET Core MVC + **Stimulsoft.Dashboards.Web.NetCore 2024.3.3**.

Это нативный .NET-движок: `.mrt` с **`ScriptLanguage: CSharp`** (Windows Designer) открывается как есть — в отличие от `stimulsoft-app` (Dashboards.JS).

Нужен **.NET 8 SDK** (Windows / Linux; на Mac тоже можно, если установлен SDK).

## Запуск

```powershell
cd stimulsoft-csharp
copy .env.example .env
# MRT_PATH / PG_CONNECTION_STRING / STIMULSOFT_LICENSE_KEY

# подхватить .env в текущую сессию (PowerShell)
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $k,$v = $_.Split('=',2)
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
}

dotnet restore
dotnet run
```

На bash/macOS:

```bash
cd stimulsoft-csharp
cp .env.example .env
set -a && source .env && set +a
dotnet restore && dotnet run
```

- Viewer: http://127.0.0.1:8050/
- Designer: http://127.0.0.1:8050/Designer

Порт 8050, чтобы не пересекаться с `stimulsoft-app` (8040).

## Docker (другой ПК)

Нужен только **Docker**. Своя БД в этом compose **не поднимается** — используется уже существующий Postgres (например корневой `docker compose up -d` в репо).

```bash
cd stimulsoft-csharp
cp .env.example .env

# .mrt
mkdir -p reports && cp /path/to/dashboard.mrt reports/Report.mrt
# или в .env: MRT_HOST_PATH=/полный/путь/dashboard.mrt

# в .env (или в ../.env корня) должны быть:
#   PG_CONNECTION_STRING=postgres://USER:PASS@host.docker.internal:54322/app
#   либо POSTGRES_HOST/PORT/DB/USER/PASSWORD
#   STIMULSOFT_LICENSE_KEY=...

docker compose up --build -d
```

Compose тянет `../.env` и `.env`, в контейнер явно прокидывает `PG_*` / `POSTGRES_*` / license.

http://127.0.0.1:8050/ · логи: `docker compose logs -f viewer`
