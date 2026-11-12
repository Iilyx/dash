using StimulsoftViewer;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddControllersWithViews();
builder.Services.Configure<DashboardOptions>(opts =>
{
    builder.Configuration.GetSection(DashboardOptions.SectionName).Bind(opts);

    opts.MrtPath = Env("MRT_PATH") ?? Env("REPORT_FILE") ?? opts.MrtPath;
    opts.PgConnectionString = Env("PG_CONNECTION_STRING") ?? opts.PgConnectionString;
    opts.PostgresHost = Env("POSTGRES_HOST") ?? opts.PostgresHost;
    opts.PostgresPort = Env("POSTGRES_PORT") ?? opts.PostgresPort;
    opts.PostgresDb = Env("POSTGRES_DB") ?? opts.PostgresDb;
    opts.PostgresUser = Env("POSTGRES_USER") ?? opts.PostgresUser;
    opts.PostgresPassword = Env("POSTGRES_PASSWORD") ?? opts.PostgresPassword;
});
builder.Services.AddSingleton<ReportLoader>();

License.Apply(builder.Configuration);

var app = builder.Build();

var dashOpts = app.Services.GetRequiredService<Microsoft.Extensions.Options.IOptions<DashboardOptions>>().Value;
app.Logger.LogInformation("MRT_PATH env={Mrt} DB={Db}",
    Environment.GetEnvironmentVariable("MRT_PATH"),
    dashOpts.MaskedConnectionString());

if (!app.Environment.IsDevelopment())
    app.UseExceptionHandler("/Viewer/Error");

app.UseStaticFiles();
app.UseRouting();

app.MapControllerRoute(
    name: "default",
    pattern: "{controller=Viewer}/{action=Index}/{id?}");

app.Run();

static string? Env(string name)
{
    var v = Environment.GetEnvironmentVariable(name);
    return string.IsNullOrWhiteSpace(v) ? null : v.Trim();
}
