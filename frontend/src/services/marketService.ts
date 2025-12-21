import { fetchKlines, fetchLatestKline } from "@/api/market";

export interface Kline {
  open_time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

class MarketService {
  private timer: number | null = null;

  async loadHistory(): Promise<Kline[]> {
    const res = await fetchKlines(200);
    return res.data;
  }

  startRealtime(onUpdate: (k: Kline) => void) {
    this.stopRealtime();

    this.timer = window.setInterval(async () => {
      const res = await fetchLatestKline();
      if (res.data) {
        onUpdate(res.data);
      }
    }, 1000);
  }

  stopRealtime() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

export const marketService = new MarketService();
