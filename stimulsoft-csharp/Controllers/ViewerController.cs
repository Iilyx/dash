using Microsoft.AspNetCore.Mvc;
using Stimulsoft.Report.Mvc;

namespace StimulsoftViewer.Controllers;

public sealed class ViewerController : Controller
{
    private readonly ReportLoader _loader;
    private readonly ILogger<ViewerController> _log;

    public ViewerController(ReportLoader loader, ILogger<ViewerController> log)
    {
        _loader = loader;
        _log = log;
    }

    public IActionResult Index() => View();

    public IActionResult Error() => Content("Viewer error — check server logs.");

    public IActionResult GetReport()
    {
        try
        {
            var report = _loader.LoadDashboard();
            return StiNetCoreViewer.GetReportResult(this, report);
        }
        catch (Exception ex)
        {
            _log.LogError(ex, "GetReport failed");
            // чтобы в UI было видно причину, а не пустой экран
            return Content(
                "GetReport failed: " + ex.Message +
                "\n\nПроверь MRT_HOST_PATH / reports/Report.mrt и /dbcheck\n\n" + ex,
                "text/plain; charset=utf-8");
        }
    }

    public IActionResult ViewerEvent() =>
        StiNetCoreViewer.ViewerEventResult(this);
}
