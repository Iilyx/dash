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

    public string BuildConnectionString() =>
        $"Host={PostgresHost};Port={PostgresPort};Database={PostgresDb};Username={PostgresUser};Password={PostgresPassword}";
}
