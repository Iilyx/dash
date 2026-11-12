namespace StimulsoftViewer;

internal static class License
{
    public static void Apply(IConfiguration configuration)
    {
        var key =
            FirstNonEmpty(
                Environment.GetEnvironmentVariable("STIMULSOFT_LICENSE_KEY"),
                Environment.GetEnvironmentVariable("STIMULSOFT_DASHBOARDS_LICENSE_KEY"),
                Environment.GetEnvironmentVariable("STIMULSOFT_REPORTS_LICENSE_KEY"),
                configuration["Stimulsoft:LicenseKey"]);

        if (!string.IsNullOrWhiteSpace(key))
            Stimulsoft.Base.StiLicense.Key = key.Trim();
    }

    private static string? FirstNonEmpty(params string?[] values) =>
        values.FirstOrDefault(v => !string.IsNullOrWhiteSpace(v));
}
