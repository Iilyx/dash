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

  /** Только отключить Request Parameters — холст НЕ трогаем */
  disableParameterPrompt: function (report) {
    report.requestParameters = false;
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
  },

  patchPg: function (report, cfg) {
    window.StiApp.disableParameterPrompt(report);
    try {
      var dbs = report.dictionary.databases;
      for (var i = 0; i < (dbs.count || 0); i++) {
        var db = dbs.getByIndex(i);
        if (db && cfg && cfg.pgConn) db.connectionString = cfg.pgConn;
      }
    } catch (e) {}
    report.onBeginProcessData = function (args) {
      console.log("[sti]", args.command, args.database, args.dataSource || "");
      if (cfg && cfg.pgConn) args.connectionString = cfg.pgConn;
    };
  },

  stats: function (report) {
    var pages = 0;
    var comps = 0;
    var dbs = 0;
    var dss = 0;
    try {
      pages = report.pages.count || 0;
      for (var i = 0; i < pages; i++) {
        var p = report.pages.getByIndex(i);
        comps += (p && p.components && p.components.count) || 0;
      }
    } catch (e) {}
    try {
      dbs = report.dictionary.databases.count || 0;
    } catch (e) {}
    try {
      dss = report.dictionary.dataSources.count || 0;
    } catch (e) {}
    return { pages: pages, comps: comps, dbs: dbs, dss: dss };
  },

  /** Demo без Postgres: JSON → таблица (чтобы viewer точно не был пустым) */
  buildJsonDemo: function (report) {
    try {
      report.dictionary.databases.clear();
      report.dictionary.dataSources.clear();
    } catch (e) {}

    var dataSet = new Stimulsoft.System.Data.DataSet("Demo");
    dataSet.readJson({
      Demo: [
        { n: 1, db: "json-demo", ts: new Date().toISOString() },
        { n: 2, db: "json-demo", ts: new Date().toISOString() },
      ],
    });
    report.regData("Demo", "Demo", dataSet);
    report.dictionary.synchronize();

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
    if (!resp.ok) throw new Error("/dashboard HTTP " + resp.status);
    var json = await resp.text();
    var report = Stimulsoft.Report.StiReport.createNewDashboard();
    var mode = "demo-json";

    if (json && !window.StiApp.isScrubbed(json)) {
      report.load(json);
      mode = "mrt";
      window.StiApp.patchPg(report, cfg);
    } else {
      window.StiApp.buildJsonDemo(report);
      window.StiApp.disableParameterPrompt(report);
    }

    return { report: report, mode: mode, stats: window.StiApp.stats(report) };
  },
};
