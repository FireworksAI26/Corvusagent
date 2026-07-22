import { proxyV2Request, validatedOrigin } from "./v2/[...path].js";

declare const process: { env: Record<string, string | undefined> };

const REWRITE_PARAMETER = "corvusPath";
const PROXY_UNAVAILABLE_STATUS = 503;
const PROXY_UNAVAILABLE_CODE = "platform_proxy_unavailable";

function proxyUnavailableResponse(): Response {
  return Response.json(
    { detail: { code: PROXY_UNAVAILABLE_CODE } },
    { status: PROXY_UNAVAILABLE_STATUS },
  );
}

function safeCapturedPath(path: string): boolean {
  if (path === "" || path.startsWith("/") || /%(?:2e|2f|5c)/i.test(path)) return false;
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return false;
  }
  return !decoded.includes("\\")
    && !decoded.includes("//")
    && decoded.split("/").every((segment) => segment !== "." && segment !== "..");
}

async function rewrittenRequest(request: Request): Promise<Request | null> {
  const source = new URL(request.url);
  const capturedPath = source.searchParams.get(REWRITE_PARAMETER);
  if (capturedPath === null || !safeCapturedPath(capturedPath)) return null;
  source.searchParams.delete(REWRITE_PARAMETER);
  source.pathname = `/api/v2/${capturedPath}`;
  const body = request.method === "GET" || request.method === "HEAD"
    ? undefined
    : await request.arrayBuffer();
  return new Request(source, {
    method: request.method,
    headers: request.headers,
    body: body?.byteLength === 0 ? undefined : body,
    redirect: request.redirect,
  });
}

export async function proxyRewrittenV2Request(
  request: Request,
  configuredOrigin: string | undefined,
  fetchImpl: typeof fetch = fetch,
  deploymentEnvironment: string | undefined = process.env.VERCEL_ENV,
): Promise<Response> {
  if (validatedOrigin(configuredOrigin, deploymentEnvironment) === null) {
    return proxyUnavailableResponse();
  }
  const rewritten = await rewrittenRequest(request);
  if (rewritten === null) {
    return Response.json(
      { detail: { code: "platform_proxy_path_invalid" } },
      { status: 400 },
    );
  }
  return proxyV2Request(rewritten, configuredOrigin, fetchImpl, deploymentEnvironment);
}

export default {
  fetch(request: Request): Promise<Response> {
    return proxyRewrittenV2Request(
      request,
      process.env.CORVUS_RAILWAY_ORIGIN,
      fetch,
      process.env.VERCEL_ENV,
    );
  },
};
