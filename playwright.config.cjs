const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/browser', workers: 1, timeout: 45000,
  use: {
    baseURL: process.env.RELAY_API_URL || 'http://127.0.0.1:8000',
    headless: true, viewport: { width: 1280, height: 900 },
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {},
    // Do not record network traces: status requests contain private bearer tokens.
    trace: 'off', screenshot: 'only-on-failure',
  },
});
