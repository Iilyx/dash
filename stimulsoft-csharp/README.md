# Stimulsoft Dashboards viewer (.NET / C#)

ASP.NET Core MVC + **Stimulsoft.Dashboards.Web.NetCore 2024.3.3** — тот же .NET-движок, что Windows Designer 2024. CSharp-скрипты и Request Parameters из `.mrt` работают нативно (в отличие от Dashboards.JS 2024.3.3).

Нужен **.NET 8 SDK** и Windows/Linux (на этом Mac `dotnet` не установлен — собирай на машине с SDK).

## Запуск (Windows)

```powershell
cd stimulsoft-csharp
copy .env.example .env
# правь MRT_PATH / POSTGRES_* / license в .env или в appsettings.json

# подхватить .env в сессию (или задай переменные вручную / через launchSettings)
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $k,$v = $_.Split('=',2); [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
}

dotnet restore
dotnet run
```

Откроется [http://127.0.0.1:8040](http://127.0.0.1:8040) — Viewer. Designer: `/Designer`.

## Конфиг

| Переменная / ключ | Назначение |
|---|---|
| `MRT_PATH` / `Dashboard:MrtPath` | путь к `.mrt` (относительно `stimulsoft-csharp/` или абсолютный) |
| `POSTGRES_*` | строка подключения подменяется во всех SQL-базах словаря MRT |
| `STIMULSOFT_DASHBOARDS_LICENSE_KEY` | лицензия (иначе trial watermark) |

По умолчанию `MRT_PATH=../test.mrt` (файл в корне репо).
