// Общая подготовка отчёта: Postgres + Preview без фильтров
window.StiApp = {
  applyLicense: function (cfg) {
    if (!cfg || !cfg.licenseKey) return;
    try {
      if (Stimulsoft.Base && Stimulsoft.Base.StiLicense)
        Stimulsoft.Base.StiLicense.key = cfg.licenseKey;
    } catch (e) {}
  },

  setupAdapter: function () {
    var dataUrl = window.location.origin + "/data";
    StiOptions.WebServer.url = dataUrl;
    StiOptions.WebServer.encryptData = false;
    StiOptions.WebServer.checkDataAdaptersVersion = false;
    return dataUrl;
  },

  isScrubbed: function (jsonText) {
    try {
      var d = JSON.parse(jsonText);
      var db0 = d.Dictionary && d.Dictionary.Databases && d.Dictionary.Databases["0"];
      return !db0 || !String(db0.Ident || "").trim() || !String(db0.Name || "").trim();
    } catch (e) {
      return true;
    }
  },

  isFilterComponent: function (comp) {
    if (!comp) return false;
    var name = String(comp.name || "");
    var typeName = "";
    try {
      if (comp.componentType != null) typeName = String(comp.componentType);
    } catch (e) {}
    try {
      if (!typeName && comp.ident) typeName = String(comp.ident);
    } catch (e) {}
    try {
      if (!typeName && comp.constructor && comp.constructor.name)
        typeName = comp.constructor.name;
    } catch (e) {}
    var s = (typeName + " " + name).toLowerCase();
    return /combobox|listbox|datepicker|treeview|filter|parameter/.test(s);
  },

  /** Убрать панель параметров и filter-элементы с холста дашборда */
  stripFilters: function (report) {
    report.requestParameters = false;
    try {
      if (report.parametersDisplayMode != null) report.parametersDisplayMode = 0;
    } catch (e) {}

    try {
      var vars = report.dictionary.variables;
      for (var i = 0; i < (vars.count || 0); i++) {
        var v = vars.getByIndex(i);
        if (!v) continue;
        v.requestFromUser = false;
        try {
          if ("showOnParametersPanel" in v) v.showOnParametersPanel = false;
        } catch (e2) {}
      }
    } catch (e) {}

    try {
      for (var p = 0; p < (report.pages.count || 0); p++) {
        var page = report.pages.getByIndex(p);
        if (!page || !page.components) continue;
        var remove = [];
        for (var c = 0; c < (page.components.count || 0); c++) {
          var comp = page.components.getByIndex(c);
          if (window.StiApp.isFilterComponent(comp)) remove.push(comp);
        }
        for (var r = 0; r < remove.length; r++) {
          try {
            page.components.remove(remove[r]);
          } catch (e3) {
            try {
              page.components.removeAt(page.components.indexOf(remove[r]));
            } catch (e4) {}
          }
        }
      }
    } catch (e) {
      console.warn("[sti] strip filter components", e);
    }
  },

  patchPg: function (report, cfg) {
    window.StiApp.stripFilters(report);
    try {
      var dbs = report.dictionary.databases;
      for (var i = 0; i < (dbs.count || 0); i++) {
        var db = dbs.getByIndex(i);
        if (db && cfg && cfg.pgConn) db.connectionString = cfg.pgConn;
      }
    } catch (e) {}
    report.onBeginProcessData = function (args) {
      console.log("[sti] BeginProcessData", args.command, args.database, args.dataSource);
      if (cfg && cfg.pgConn) args.connectionString = cfg.pgConn;
    };
  },

  buildPgDemo: function (report, cfg) {
    report.dictionary.databases.clear();
    report.dictionary.dataSources.clear();

    var db = new Stimulsoft.Report.Dictionary.StiPostgreSQLDatabase(
      "Connection",
      "Connection",
      (cfg && cfg.pgConn) || ""
    );
    report.dictionary.databases.add(db);

    var ds = new Stimulsoft.Report.Dictionary.StiPostgreSQLSource(
      "Connection",
      "Demo",
      "Demo",
      "SELECT 1 AS n, current_database()::text AS db, now()::text AS ts"
    );
    ds.columns.add(
      new Stimulsoft.Report.Dictionary.StiDataColumn("n", "n", "n", Stimulsoft.System.Int32)
    );
    ds.columns.add(
      new Stimulsoft.Report.Dictionary.StiDataColumn("db", "db", "db", Stimulsoft.System.String)
    );
    ds.columns.add(
      new Stimulsoft.Report.Dictionary.StiDataColumn("ts", "ts", "ts", Stimulsoft.System.String)
    );
    report.dictionary.dataSources.add(ds);

    var dashboard = report.pages.getByIndex(0);
    try {
      dashboard.components.clear();
    } catch (e) {}

    var tableElement = new Stimulsoft.Dashboard.Components.Table.StiTableElement();
    tableElement.left = 20;
    tableElement.top = 20;
    tableElement.width = 720;
    tableElement.height = 400;
    tableElement.name = "DemoTable";
    dashboard.components.add(tableElement);

    ["Demo.n", "Demo.db", "Demo.ts"].forEach(function (expr) {
      var col = new Stimulsoft.Dashboard.Components.Table.StiDimensionColumn();
      col.expression = expr;
      tableElement.columns.add(col);
    });
  },

  loadReport: async function (cfg) {
    var resp = await fetch("/dashboard", { cache: "no-store" });
    var json = resp.ok ? await resp.text() : "";
    var report = Stimulsoft.Report.StiReport.createNewDashboard();
    var demo = true;
    if (json && !window.StiApp.isScrubbed(json)) {
      report.load(json);
      demo = false;
    } else {
      window.StiApp.buildPgDemo(report, cfg);
    }
    window.StiApp.patchPg(report, cfg);
    return { report: report, demo: demo };
  },
};
