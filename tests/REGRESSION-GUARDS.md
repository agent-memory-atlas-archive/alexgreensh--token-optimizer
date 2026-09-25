# Regression guards

Dashboard and savings numbers have regressed more than once while every test
still passed. Each fix patched one spot and nothing stopped the next change from
bringing the bug back. Three layers now stop that:

1. **Golden user** (`test_golden_user.py`). A synthetic history with answers worked
   out by hand in the file: daily cost, pre-install baseline, before/after,
   repeat reads, report tiers, dashboard cards. It also re-collects, grows a
   session and ages the history, and checks nothing moves.
2. **Coverage from the price table** (`test_pricing_coverage.py`, and
   `pricing-coverage.test.ts` in both TypeScript engines). Every model in
   `prices.json` must bill at its own card in every engine. New models from the
   daily refresh are covered automatically.
3. **Planted bugs** (`scripts/check_regression_guards.py`, CI job
   `regression-guards`). Every bug that has shipped before is put back into a
   throwaway copy, and the guard tests must fail on it. A guard that passes on
   broken code fails the build.

## When you fix a bug

- Add a test that fails without the fix.
- Add the bug to `MUTATIONS` in `scripts/check_regression_guards.py`, so CI proves
  the test keeps catching it.
- If the fix changes a number on the dashboard, add that number to the golden
  user with its hand-worked value.

## Rules the guards hold

- A baseline or workload anchor is never later than install. It is pinned per
  user and never slides as history ages.
- One session counts once, however many copies or re-collects it has.
- Every model bills at its own generation card, never its family card.
- A flat before/after comparison never hides measured savings.
- A pricing fix reaches rows stored before it.
