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

    /// <summary>URL или уже готовый Host=...; — всегда нормализуем в Host=;Port=;Database=;User Id=;Password=</summary>
    public string? PgConnectionString { get; set; }

    /// <summary>Строка для Stimulsoft / Npgsql (только key=value).</summary>
    public string BuildConnectionString()
    {
        var raw = Clean(PgConnectionString);
        if (raw != null)
        {
            if (LooksLikeKeyValue(raw))
                return raw;

            if (TryParseUrl(raw, out var fromUrl))
                return fromUrl;

            // не URL и не Host= — не отдаём как есть (будет "index 0")
            throw new InvalidOperationException(
                "PG_CONNECTION_STRING должен быть postgres://user:pass@host:port/db " +
                "или Host=...;Port=...;Database=...;User Id=...;Password=...");
        }

        return Format(PostgresHost, PostgresPort, PostgresDb, PostgresUser, PostgresPassword);
    }

    public string MaskedConnectionString()
    {
        try
        {
            return System.Text.RegularExpressions.Regex.Replace(
                BuildConnectionString(),
                @"(Password|Pwd)\s*=\s*[^;]*",
                "$1=***",
                System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        }
        catch
        {
            return "(invalid)";
        }
    }

    private static string? Clean(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
            return null;
        var v = value.Trim().Trim('\uFEFF');
        if ((v.StartsWith('"') && v.EndsWith('"')) || (v.StartsWith('\'') && v.EndsWith('\'')))
            v = v[1..^1].Trim();
        return string.IsNullOrWhiteSpace(v) ? null : v;
    }

    private static bool LooksLikeKeyValue(string s) =>
        s.Contains('=')
        && !s.Contains("://", StringComparison.Ordinal)
        && (s.Contains("Host=", StringComparison.OrdinalIgnoreCase)
            || s.Contains("Server=", StringComparison.OrdinalIgnoreCase)
            || s.Contains("Username=", StringComparison.OrdinalIgnoreCase)
            || s.Contains("User Id=", StringComparison.OrdinalIgnoreCase));

    private static bool TryParseUrl(string raw, out string connectionString)
    {
        connectionString = "";
        raw = raw.Replace("postgresql://", "postgres://", StringComparison.OrdinalIgnoreCase);
        if (!raw.StartsWith("postgres://", StringComparison.OrdinalIgnoreCase))
            return false;

        if (!Uri.TryCreate(raw, UriKind.Absolute, out var uri) || string.IsNullOrEmpty(uri.Host))
            return false;

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

        var port = uri.Port > 0 ? uri.Port.ToString() : "5432";
        connectionString = Format(uri.Host, port, db, user, pass);
        return true;
    }

    private static string Format(string host, string port, string db, string user, string pass) =>
        // Stimulsoft PG + Npgsql: User Id=; Host=;
        $"Host={host};Port={port};Database={db};User Id={user};Password={pass}";
}
