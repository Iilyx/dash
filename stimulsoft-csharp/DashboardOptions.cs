namespace StimulsoftViewer;

public sealed class DashboardOptions
{
    public const string SectionName = "Dashboard";

    /// <summary>Path to .mrt (absolute or relative to content root / repo root).</summary>
    public string MrtPath { get; set; } = "../test.mrt";

    public string PostgresHost { get; set; } = "127.0.0.1";
    public string PostgresPort { get; set; } = "5432";
    public string PostgresDb { get; set; } = "app";
    public string PostgresUser { get; set; } = "admin";
    public string PostgresPassword { get; set; } = "admin123";

    /// <summary>Optional full URL: postgres://user:pass@host:port/db</summary>
    public string? PgConnectionString { get; set; }

    public string BuildConnectionString()
    {
        if (!string.IsNullOrWhiteSpace(PgConnectionString))
        {
            // Stimulsoft .NET PG adapter expects Host=;Port=;Database=;User Id=;Password=
            var raw = PgConnectionString.Trim()
                .Replace("postgresql://", "postgres://", StringComparison.OrdinalIgnoreCase);
            if (raw.StartsWith("postgres://", StringComparison.OrdinalIgnoreCase)
                && Uri.TryCreate(raw, UriKind.Absolute, out var uri)
                && !string.IsNullOrEmpty(uri.Host))
            {
                var db = uri.AbsolutePath.Trim('/').Split('?', 1)[0];
                var user = Uri.UnescapeDataString(uri.UserInfo.Split(':')[0]);
                var pass = uri.UserInfo.Contains(':')
                    ? Uri.UnescapeDataString(uri.UserInfo[(uri.UserInfo.IndexOf(':') + 1)..])
                    : "";
                return $"Host={uri.Host}; Port={uri.Port}; Database={db}; User Id={user}; Password={pass};";
            }

            return PgConnectionString.Trim();
        }

        return $"Host={PostgresHost}; Port={PostgresPort}; Database={PostgresDb}; " +
               $"User Id={PostgresUser}; Password={PostgresPassword};";
    }
}
