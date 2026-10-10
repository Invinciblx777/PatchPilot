import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// The build is served by patchpilot-ui from inside the Python package.
const outDir = fileURLToPath(new URL("../src/patchpilot/web/static", import.meta.url));

export default defineConfig({
  build: {
    outDir,
    emptyOutDir: true,
    // Fonts and images stay files: the CSP allows font-src 'self' only.
    assetsInlineLimit: 0,
    sourcemap: false,
    target: "es2022",
  },
});
