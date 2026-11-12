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
        var info = new Dictionary<string, object?>
        {
            ["ok"] = false,
            ["stimulsoft"] = _options.MaskedConnectionString(),
        };

        try
        {
            // только Host=...; формат — URI даёт "initialization string ... index 0"
            var cs = _options.BuildConnectionString();
            await using var conn = new NpgsqlConnection(cs);
            await conn.OpenAsync(ct);
            await using var cmd = new NpgsqlCommand(
                "select current_database() as db, current_user as u", conn);
            await using var r = await cmd.ExecuteReaderAsync(ct);
            if (await r.ReadAsync(ct))
            {
                info["ok"] = true;
                info["database"] = r.GetString(0);
                info["user"] = r.GetString(1);
            }
        }
        catch (Exception ex)
        {
            info["error"] = $"{ex.GetType().Name}: {ex.Message}";
        }

        return Json(info);
    }
}
