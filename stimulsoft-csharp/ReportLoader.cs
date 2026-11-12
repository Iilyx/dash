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

        var len = new FileInfo(path).Length;
        if (len < 64)
            throw new InvalidOperationException(
                $"MRT пустой или слишком маленький ({len} bytes): {path}. " +
                "Положи файл в reports/Report.mrt или поправь MRT_HOST_PATH.");

        _log.LogInformation("Loading dashboard: {Path} ({Len} bytes)", path, len);
        _log.LogInformation("DB connection: {Cs}", _options.MaskedConnectionString());

        var report = StiReport.CreateNewDashboard();
        report.Load(path);

        // иначе холст белый, пока не нажмут Submit на панели параметров
        report.RequestParameters = false;

        ApplyPostgresConnection(report);

        _log.LogInformation(
            "Loaded: pages={Pages} databases={Db} requestParameters={RP}",
            report.Pages?.Count ?? 0,
            report.Dictionary?.Databases?.Count ?? 0,
            report.RequestParameters);

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
