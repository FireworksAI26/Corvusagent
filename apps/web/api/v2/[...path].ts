const API_PREFIX = "/api/v2";
const GOOGLE_AUTHORIZATION_URL = new URL("https://accounts.google.com/o/oauth2/v2/auth");
const RAILWAY_HOST_SUFFIX = ".up.railway.app";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);
const LOOPBACK_ENVIRONMENTS = new Set(["development", "test"]);
const ALLOWED_METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]);
const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "cookie",
  "idempotency-key",
  "x-csrf-token",
] as const;
const FORWARDED_RESPONSE_HEADERS = [
  "cache-control",
  "content-language",
  "content-type",
  "expires",
  "pragma",
  "vary",
] as const;
const BODY_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

declare const process: { env: Record<string, string | undefined> };

function errorResponse(status: number, code: string): Response {
  return Response.json({ detail: { code } }, { status });
}

export function validatedOrigin(
  configuredOrigin: string | undefined,
  deploymentEnvironment: string | undefined,
): URL | null {
  if (configuredOrigin === undefined || configuredOrigin.trim() === "") return null;
  let parsed: URL;
  try {
    parsed = new URL(configuredOrigin);
  } catch {
    return null;
  }
  const railwayHost =
    parsed.hostname.endsWith(RAILWAY_HOST_SUFFIX) &&
    parsed.hostname.length > RAILWAY_HOST_SUFFIX.length;
  const secureRailway = parsed.protocol === "https:" && parsed.port === "" && railwayHost;
  const loopback =
    LOOPBACK_ENVIRONMENTS.has(deploymentEnvironment ?? "") &&
    parsed.protocol === "http:" &&
    LOOPBACK_HOSTS.has(parsed.hostname);
  if (
    (!secureRailway && !loopback) ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.pathname !== "/" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    return null;
  }
  return parsed;
}

function safeRedirectLocation(location: string): boolean {
  if (/[\u0000-\u001f\u007f]/.test(location) || location.includes("\\")) return false;
  if (location.startsWith("/")) {
    return !location.startsWith("//") && !location.includes("#");
  }
  let parsed: URL;
  try {
    parsed = new URL(location);
  } catch {
    return false;
  }
  return (
    parsed.origin === GOOGLE_AUTHORIZATION_URL.origin &&
    parsed.pathname === GOOGLE_AUTHORIZATION_URL.pathname &&
    parsed.username === "" &&
    parsed.password === "" &&
    parsed.hash === ""
  );
}

function safeApiPath(requestUrl: URL): string | null {
  const path = requestUrl.pathname;
  if (path !== API_PREFIX && !path.startsWith(`${API_PREFIX}/`)) return null;
  if (/%(?:2e|2f|5c)/i.test(path) || path.includes("\\")) return null;
  const captured = path.slice(API_PREFIX.length);
  if (captured.includes("//")) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(captured);
  } catch {
    return null;
  }
  if (
    decoded.includes("\\") ||
    decoded.includes("//") ||
    decoded.split("/").some((segment) => segment === "." || segment === "..")
  ) {
    return null;
  }
  return `${API_PREFIX}${captured}`;
}

function requestHeaders(request: Request, trustedBrowserOrigin: string | null): Headers {
  const forwarded = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) forwarded.set(name, value);
  }
  if (trustedBrowserOrigin !== null) forwarded.set("origin", trustedBrowserOrigin);
  return forwarded;
}

function publicCookie(cookie: string, requestUrl: URL): string | null {
  if (/[\u0000-\u001f\u007f]/.test(cookie)) return null;
  const segments = cookie.split(";").map((segment) => segment.trim()).filter(Boolean);
  const nameValue = segments.shift();
  if (nameValue === undefined || !nameValue.includes("=")) return null;
  const attributes = segments.filter((attribute) => {
    const name = attribute.split("=", 1)[0].trim().toLowerCase();
    return name !== "domain" && name !== "path" && name !== "secure";
  });
  attributes.push("Path=/");
  if (requestUrl.protocol === "https:") attributes.push("Secure");
  return [nameValue, ...attributes].join("; ");
}

function responseHeaders(upstream: Response, requestUrl: URL): Headers {
  const forwarded = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) forwarded.set(name, value);
  }
  const location = upstream.headers.get("location");
  if (
    location !== null &&
    safeRedirectLocation(location)
  ) {
    forwarded.set("location", location);
  }
  const cookieHeaders = upstream.headers as Headers & { getSetCookie?: () => string[] };
  const cookies = cookieHeaders.getSetCookie?.() ?? [];
  if (cookies.length > 0) {
    for (const cookie of cookies) {
      const rewritten = publicCookie(cookie, requestUrl);
      if (rewritten !== null) forwarded.append("set-cookie", rewritten);
    }
  } else {
    const cookie = upstream.headers.get("set-cookie");
    const rewritten = cookie === null ? null : publicCookie(cookie, requestUrl);
    if (rewritten !== null) forwarded.append("set-cookie", rewritten);
  }
  return forwarded;
}

export async function proxyV2Request(
  request: Request,
  configuredOrigin: string | undefined,
  fetchImpl: typeof fetch = fetch,
  deploymentEnvironment: string | undefined = process.env.VERCEL_ENV,
): Promise<Response> {
  const origin = validatedOrigin(configuredOrigin, deploymentEnvironment);
  if (origin === null) return errorResponse(503, "platform_proxy_unavailable");
  if (!ALLOWED_METHODS.has(request.method)) {
    return errorResponse(405, "platform_proxy_method_forbidden");
  }
  const requestUrl = new URL(request.url);
  const apiPath = safeApiPath(requestUrl);
  if (apiPath === null) return errorResponse(400, "platform_proxy_path_invalid");
  const browserOrigin = request.headers.get("origin");
  if (browserOrigin !== null && browserOrigin !== requestUrl.origin) {
    return errorResponse(403, "platform_proxy_origin_forbidden");
  }
  const target = `${origin.origin}${apiPath}${requestUrl.search}`;
  const body = BODY_METHODS.has(request.method) ? await request.arrayBuffer() : undefined;
  let upstream: Response;
  try {
    upstream = await fetchImpl(target, {
      method: request.method,
      headers: requestHeaders(request, browserOrigin === null ? null : requestUrl.origin),
      body: body?.byteLength === 0 ? undefined : body,
      redirect: "manual",
    });
  } catch {
    return errorResponse(503, "platform_proxy_unavailable");
  }
  const bodyForbidden =
    request.method === "HEAD" || [204, 205, 304].includes(upstream.status);
  return new Response(bodyForbidden ? null : upstream.body, {
    status: upstream.status,
    headers: responseHeaders(upstream, requestUrl),
  });
}

export default {
  fetch(request: Request): Promise<Response> {
    return proxyV2Request(
      request,
      process.env.CORVUS_RAILWAY_ORIGIN,
      fetch,
      process.env.VERCEL_ENV,
    );
  },
};
