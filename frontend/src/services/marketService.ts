import axios from "axios";

const api = axios.create({
  baseURL: "http://127.0.0.1:8000",
});

export interface Kline {
  open_time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

class MarketService {
  /** 切换后端行情源 */
  async switch(symbol: string, interval: string) {
    await api.post("/klines/switch", { symbol, interval });
  }

  /** 拉取历史已收盘 K 线 */
  async loadHistory(limit = 10000): Promise<Kline[]> {
    const res = await api.get("/klines", {
      params: { limit },
    });
    return res.data;
  }

  /** 获取当前最新 K 线（可能未收盘） */
  async loadLatest(): Promise<Kline | null> {
    const res = await api.get("/klines/latest");
    return res.data;
  }
}

export const marketService = new MarketService();
