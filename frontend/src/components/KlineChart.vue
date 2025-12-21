<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import {
  createChart,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  type ISeriesApi,
} from "lightweight-charts";
import type { Kline } from "@/services/marketService";
import type { IndicatorState } from "@/types/indicator";

/* ---------- props ---------- */

const props = defineProps<{
  data: Kline[];
  indicators: IndicatorState;
}>();

/* ---------- refs ---------- */

const container = ref<HTMLDivElement | null>(null);

let chart: ReturnType<typeof createChart> | null = null;
let candle: ISeriesApi<"Candlestick"> | null = null;

let maSeries: ISeriesApi<"Line">[] = [];
let emaSeries: ISeriesApi<"Line">[] = [];

let macdHist: ISeriesApi<"Histogram"> | null = null;
let macdLine: ISeriesApi<"Line"> | null = null;
let macdSignal: ISeriesApi<"Line"> | null = null;

let rsiSeries: ISeriesApi<"Line"> | null = null;

/* ---------- utils ---------- */

const toUnix = (t: string) =>
  Math.floor(new Date(t).getTime() / 1000);

/* ---------- indicator algorithms ---------- */

function calcEMA(data: number[], period: number) {
  const k = 2 / (period + 1);
  const ema: number[] = [];
  data.forEach((v, i) => {
    if (i === 0) ema.push(v);
    else ema.push(v * k + ema[i - 1] * (1 - k));
  });
  return ema;
}

function calcMACD(closes: number[]) {
  const ema12 = calcEMA(closes, 12);
  const ema26 = calcEMA(closes, 26);
  const dif = ema12.map((v, i) => v - ema26[i]);
  const dea = calcEMA(dif, 9);
  const hist = dif.map((v, i) => v - dea[i]);
  return { dif, dea, hist };
}

function calcRSI(closes: number[], period = 14) {
  const rsi: number[] = [];
  for (let i = period; i < closes.length; i++) {
    let gain = 0;
    let loss = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const diff = closes[j] - closes[j - 1];
      diff >= 0 ? (gain += diff) : (loss -= diff);
    }
    const rs = loss === 0 ? 100 : gain / loss;
    rsi.push(100 - 100 / (1 + rs));
  }
  return rsi;
}

/* ---------- mount ---------- */

onMounted(() => {
  if (!container.value) return;

  chart = createChart(container.value, {
    height: 600,
    layout: {
      background: { color: "#0f172a" },
      textColor: "#cbd5f5",
    },
    grid: {
      vertLines: { color: "#1e293b" },
      horzLines: { color: "#1e293b" },
    },
  });

  candle = chart.addSeries(CandlestickSeries, {
    upColor: "transparent",
    borderUpColor: "#22c55e",
    wickUpColor: "#22c55e",
    downColor: "#ef4444",
    borderDownColor: "#ef4444",
    wickDownColor: "#ef4444",
  });
});

/* ---------- realtime update (latest kline) ---------- */

function updateKline(k: Kline) {
  if (!candle) return;

  candle.update({
    time: toUnix(k.open_time),
    open: k.open,
    high: k.high,
    low: k.low,
    close: k.close,
  });
}

defineExpose({ updateKline });

/* ---------- redraw on data / indicator change ---------- */

watch(
  () => [props.data, props.indicators],
  () => {
    if (!chart || !candle || props.data.length === 0) return;

    const times = props.data.map(k => toUnix(k.open_time));
    const closes = props.data.map(k => k.close);

    /* --- K线 --- */
    candle.setData(
      props.data.map(k => ({
        time: toUnix(k.open_time),
        open: k.open,
        high: k.high,
        low: k.low,
        close: k.close,
      }))
    );

    /* --- 清理旧指标 --- */
    [...maSeries, ...emaSeries].forEach(s => chart!.removeSeries(s));
    maSeries = [];
    emaSeries = [];

    if (macdHist) chart.removeSeries(macdHist);
    if (macdLine) chart.removeSeries(macdLine);
    if (macdSignal) chart.removeSeries(macdSignal);
    if (rsiSeries) chart.removeSeries(rsiSeries);

    macdHist = macdLine = macdSignal = rsiSeries = null;

    /* --- EMA（主图） --- */
    props.indicators.ema.forEach(cfg => {
      const ema = calcEMA(closes, cfg.period);
      const s = chart!.addSeries(LineSeries, {
        color: cfg.color,
        lineWidth: 1,
      });
      s.setData(
        ema.map((v, i) => ({
          time: times[i],
          value: v,
        }))
      );
      emaSeries.push(s);
    });

    /* --- MACD（副图） --- */
    if (props.indicators.macd) {
      const { dif, dea, hist } = calcMACD(closes);

      macdHist = chart.addSeries(HistogramSeries, {
        priceScaleId: "macd",
      });
      macdHist.setData(
        hist.map((v, i) => ({
          time: times[i],
          value: v,
          color: v >= 0 ? "#22c55e" : "#ef4444",
        }))
      );

      macdLine = chart.addSeries(LineSeries, {
        color: "#38bdf8",
        priceScaleId: "macd",
      });
      macdLine.setData(
        dif.map((v, i) => ({ time: times[i], value: v }))
      );

      macdSignal = chart.addSeries(LineSeries, {
        color: "#facc15",
        priceScaleId: "macd",
      });
      macdSignal.setData(
        dea.map((v, i) => ({ time: times[i], value: v }))
      );
    }

    /* --- RSI（副图） --- */
    if (props.indicators.rsi) {
      const rsi = calcRSI(closes);
      rsiSeries = chart.addSeries(LineSeries, {
        color: "#a855f7",
        priceScaleId: "rsi",
      });
      rsiSeries.setData(
        rsi.map((v, i) => ({
          time: times[i + 14],
          value: v,
        }))
      );
    }
  },
  { deep: true }
);

/* ---------- cleanup ---------- */

onBeforeUnmount(() => {
  chart?.remove();
  chart = null;
});
</script>

<template>
  <div ref="container" style="width: 100%; height: 600px" />
</template>
