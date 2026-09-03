import { defineConfig, loadEnv } from "vite";

function gatewayProxy(origin) {
  const trimmed = String(origin || "").trim().replace(/\/$/, "");
  if (!trimmed) return {};

  return {
    "/v2": {
      target: trimmed,
      changeOrigin: true,
      secure: true,
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const verifySsl = String(env.GAIA_EXTERNAL_GATEWAY_VERIFY_SSL || "true").trim().toLowerCase();
  const proxy = gatewayProxy(env.GAIA_EXTERNAL_GATEWAY_ORIGIN);
  if (proxy["/v2"]) {
    proxy["/v2"].secure = !["0", "false", "no", "off"].includes(verifySsl);
  }
  return {
    server: {
      host: "127.0.0.1",
      port: 8004,
      strictPort: true,
      proxy,
    },
  };
});
