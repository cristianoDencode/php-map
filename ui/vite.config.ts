import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` fala com o `php-map ui` (porta padrão 8765) pelo proxy do /graphql.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/graphql": { target: "http://127.0.0.1:8765", changeOrigin: false },
      "/auth": { target: "http://127.0.0.1:8765", changeOrigin: false },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 900 },
});
