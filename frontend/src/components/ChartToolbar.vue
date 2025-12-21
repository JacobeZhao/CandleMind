<script setup lang="ts">
import type { IndicatorState } from "@/types/indicator";

const props = defineProps<{
  symbol: string;
  interval: string;
  indicators: IndicatorState;
}>();

const emit = defineEmits<{
  (e: "update:symbol", v: string): void;
  (e: "update:interval", v: string): void;
  (e: "update:indicators", v: IndicatorState): void;
}>();

const symbols = ["BTCUSDT", "ETHUSDT"];
const intervals = ["1m", "5m", "15m", "1h", "1d"];
</script>

<template>
  <div class="toolbar">
    <!-- 品种 -->
    <select :value="symbol" @change="emit('update:symbol', $event.target.value)">
      <option v-for="s in symbols" :key="s">{{ s }}</option>
    </select>

    <!-- 周期 -->
    <select :value="interval" @change="emit('update:interval', $event.target.value)">
      <option v-for="i in intervals" :key="i">{{ i }}</option>
    </select>

    <!-- 指标 -->
    <label>
      <input type="checkbox" v-model="indicators.macd" />
      MACD
    </label>
    <label>
      <input type="checkbox" v-model="indicators.rsi" />
      RSI
    </label>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  padding: 8px;
  background: #111;
  color: #eee;
}
select {
  background: #222;
  color: #eee;
}
</style>
