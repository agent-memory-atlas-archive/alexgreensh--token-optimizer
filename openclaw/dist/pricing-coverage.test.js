"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
// Every card in the generated price table is reachable by its model id, so a
// new generation is never billed at its family card. Generated from the table,
// so models added by the daily refresh are covered with no test edit.
const bun_test_1 = require("bun:test");
const pricing_1 = require("./pricing");
const prices_generated_1 = require("./prices.generated");
const GENERATION = /^(fable|mythos|opus|sonnet|haiku)-(\d+)(?:-(\d+))?$/;
(0, bun_test_1.test)("every generated Claude card is reachable by its model id", () => {
    for (const key of Object.keys(prices_generated_1.GENERATED_PRICING)) {
        const m = GENERATION.exec(key);
        if (!m)
            continue;
        const [, family, major, minor] = m;
        const version = minor ? `${major}-${minor}` : major;
        const id = Number(major) >= 4 ? `claude-${family}-${version}-20990101` : `claude-${version}-${family}-20990101`;
        (0, bun_test_1.expect)([id, (0, pricing_1.claudePricingKey)(id, pricing_1.DEFAULT_PRICING)]).toEqual([id, key]);
    }
});
(0, bun_test_1.test)("every generated OpenAI and Gemini card is reachable by a dated id", () => {
    for (const key of Object.keys(prices_generated_1.GENERATED_PRICING)) {
        if (!/^(gpt-|o\d|gemini-)/.test(key))
            continue;
        (0, bun_test_1.expect)([key, (0, pricing_1.pricedPrefixKey)(key + "-2099-01-01", pricing_1.DEFAULT_PRICING)]).toEqual([key, key]);
    }
});
//# sourceMappingURL=pricing-coverage.test.js.map