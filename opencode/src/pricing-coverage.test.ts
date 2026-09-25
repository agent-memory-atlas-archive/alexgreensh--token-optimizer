// Every card in the generated price table is reachable by its model id, so a
// new generation is never billed at its family card. Generated from the table,
// so models added by the daily refresh are covered with no test edit.
import { expect, test } from "bun:test";
import { claudePricingKey, DEFAULT_PRICING, pricedPrefixKey } from "./pricing.js";
import { GENERATED_PRICING } from "./prices.generated.js";

const GENERATION = /^(fable|mythos|opus|sonnet|haiku)-(\d+)(?:-(\d+))?$/;

test("every generated Claude card is reachable by its model id", () => {
  for (const key of Object.keys(GENERATED_PRICING)) {
    const m = GENERATION.exec(key);
    if (!m) continue;
    const [, family, major, minor] = m;
    const version = minor ? `${major}-${minor}` : major;
    const id = Number(major) >= 4 ? `claude-${family}-${version}-20990101` : `claude-${version}-${family}-20990101`;
    expect([id, claudePricingKey(id, DEFAULT_PRICING)]).toEqual([id, key]);
  }
});

test("every generated OpenAI and Gemini card is reachable by a dated id", () => {
  for (const key of Object.keys(GENERATED_PRICING)) {
    if (!/^(gpt-|o\d|gemini-)/.test(key)) continue;
    expect([key, pricedPrefixKey(key + "-2099-01-01", DEFAULT_PRICING)]).toEqual([key, key]);
  }
});
