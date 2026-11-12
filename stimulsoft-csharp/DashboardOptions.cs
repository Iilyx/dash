namespace StimulsoftViewer;

public sealed class DashboardOptions
{
    public const string SectionName = "Dashboard";

    public string MrtPath { get; set; } = "../test.mrt";

    public string PostgresHost { get; set; } = "127.0.0.1";
    public string PostgresPort { get; set; } = "5432";
    public string PostgresDb { get; set; } = "app";
    public string PostgresUser { get; set; } = "admin";
    public string PostgresPassword { get; set; } = "admin123";

    /// <summary>Optional URL: postgres://user:pass@host:port/db</summary>
    public string? PgConnectionString { get; set; }

    /// <summary>Stimulsoft/Npgsql: Host=;Port=;Database=;User Id=;Password=</summary>
    public string BuildConnectionString()
    {
        if (!string.IsNullOrWhiteSpace(PgConnectionString))
        {
            var raw = PgConnectionString.Trim()
                .Replace("postgresql://", "postgres://", StringComparison.OrdinalIgnoreCase);

            if (raw.StartsWith("postgres://", StringComparison.OrdinalIgnoreCase)
                && Uri.TryCreate(raw, UriKind.Absolute, out var uri)
                && !string.IsNullOrEmpty(uri.Host))
            {
                var db = uri.AbsolutePath.Trim('/').Split('?', 1)[0];
                var user = "";
                var pass = "";
                if (!string.IsNullOrEmpty(uri.UserInfo))
                {
                    var parts = uri.UserInfo.Split(':', 2);
                    user = Uri.UnescapeDataString(parts[0]);
                    if (parts.Length > 1)
                        pass = Uri.UnescapeDataString(parts[1]);
                }

                var port = uri.Port > 0 ? uri.Port : 5432;
                return Format(uri.Host, port.ToString(), db, user, pass);
            }

            // уже Host=...; формат
            if (raw.Contains("Host=", StringComparison.OrdinalIgnoreCase)
                || raw.Contains("Server=", StringComparison.OrdinalIgnoreCase))
                return raw;

            return raw;
        }

        return Format(PostgresHost, PostgresPort, PostgresDb, PostgresUser, PostgresPassword);
    }

    public string BuildNpgsqlUrl()
    {
        // для /dbcheck через Npgsql
        if (!string.IsNullOrWhiteSpace(PgConnectionString)
            && PgConnectionString.Contains("://", StringComparison.Ordinal))
        {
            return PgConnectionString.Trim()
                .Replace("postgresql://", "postgres://", StringComparison.OrdinalIgnoreCase);
        }

        return $"postgres://{Uri.EscapeDataString(PostgresUser)}:{Uri.EscapeDataString(PostgresPassword)}" +
               $"@{PostgresHost}:{PostgresPort}/{PostgresDb}";
    }

    public string MaskedConnectionString()
    {
        var cs = BuildConnectionString();
        return System.Text.RegularExpressions.Regex.Replace(
            cs,
            @"(Password|Pwd)\s*=\s*[^;]*",
            "$1=***",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    }

    private static string Format(string host, string port, string db, string user, string pass) =>
        $"Host={host}; Port={port}; Database={db}; User Id={user}; Password={pass};";
}
