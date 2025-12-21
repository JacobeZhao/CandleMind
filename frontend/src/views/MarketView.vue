<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from "vue";
import { marketService, type Kline } from "@/services/marketService";
import KlineChart from "@/components/KlineChart.vue";
import ChartToolbar from "@/components/ChartToolbar.vue";
import type { IndicatorState } from "@/types/indicator";

const symbol = ref("btcusdt");
const interval = ref("1m");

const indicators = ref<IndicatorState>({
  ma: [{ period: 20, color: "#38bdf8" }],
  ema: [{ period: 12, color: "#a855f7" }],
  macd: true,
  rsi: true,
});

const klines = ref<Kline[]>([]);
const loading = ref(false);

let timer: number | null = null;

/** 启动实时轮询 latest */
function startRealtime() {
  stopRealtime();

  timer = window.setInterval(async () => {
    const k = await marketService.loadLatest();
    if (k) {
      chartRef.value?.updateKline(k);
    }
  }, 1000);
}

function stopRealtime() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

const chartRef = ref<InstanceType<typeof KlineChart>>();

async function reloadAll() {
  loading.value = true;
  stopRealtime();

  // 1️⃣ 通知后端切换行情源
  await marketService.switch(symbol.value, interval.value);

  // 2️⃣ 拉取 10000 根历史 K 线
  klines.value = await marketService.loadHistory(10000);

  loading.value = false;

  // 3️⃣ 开始实时更新当前 K 线
  startRealtime();
}

// 切换品种 / 周期 → 全流程重载
watch([symbol, interval], reloadAll, { immediate: true });

onBeforeUnmount(stopRealtime);
</script>

<template>
  <ChartToolbar
    :symbol="symbol"
    :interval="interval"
    :indicators="indicators"
    @update:symbol="symbol = $event"
    @update:interval="interval = $event"
    @update:indicators="indicators = $event"
  />

  <div v-if="loading" style="padding: 12px; color: #888">
    正在加载 10000 根历史 K 线…
  </div>

  <KlineChart
    v-else
    ref="chartRef"
    :data="klines"
    :indicators="indicators"
  />
</template>
