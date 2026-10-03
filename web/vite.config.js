import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Forward API calls to the FastAPI backend so the browser sees one origin
    proxy: { "/agent": "http://localhost:8000" },
  },
});
