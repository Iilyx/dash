using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using Npgsql;

namespace StimulsoftViewer.Controllers;

[Route("dbcheck")]
public sealed class DbCheckController : Controller
{
    private readonly DashboardOptions _options;

    public DbCheckController(IOptions<DashboardOptions> options) => _options = options.Value;

    [HttpGet]
    public async Task<IActionResult> Get(CancellationToken ct)
    {
        var url = _options.BuildNpgsqlUrl();
        var info = new Dictionary<string, object?>
        {
            ["ok"] = false,
            ["npgsql"] = MaskUrl(url),
            ["stimulsoft"] = _options.MaskedConnectionString(),
        };

        try
        {
            await using var conn = new NpgsqlConnection(url);
            await conn.OpenAsync(ct);
            await using var cmd = new NpgsqlCommand("select current_database() as db, current_user as u, inet_server_addr()::text as addr", conn);
            await using var r = await cmd.ExecuteReaderAsync(ct);
            if (await r.ReadAsync(ct))
            {
                info["ok"] = true;
                info["database"] = r.GetString(0);
                info["user"] = r.GetString(1);
                info["server"] = r.IsDBNull(2) ? null : r.GetString(2);
            }
        }
        catch (Exception ex)
        {
            info["error"] = $"{ex.GetType().Name}: {ex.Message}";
        }

        return Json(info);
    }

    private static string MaskUrl(string url)
    {
        try
        {
            var u = new Uri(url.Replace("postgresql://", "postgres://", StringComparison.OrdinalIgnoreCase));
            var user = u.UserInfo.Split(':')[0];
            return $"postgres://{user}:***@{u.Host}:{u.Port}{u.AbsolutePath}";
        }
        catch
        {
            return "(unparseable)";
        }
    }
}
