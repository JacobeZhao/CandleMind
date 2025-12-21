<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from "vue";
import { marketService, type Kline } from "@/services/marketService";
import KlineChart from "@/components/KlineChart.vue";

const klines = ref<Kline[]>([]);
const chartRef = ref<InstanceType<typeof KlineChart>>();

onMounted(async () => {
  klines.value = await marketService.loadHistory();

  marketService.startRealtime(k => {
    chartRef.value?.updateKline(k);
  });
});

onBeforeUnmount(() => {
  marketService.stopRealtime();
});
</script>

<template>
  <div>
    <h2>BTCUSDT 实时 K 线</h2>
    <KlineChart ref="chartRef" :data="klines" />
  </div>
</template>
