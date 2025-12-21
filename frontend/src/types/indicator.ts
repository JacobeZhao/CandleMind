export interface MAConfig {
  period: number;
  color: string;
}

export interface EMAConfig {
  period: number;
  color: string;
}

export interface IndicatorState {
  ma: MAConfig[];
  ema: EMAConfig[];
  macd: boolean;
  rsi: boolean;
}
