"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const bun_test_1 = require("bun:test");
const pricing_1 = require("./pricing");
const quality_1 = require("./quality");
const prices_generated_1 = require("./prices.generated");
const BASE_TOKENS = {
    input: 50_000,
    output: 50_000,
    cacheRead: 50_000,
    cacheWrite: 50_000,
};
const LONG_CONTEXT_TOKENS = {
    input: 1_000_000,
    output: 1_000_000,
    cacheRead: 1_000_000,
    cacheWrite: 1_000_000,
};
// Expected rates come from the auto-refreshed table, so a real price change
// (promo ending, new card) never breaks this test; it checks that the engine
// applies those rates correctly, including the per-request long-context tier.
const MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"];
const cost = (t, r) => t.input * r.input + t.output * r.output + t.cacheRead * r.cacheRead + t.cacheWrite * r.cacheWrite;
(0, bun_test_1.test)("GPT-5.6 aliases normalize to the documented canonical IDs", () => {
    (0, bun_test_1.expect)((0, pricing_1.normalizeModelName)("gpt-5.6")).toBe("gpt-5.6-sol");
    (0, bun_test_1.expect)((0, pricing_1.normalizeModelName)("openrouter/openai/gpt-5.6-sol-2026-07-09")).toBe("gpt-5.6-sol");
    (0, bun_test_1.expect)((0, pricing_1.normalizeModelName)("GPT-5.6 Sol Pro")).toBe("gpt-5.6-sol");
    (0, bun_test_1.expect)((0, pricing_1.normalizeModelName)("openai:gpt-5.6-terra-2026-07-09")).toBe("gpt-5.6-terra");
    (0, bun_test_1.expect)((0, pricing_1.normalizeModelName)("gpt-5.6_luna")).toBe("gpt-5.6-luna");
});
(0, bun_test_1.test)("GPT-5.6 pricing applies base and long-context API-equivalent rates", () => {
    (0, pricing_1.resetPricingCache)();
    for (const model of MODELS) {
        const base = prices_generated_1.GENERATED_PRICING[model];
        const long = prices_generated_1.GENERATED_OPENAI_LONG_CONTEXT_PRICING[model];
        (0, bun_test_1.expect)(base).toBeDefined();
        (0, bun_test_1.expect)(long).toBeDefined();
        (0, bun_test_1.expect)(pricing_1.DEFAULT_PRICING[model]).toEqual(base);
        (0, bun_test_1.expect)(long.input).toBeGreaterThan(base.input);
        (0, bun_test_1.expect)((0, pricing_1.calculateCost)(BASE_TOKENS, model, "/tmp/token-optimizer-gpt56-no-config")).toBeCloseTo(cost(BASE_TOKENS, base), 6);
        (0, bun_test_1.expect)((0, pricing_1.calculateCost)(LONG_CONTEXT_TOKENS, model, "/tmp/token-optimizer-gpt56-no-config")).toBeCloseTo(cost(LONG_CONTEXT_TOKENS, long), 6);
    }
});
(0, bun_test_1.test)("GPT-5.6 feeds OpenClaw savings and context-window helpers", () => {
    (0, bun_test_1.expect)((0, quality_1.freshSessionSavingsUsd)(1_000_000, "GPT-5.6 Sol Pro")).toBeCloseTo(prices_generated_1.GENERATED_PRICING["gpt-5.6-sol"].input * 1e6);
    (0, bun_test_1.expect)((0, quality_1.freshSessionSavingsUsd)(1_000_000, "openai:gpt-5.6-terra-2026-07-09")).toBeCloseTo(prices_generated_1.GENERATED_PRICING["gpt-5.6-terra"].input * 1e6);
    (0, bun_test_1.expect)((0, quality_1.freshSessionSavingsUsd)(1_000_000, "gpt-5.6_luna")).toBeCloseTo(prices_generated_1.GENERATED_PRICING["gpt-5.6-luna"].input * 1e6);
    (0, bun_test_1.expect)((0, quality_1.contextWindowForModel)("gpt-5.6")).toBe(1_050_000);
    (0, bun_test_1.expect)((0, quality_1.contextWindowForModel)("openrouter/openai/gpt-5.6-terra-2026-07-09")).toBe(1_050_000);
});
(0, bun_test_1.test)("vertex-regional surcharge applies to every Claude card, including generated generations", () => {
    for (const key of ["opus", "opus-4-8", "fable-5-1", "sonnet-legacy", "haiku-4-5", "mythos-5-1"]) {
        (0, bun_test_1.expect)((0, pricing_1.tierMultiplier)("vertex-regional", key)).toBe(1.1);
    }
    (0, bun_test_1.expect)((0, pricing_1.tierMultiplier)("vertex-regional", "gpt-5.5")).toBe(1);
    (0, bun_test_1.expect)((0, pricing_1.tierMultiplier)("anthropic", "opus-4-8")).toBe(1);
});
(0, bun_test_1.test)("a Gemini request past 200k prompt tokens bills at the long-context rate", () => {
    const key = Object.keys(prices_generated_1.GENERATED_GEMINI_LONG_CONTEXT_PRICING)[0];
    const long = prices_generated_1.GENERATED_GEMINI_LONG_CONTEXT_PRICING[key];
    const tokens = { input: 250_000, output: 1_000, cacheRead: 0, cacheWrite: 0 };
    (0, bun_test_1.expect)((0, pricing_1.calculateCost)(tokens, key)).toBeCloseTo(250_000 * long.input + 1_000 * long.output, 8);
    const small = { input: 1_000, output: 1_000, cacheRead: 0, cacheWrite: 0 };
    const base = prices_generated_1.GENERATED_PRICING[key] ?? pricing_1.DEFAULT_PRICING[key];
    (0, bun_test_1.expect)((0, pricing_1.calculateCost)(small, key)).toBeCloseTo(1_000 * base.input + 1_000 * base.output, 8);
});
(0, bun_test_1.test)("Claude 3-era ids price as their own model, not the current family card", () => {
    (0, bun_test_1.expect)((0, pricing_1.claudePricingKey)("claude-3-5-sonnet-20241022", pricing_1.DEFAULT_PRICING)).toBe("sonnet-legacy");
    (0, bun_test_1.expect)((0, pricing_1.claudePricingKey)("claude-3-opus-20240229", pricing_1.DEFAULT_PRICING)).toBe("opus-3");
    (0, bun_test_1.expect)((0, pricing_1.claudePricingKey)("claude-3-5-haiku-20241022", pricing_1.DEFAULT_PRICING)).toBe("haiku-3-5");
    (0, bun_test_1.expect)((0, pricing_1.claudePricingKey)(123, pricing_1.DEFAULT_PRICING)).toBeNull();
});
//# sourceMappingURL=pricing-gpt56.test.js.map