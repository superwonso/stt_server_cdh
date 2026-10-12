import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  timeout: 60000,
  use: {
    baseURL: process.env.STUDY_TEST_URL || 'http://127.0.0.1:3218',
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
    launchOptions: process.env.STUDY_BROWSER_EXECUTABLE
      ? { executablePath: process.env.STUDY_BROWSER_EXECUTABLE }
      : process.platform === 'win32'
        ? { executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' }
        : {},
  },
  reporter: [['list'], ['json', { outputFile: 'test-results/e2e.json' }]],
  webServer: process.env.STUDY_TEST_URL
    ? undefined
    : {
        command: 'node scripts/serve.mjs',
        url: 'http://127.0.0.1:3218/__study-test/',
        reuseExistingServer: true,
      },
});
