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

Нужен только **Docker**. БД не поднимаем — уже существующий Postgres.  
Используется **только** `stimulsoft-csharp/.env` (не корневой).

```bash
cd stimulsoft-csharp
cp .env.example .env

mkdir -p reports && cp /path/to/dashboard.mrt reports/Report.mrt
# Windows путь: MRT_HOST_PATH=C:/Users/You/Report.mrt   (не C:\...)
# PG_CONNECTION_STRING=postgres://USER:PASS@host.docker.internal:54322/app

docker compose up --build -d
```

Ошибка `invalid environment variable: =c` — битая строка в `.env` (часто `C:\...` или строка вида `=...`). Пересоздай из `.env.example`.

Образы: `mcr.microsoft.com/dotnet/sdk:8.0` / `aspnet:8.0`.

http://127.0.0.1:8050/ · проверка БД: http://127.0.0.1:8050/dbcheck  
логи: `docker compose logs -f viewer`

Если `/dbcheck` → `ok: false`:
1. Postgres запущен и порт с хоста открыт (`54322`)
2. в `.env` именно `host.docker.internal`, не `127.0.0.1`
3. пароль/user/db как в корневом Postgres
