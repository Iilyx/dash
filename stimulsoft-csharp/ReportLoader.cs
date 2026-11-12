using Microsoft.Extensions.Options;
using Stimulsoft.Report;
using Stimulsoft.Report.Dictionary;

namespace StimulsoftViewer;

public sealed class ReportLoader
{
    private readonly DashboardOptions _options;
    private readonly IWebHostEnvironment _env;
    private readonly ILogger<ReportLoader> _log;

    public ReportLoader(
        IOptions<DashboardOptions> options,
        IWebHostEnvironment env,
        ILogger<ReportLoader> logger)
    {
        _options = options.Value;
        _env = env;
        _log = logger;
    }

    public StiReport LoadDashboard()
    {
        var path = ResolveMrtPath();
        if (!File.Exists(path))
            throw new FileNotFoundException($"MRT not found: {path}");

        _log.LogInformation("Loading dashboard: {Path}", path);
        _log.LogInformation("DB connection: {Cs}", _options.MaskedConnectionString());

        var report = StiReport.CreateNewDashboard();
        report.Load(path);
        ApplyPostgresConnection(report);
        return report;
    }

    private void ApplyPostgresConnection(StiReport report)
    {
        var cs = _options.BuildConnectionString();
        var n = 0;

        foreach (StiDatabase db in report.Dictionary.Databases)
        {
            if (db is StiSqlDatabase sql)
            {
                sql.ConnectionString = cs;
                n++;
                _log.LogInformation("Connection applied to SQL database '{Name}' ({Type})", sql.Name, sql.GetType().Name);
                continue;
            }

            // на всякий случай — любая БД со свойством ConnectionString
            var prop = db.GetType().GetProperty("ConnectionString");
            if (prop?.CanWrite == true && prop.PropertyType == typeof(string))
            {
                prop.SetValue(db, cs);
                n++;
                _log.LogInformation("Connection applied via reflection to '{Name}'", db.Name);
            }
        }

        if (n == 0)
        {
            _log.LogWarning("No SQL databases in MRT — adding StiPostgreSQLDatabase");
            report.Dictionary.Databases.Add(new StiPostgreSQLDatabase("PostgreSQL", cs));
        }
    }

    private string ResolveMrtPath()
    {
        var raw =
            Environment.GetEnvironmentVariable("MRT_PATH")
            ?? Environment.GetEnvironmentVariable("REPORT_FILE")
            ?? _options.MrtPath;

        if (string.IsNullOrWhiteSpace(raw))
            throw new InvalidOperationException("MRT_PATH / REPORT_FILE / Dashboard:MrtPath is empty");

        if (Path.IsPathRooted(raw))
            return Path.GetFullPath(raw);

        var candidates = new[]
        {
            Path.GetFullPath(Path.Combine(_env.ContentRootPath, raw)),
            Path.GetFullPath(Path.Combine(_env.ContentRootPath, "..", raw)),
            Path.GetFullPath(Path.Combine(Directory.GetCurrentDirectory(), raw)),
        };

        return candidates.FirstOrDefault(File.Exists) ?? candidates[0];
    }
}
