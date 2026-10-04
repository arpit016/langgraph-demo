import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Forward API calls to the FastAPI backend so the browser sees one origin
    proxy: Object.fromEntries(
      ["/agent", "/employees", "/conversations", "/health"].map((p) => [p, "http://localhost:8000"])
    ),
  },
});
