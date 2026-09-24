import { expect, test } from "bun:test";

test("the dashboard opener reports failure when the opener exits nonzero", async () => {
  const { openDetached } = await import("./dashboard.js");
  expect(await openDetached("false", [])).toBe(false);
  expect(await openDetached("true", [])).toBe(true);
  expect(await openDetached("definitely-not-a-real-binary-xyz", [])).toBe(false);
});
