import { expect, test } from "bun:test";
import type { TokenBreakdown } from "./models";
import { calculateCost, claudePricingKey, DEFAULT_PRICING, normalizeModelName, resetPricingCache, tierMultiplier } from "./pricing";
import { contextWindowForModel, freshSessionSavingsUsd } from "./quality";
import {
  GENERATED_GEMINI_LONG_CONTEXT_PRICING,
  GENERATED_OPENAI_LONG_CONTEXT_PRICING,
  GENERATED_PRICING,
} from "./prices.generated";

const BASE_TOKENS: TokenBreakdown = {
  input: 50_000,
  output: 50_000,
  cacheRead: 50_000,
  cacheWrite: 50_000,
};

const LONG_CONTEXT_TOKENS: TokenBreakdown = {
  input: 1_000_000,
  output: 1_000_000,
  cacheRead: 1_000_000,
  cacheWrite: 1_000_000,
};

// Expected rates come from the auto-refreshed table, so a real price change
// (promo ending, new card) never breaks this test; it checks that the engine
// applies those rates correctly, including the per-request long-context tier.
const MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"];
const cost = (t: TokenBreakdown, r: { input: number; output: number; cacheRead: number; cacheWrite: number }) =>
  t.input * r.input + t.output * r.output + t.cacheRead * r.cacheRead + t.cacheWrite * r.cacheWrite;

test("GPT-5.6 aliases normalize to the documented canonical IDs", () => {
  expect(normalizeModelName("gpt-5.6")).toBe("gpt-5.6-sol");
  expect(normalizeModelName("openrouter/openai/gpt-5.6-sol-2026-07-09")).toBe("gpt-5.6-sol");
  expect(normalizeModelName("GPT-5.6 Sol Pro")).toBe("gpt-5.6-sol");
  expect(normalizeModelName("openai:gpt-5.6-terra-2026-07-09")).toBe("gpt-5.6-terra");
  expect(normalizeModelName("gpt-5.6_luna")).toBe("gpt-5.6-luna");
});

test("GPT-5.6 pricing applies base and long-context API-equivalent rates", () => {
  resetPricingCache();
  for (const model of MODELS) {
    const base = GENERATED_PRICING[model];
    const long = GENERATED_OPENAI_LONG_CONTEXT_PRICING[model];
    expect(base).toBeDefined();
    expect(long).toBeDefined();
    expect(DEFAULT_PRICING[model]).toEqual(base);
    expect(long.input).toBeGreaterThan(base.input);
    expect(calculateCost(BASE_TOKENS, model, "/tmp/token-optimizer-gpt56-no-config")).toBeCloseTo(cost(BASE_TOKENS, base), 6);
    expect(calculateCost(LONG_CONTEXT_TOKENS, model, "/tmp/token-optimizer-gpt56-no-config")).toBeCloseTo(cost(LONG_CONTEXT_TOKENS, long), 6);
  }
});

test("GPT-5.6 feeds OpenClaw savings and context-window helpers", () => {
  expect(freshSessionSavingsUsd(1_000_000, "GPT-5.6 Sol Pro")).toBeCloseTo(GENERATED_PRICING["gpt-5.6-sol"].input * 1e6);
  expect(freshSessionSavingsUsd(1_000_000, "openai:gpt-5.6-terra-2026-07-09")).toBeCloseTo(GENERATED_PRICING["gpt-5.6-terra"].input * 1e6);
  expect(freshSessionSavingsUsd(1_000_000, "gpt-5.6_luna")).toBeCloseTo(GENERATED_PRICING["gpt-5.6-luna"].input * 1e6);
  expect(contextWindowForModel("gpt-5.6")).toBe(1_050_000);
  expect(contextWindowForModel("openrouter/openai/gpt-5.6-terra-2026-07-09")).toBe(1_050_000);
});

test("vertex-regional surcharge applies to every Claude card, including generated generations", () => {
    for (const key of ["opus", "opus-4-8", "fable-5-1", "sonnet-legacy", "haiku-4-5", "mythos-5-1"]) {
    expect(tierMultiplier("vertex-regional", key)).toBe(1.1);
  }
    expect(tierMultiplier("vertex-regional", "gpt-5.5")).toBe(1);
    expect(tierMultiplier("anthropic", "opus-4-8")).toBe(1);
});

test("a Gemini request past 200k prompt tokens bills at the long-context rate", () => {
  const key = Object.keys(GENERATED_GEMINI_LONG_CONTEXT_PRICING)[0];
  const long = GENERATED_GEMINI_LONG_CONTEXT_PRICING[key];
  const tokens: TokenBreakdown = { input: 250_000, output: 1_000, cacheRead: 0, cacheWrite: 0 };
  expect(calculateCost(tokens, key)).toBeCloseTo(250_000 * long.input + 1_000 * long.output, 8);
  const small: TokenBreakdown = { input: 1_000, output: 1_000, cacheRead: 0, cacheWrite: 0 };
  const base = GENERATED_PRICING[key] ?? DEFAULT_PRICING[key];
  expect(calculateCost(small, key)).toBeCloseTo(1_000 * base.input + 1_000 * base.output, 8);
});

test("Claude 3-era ids price as their own model, not the current family card", () => {
  expect(claudePricingKey("claude-3-5-sonnet-20241022", DEFAULT_PRICING)).toBe("sonnet-legacy");
  expect(claudePricingKey("claude-3-opus-20240229", DEFAULT_PRICING)).toBe("opus-3");
  expect(claudePricingKey("claude-3-5-haiku-20241022", DEFAULT_PRICING)).toBe("haiku-3-5");
  expect(claudePricingKey(123 as unknown as string, DEFAULT_PRICING)).toBeNull();
});
