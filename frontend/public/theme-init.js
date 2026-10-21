(function () {
  var preference = "system";
  try { preference = localStorage.getItem("truststack-theme") || localStorage.getItem("report-reader-theme") || "system"; } catch (_) {}
  var theme = preference === "dark" || preference === "light" ? preference : (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
}());
