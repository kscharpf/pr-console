import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The CSP is `default-src 'self'`: everything is bundled into /assets, nothing inline.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
  },
});
