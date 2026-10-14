import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The FastAPI backend runs on :8000 in development.
    proxy: { "/api": "http://localhost:8000" },
  },
  test: { environment: "node" },
});
