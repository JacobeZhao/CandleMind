const CANCEL_CODES = new Set(["ERR_CANCELED", "ECONNABORTED"]);

function firstString(...values) {
  return values.find((value) => typeof value === "string" && value.trim())?.trim() || "";
}

function detailMessage(detail) {
  if (typeof detail === "string") return detail.trim();
  if (Array.isArray(detail)) {
    return detail
      .map((item) => firstString(item?.msg, item?.message, typeof item === "string" ? item : ""))
      .filter(Boolean)
      .join("；");
  }
  return firstString(detail?.message, detail?.msg, detail?.error);
}

export function isRequestCancelled(error) {
  return error?.name === "CanceledError"
    || error?.name === "AbortError"
    || error?.code === "ERR_CANCELED";
}

export function normalizeApiError(error, fallback = "请求失败，请稍后重试。") {
  if (error?.normalizedApiError) return error;

  const cancelled = isRequestCancelled(error);
  const data = error?.response?.data;
  const detail = data?.detail;
  const nestedError = typeof detail?.error === "object" ? detail.error : data?.error;
  const explicitRetryable = typeof detail?.retryable === "boolean"
    ? detail.retryable
    : typeof nestedError?.retryable === "boolean"
      ? nestedError.retryable
      : typeof data?.retryable === "boolean" ? data.retryable : null;
  const status = Number(error?.response?.status) || null;
  const code = detail?.code ?? nestedError?.code ?? data?.code ?? error?.code ?? null;
  const rawMessage = firstString(
    detailMessage(detail),
    nestedError?.message,
    nestedError?.msg,
    typeof data === "string" ? data : "",
    data?.message,
    data?.msg,
    error?.message,
  );
  const isBinance2015 = Number(code) === -2015
    || /(?:^|\D)-2015(?:\D|$)|invalid api-key|api key, futures permission, or ip allowlist/i.test(rawMessage);
  const isAuthenticationFailure = isBinance2015
    || /鉴权|认证|api.?key|permission|权限|allowlist|白名单/i.test(rawMessage);
  const message = cancelled
    ? ""
    : isBinance2015
      ? "Binance 拒绝了请求（-2015）：可能是 API Key 无效、缺少合约交易权限，或后端服务器出口 IP 未加入白名单。"
      : rawMessage || fallback;
  const retryable = !cancelled && !isAuthenticationFailure && (
    explicitRetryable ?? (
      status == null
      || status === 408
      || status === 429
      || status >= 500
      || CANCEL_CODES.has(error?.code)
    )
  );

  return {
    normalizedApiError: true,
    cancelled,
    code,
    status,
    retryable,
    message,
  };
}
