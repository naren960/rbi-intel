const UA =
  "rbi-intel/2.0 (regulatory research indexer; contact via repository) Mozilla/5.0 (compatible)";

export interface FetchOpts {
  retries?: number;
  timeoutMs?: number;
  method?: "GET" | "POST";
  body?: string;
  headers?: Record<string, string>;
}

/**
 * Polite fetch with exponential backoff on 429/5xx.
 * Adds jitter so parallel workers do not retry in lockstep.
 */
export async function politeFetch(url: string, opts: FetchOpts = {}): Promise<Response> {
  const retries = opts.retries ?? 2;
  const timeoutMs = opts.timeoutMs ?? 30_000;
  let lastErr: unknown;

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, {
        method: opts.method ?? "GET",
        headers: {
          "User-Agent": UA,
          Accept: "text/html,application/xhtml+xml,application/pdf,*/*",
          "Accept-Language": "en-IN,en;q=0.9",
          ...(opts.headers ?? {}),
        },
        body: opts.body,
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (res.ok) return res;
      if (res.status === 429 || res.status >= 500) {
        await sleep(backoff(attempt));
        continue;
      }
      throw new Error(`HTTP ${res.status} ${res.statusText} for ${url}`);
    } catch (e) {
      lastErr = e;
      if (attempt === retries) break;
      await sleep(backoff(attempt));
    }
  }
  throw new Error(
    `Failed after ${retries + 1} attempts: ${url} — ${lastErr instanceof Error ? lastErr.message : String(lastErr)}`
  );
}

function backoff(attempt: number): number {
  return 1000 * Math.pow(2, attempt) + Math.random() * 400;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
