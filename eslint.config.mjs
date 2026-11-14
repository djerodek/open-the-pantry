// CI's undefined-name check for the page scripts (0190): a name used but
// never defined or imported fails the build. Only no-undef, on purpose: a
// move between modules that forgets an import passes every test that
// doesn't click the one button using it (runInboxScan, review of
// 0185-0189). Run: npm install --no-save eslint@9.14.0 globals@15.12.0 &&
// npx eslint
import globals from "globals";

export default [
  {
    // The main page's modules (index.html, type="module").
    files: ["frontend/js/app.js", "frontend/js/settings.js", "frontend/js/add_recipe.js"],
    languageOptions: { ecmaVersion: "latest", sourceType: "module", globals: { ...globals.browser } },
    rules: { "no-undef": "error" },
  },
  {
    // Plain scripts, sharing through window.OTP_* (and the service worker).
    files: ["frontend/js/i18n.js", "frontend/js/weights.js", "frontend/js/amounts.js",
            "frontend/service-worker.js", "frontend-picker/*.js"],
    languageOptions: { ecmaVersion: "latest", sourceType: "script",
                       globals: { ...globals.browser, ...globals.serviceworker } },
    rules: { "no-undef": "error" },
  },
];
