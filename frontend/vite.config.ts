import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // In development the API runs on :8000; proxying keeps cookies same-origin.
    proxy: { "/api": "http://localhost:8000" },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: { vendor: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"], charts: ["recharts"] },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
