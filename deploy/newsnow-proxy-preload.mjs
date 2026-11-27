const DOMESTIC_NO_PROXY = [
  ".cn",
  "wallstcn.com",
  "wallstreetcn.com",
  "awtmt.com",
  "zhihu.com",
  "zhimg.com",
  "weibo.com",
  "ithome.com",
  "36kr.com",
  "sspai.com",
  "bilibili.com",
  "douban.com",
  "doubanio.com",
  "toutiao.com",
  "toutiaoimg.com",
  "aihot.news",
  "baidu.com",
  "qq.com",
  "163.com",
  "ifeng.com",
  "xueqiu.com",
  "gelonghui.com",
];

const existingNoProxy = (process.env.NO_PROXY || process.env.no_proxy || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
const mergedNoProxy = Array.from(new Set([...existingNoProxy, ...DOMESTIC_NO_PROXY])).join(",");
process.env.NO_PROXY = mergedNoProxy;
process.env.no_proxy = mergedNoProxy;

// Rewrite decommissioned api-one.wallstcn.com -> api-one-wscn.awtmt.com & ensure Douban Referer
const origFetch = globalThis.fetch;
if (typeof origFetch === "function") {
  globalThis.fetch = function patchedFetch(input, init) {
    let urlStr = typeof input === "string" ? input : input instanceof URL ? input.toString() : "";
    if (typeof input === "string" && input.includes("api-one.wallstcn.com")) {
      input = input.replaceAll("api-one.wallstcn.com", "api-one-wscn.awtmt.com");
      urlStr = input;
    } else if (input instanceof URL && input.hostname === "api-one.wallstcn.com") {
      const next = new URL(input.toString());
      next.hostname = "api-one-wscn.awtmt.com";
      input = next;
      urlStr = next.toString();
    }
    if (urlStr.includes("m.douban.com/rexxar/")) {
      const nextHeaders = new Headers(init?.headers || {});
      if (!nextHeaders.has("Referer")) {
        nextHeaders.set("Referer", "https://m.douban.com/movie/");
      }
      init = { ...init, headers: nextHeaders };
    }
    return origFetch.call(this, input, init);
  };
}

const hasProxy = Boolean(
  process.env.HTTP_PROXY ||
    process.env.HTTPS_PROXY ||
    process.env.http_proxy ||
    process.env.https_proxy,
);

if (hasProxy) {
  try {
    const { EnvHttpProxyAgent, setGlobalDispatcher } = await import(
      "file:///usr/app/output/server/node_modules/undici/index.js"
    );
    setGlobalDispatcher(new EnvHttpProxyAgent());
  } catch (err) {
    console.warn(
      "[newsnow-proxy-preload] skipped:",
      err instanceof Error ? err.message : String(err),
    );
  }
}
