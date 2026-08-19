import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.CANDLEMIND_DEV_API_TARGET || "http://localhost:8000";
  const websocketTarget = apiTarget.replace(/^http/, "ws");
  return {
    plugins: [react()],
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.{js,jsx}"],
      clearMocks: true,
      restoreMocks: true,
    },
    server: {
      proxy: {
        "/api": { target: apiTarget, changeOrigin: true },
        "/ws":  { target: websocketTarget, ws: true },
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});
