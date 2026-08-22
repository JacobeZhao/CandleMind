import { describe, expect, it } from "vitest";
import { isRequestCancelled, normalizeApiError } from "./errors";

describe("normalizeApiError", () => {
  it("reads structured FastAPI details", () => {
    expect(normalizeApiError({
      response: { status: 503, data: { detail: { code: "upstream_timeout", message: "行情源超时" } } },
    })).toMatchObject({ code: "upstream_timeout", message: "行情源超时", retryable: true });
    expect(normalizeApiError({
      response: { status: 400, data: { error: { code: "bad_scope", message: "范围无效" } } },
    })).toMatchObject({ code: "bad_scope", message: "范围无效", retryable: false });
  });

  it("prefers the backend retryable contract over HTTP status heuristics", () => {
    expect(normalizeApiError({
      response: { status: 503, data: { detail: { message: "尚未连接 Binance", retryable: false } } },
    }).retryable).toBe(false);
    expect(normalizeApiError({
      response: { status: 409, data: { detail: { message: "账户绑定切换中", retryable: true } } },
    }).retryable).toBe(true);
  });

  it("describes every plausible -2015 cause without blaming the browser IP", () => {
    const result = normalizeApiError({
      response: { status: 401, data: { code: -2015, msg: "Invalid API-key, IP, or permissions" } },
    });
    expect(result.message).toContain("API Key 无效");
    expect(result.message).toContain("合约交易权限");
    expect(result.message).toContain("后端服务器出口 IP");
    expect(result.retryable).toBe(false);
  });

  it("normalizes validation arrays and cancellation", () => {
    expect(normalizeApiError({ response: { status: 422, data: { detail: [{ msg: "品种无效" }] } } }).message).toBe("品种无效");
    const cancelled = { code: "ERR_CANCELED", name: "CanceledError" };
    expect(isRequestCancelled(cancelled)).toBe(true);
    expect(normalizeApiError(cancelled).cancelled).toBe(true);
  });
});
