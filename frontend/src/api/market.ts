import axios from "axios";

const api = axios.create({
  baseURL: "http://127.0.0.1:8000",
  timeout: 5000,
});

export function fetchKlines(limit = 200) {
  return api.get("/klines", {
    params: { limit },
  });
}

export function fetchLatestKline() {
  return api.get("/klines/latest");
}
