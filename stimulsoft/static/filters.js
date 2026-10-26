function conditionName(value) {
  if (typeof value === "string") return value;
  var engine = window.Stimulsoft && Stimulsoft.Data && Stimulsoft.Data.Engine;
  var names = engine && engine.StiDataFilterCondition;
  if (names && typeof names[value] === "string") return names[value];
  return value == null ? "" : String(value);
}

function asArray(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  if (Array.isArray(value.list)) return value.list;
  if (typeof value.length === "number") {
    var items = [];
    for (var i = 0; i < value.length; i += 1) {
      items.push(typeof value.getByIndex === "function" ? value.getByIndex(i) : value[i]);
    }
    return items;
  }
  return [];
}

function textOf(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (typeof value.toString === "function" && value.toString !== Object.prototype.toString) {
    return String(value.toString());
  }
  return "";
}

function collectDashboardFilters(report) {
  var filters = [];
  asArray(report && report.pages).forEach(function (page) {
    asArray(page.components || page.getComponents && page.getComponents()).forEach(function (element) {
      asArray(element.userFilters).forEach(function (item) {
        var value = textOf(item.value);
        var value2 = textOf(item.value2);
        var condition = conditionName(item.condition);
        if (!value && !value2) return;
        if (condition === "Any") return;
        filters.push({
          element: textOf(element.name),
          path: textOf(item.path),
          condition: condition,
          value: value,
          value2: value2
        });
      });
    });
  });
  return filters;
}

function currentReport(args) {
  if (args && args.report && args.report.pages) return args.report;
  var root = typeof viewer !== "undefined" ? viewer : null;
  var nested = root && root.jsObject && root.jsObject.viewer;
  if (nested && nested.report) return nested.report;
  if (root && root.report) return root.report;
  return null;
}

function publishDashboardFilters(args) {
  if (args && args.action === "DashboardResetAllFilters") {
    window.parent.postMessage({ type: "dashboard-filters", filters: [] }, window.location.origin);
  }
  setTimeout(function () {
    window.parent.postMessage(
      { type: "dashboard-filters", filters: collectDashboardFilters(currentReport(args)) },
      window.location.origin
    );
  }, 500);
}
