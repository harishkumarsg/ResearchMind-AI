import { defineConfig } from "vitest/config";
import path from "node:path";

// Deliberately a separate config from vite.config.ts, which wraps a
// custom defineConfig (@lovable.dev/vite-tanstack-config) for the
// TanStack Start/Nitro build — adding a `test` block there risks being
// silently dropped or interacting with that wrapper in an unverified way.
// Vitest supports its own standalone config file, so this avoids touching
// the build pipeline at all.
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
