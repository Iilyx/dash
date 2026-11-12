using Microsoft.AspNetCore.Mvc;
using Stimulsoft.Report.Mvc;

namespace StimulsoftViewer.Controllers;

public sealed class DesignerController : Controller
{
    private readonly ReportLoader _loader;
    private readonly ILogger<DesignerController> _log;

    public DesignerController(ReportLoader loader, ILogger<DesignerController> log)
    {
        _loader = loader;
        _log = log;
    }

    public IActionResult Index() => View();

    public IActionResult GetReport()
    {
        try
        {
            var report = _loader.LoadDashboard();
            return StiNetCoreDesigner.GetReportResult(this, report);
        }
        catch (Exception ex)
        {
            _log.LogError(ex, "Designer GetReport failed");
            throw;
        }
    }

    public IActionResult DesignerEvent() =>
        StiNetCoreDesigner.DesignerEventResult(this);
}
