const hasProxy = Boolean(
  process.env.HTTP_PROXY || process.env.HTTPS_PROXY || process.env.http_proxy || process.env.https_proxy
);

if (hasProxy) {
  try {
    const { EnvHttpProxyAgent, setGlobalDispatcher } = await import(
      "file:///usr/app/output/server/node_modules/undici/index.js"
    );
    setGlobalDispatcher(new EnvHttpProxyAgent());
  } catch (err) {
    console.warn("[newsnow-proxy-preload] skipped:", err instanceof Error ? err.message : String(err));
  }
}
