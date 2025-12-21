<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from "vue";
import {
  createChart,
  CandlestickSeries,
  type ISeriesApi,
} from "lightweight-charts";
import type { Kline } from "@/services/marketService";

const props = defineProps<{
  data: Kline[];
}>();

const container = ref<HTMLDivElement | null>(null);

let chart: ReturnType<typeof createChart> | null = null;
let series: ISeriesApi<"Candlestick"> | null = null;

/**
 * 把后端返回的时间字符串
 * 转换为 Unix 秒时间戳（lightweight-charts 标准）
 */
function toUnixTime(timeStr: string): number {
  return Math.floor(new Date(timeStr).getTime() / 1000);
}

onMounted(() => {
  if (!container.value) return;

  chart = createChart(container.value, {
    height: 400,
    layout: {
      background: { color: "#ffffff" },
      textColor: "#000000",
    },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
    },
  });

  // ✅ 新版 API：addSeries
  series = chart.addSeries(CandlestickSeries);

  // 设置历史数据（如果有）
  if (props.data.length > 0) {
    series.setData(
      props.data.map(k => ({
        time: toUnixTime(k.open_time),
        open: k.open,
        high: k.high,
        low: k.low,
        close: k.close,
      }))
    );
  }
});

/**
 * 实时更新最新一根 K 线
 */
function updateKline(k: Kline) {
  if (!series) return;

  series.update({
    time: toUnixTime(k.open_time),
    open: k.open,
    high: k.high,
    low: k.low,
    close: k.close,
  });
}

defineExpose({ updateKline });

onBeforeUnmount(() => {
  chart?.remove();
  chart = null;
  series = null;
});
</script>

<template>
  <div ref="container" style="width: 100%; height: 400px;" />
</template>
