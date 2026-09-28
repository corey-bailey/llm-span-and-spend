// Load test for the catalog agent at 1, 5, and 20 concurrent users.
//
// Cost is bounded by construction: every stage runs a fixed number of iterations per VU,
// so the run makes at most MAX_REQUESTS requests (printed at start). Nothing here retries.
//
//   k6 run load/k6-agent.js                    # full run
//   ITERS=1 STAGE_GAP_S=90 k6 run load/k6-agent.js   # smoke: 26 requests
//
import http from "k6/http";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8010";
const ITERS = parseInt(__ENV.ITERS || "15", 10);
const GAP_S = parseInt(__ENV.STAGE_GAP_S || "240", 10); // each stage gets its own window
const REQ_TIMEOUT_S = parseInt(__ENV.REQ_TIMEOUT_S || "60", 10);
// A stage must be fully drained (maxDuration + gracefulStop) before the next one starts,
// or the two stages' latencies mix. maxDuration + gracefulStop == GAP_S exactly.
if (GAP_S <= REQ_TIMEOUT_S) {
  throw new Error(`STAGE_GAP_S (${GAP_S}) must exceed REQ_TIMEOUT_S (${REQ_TIMEOUT_S})`);
}
const STAGES = [
  { name: "vus_1", vus: 1, start: "0s" },
  { name: "vus_5", vus: 5, start: `${GAP_S}s` },
  { name: "vus_20", vus: 20, start: `${2 * GAP_S}s` },
];
export const MAX_REQUESTS = STAGES.reduce((n, s) => n + s.vus * ITERS, 0);

// Mixed difficulty: some answer in one tool call, some need search then calculate.
const QUESTIONS = [
  "How much is the 20F down sleeping bag?",
  "What does the 55L backpacking pack weigh?",
  "Total price of BAG-40F and PAD-INS?",
  "What does a 2-person tent plus a canister stove cost and weigh?",
  "Which item in the water category is lightest?",
  "List everything in the sleep category with prices.",
  "What is the cheapest shelter option?",
  "Total weight of TENT-2P, BAG-20F, PAD-INS and STOVE-CAN in kilograms?",
];

const costUsd = new Trend("agent_cost_usd");
const turns = new Trend("agent_turns");
const spendUsd = new Counter("agent_spend_usd");
const rateLimited = new Counter("agent_rate_limited");

export const options = {
  scenarios: Object.fromEntries(
    STAGES.map((s) => [
      s.name,
      {
        executor: "per-vu-iterations",
        vus: s.vus,
        iterations: ITERS,
        startTime: s.start,
        maxDuration: `${GAP_S - REQ_TIMEOUT_S}s`,
        gracefulStop: `${REQ_TIMEOUT_S}s`,
        tags: { stage: s.name },
      },
    ]),
  ),
  // Recorded, not enforced: the point is to see where the service bends.
  thresholds: Object.fromEntries(
    STAGES.flatMap((s) => [
      [`http_req_duration{stage:${s.name}}`, ["p(95)<20480"]],
      [`http_req_failed{stage:${s.name}}`, ["rate<0.01"]],
      [`agent_cost_usd{stage:${s.name}}`, ["avg<0.02"]],
      [`agent_turns{stage:${s.name}}`, ["avg<4"]], // more turns = tool retries = paid waste
    ]),
  ),
  summaryTrendStats: ["avg", "med", "p(95)", "max"],
};

export function setup() {
  console.log(`max requests this run: ${MAX_REQUESTS} (${ITERS} per VU)`);
}

export default function () {
  const q = QUESTIONS[(__VU + __ITER) % QUESTIONS.length];
  const res = http.post(`${BASE_URL}/ask`, JSON.stringify({ question: q }), {
    headers: { "Content-Type": "application/json" },
    timeout: `${REQ_TIMEOUT_S}s`,
  });
  if (res.status === 429) rateLimited.add(1);
  const ok = check(res, { "status is 200": (r) => r.status === 200 });
  if (ok) {
    const body = res.json();
    costUsd.add(body.cost_usd);
    spendUsd.add(body.cost_usd);
    turns.add(body.turns);
  }
}

export function handleSummary(data) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  return {
    [`load/results/summary-${stamp}.json`]: JSON.stringify(data, null, 2),
    stdout: stageTable(data),
  };
}

function stageTable(data) {
  const m = data.metrics;
  const val = (name, stage, stat) => {
    const metric = m[`${name}{stage:${stage}}`];
    return metric ? metric.values[stat] : undefined;
  };
  const fmt = (v, d = 2) => (v === undefined ? "-" : v.toFixed(d));
  const rows = STAGES.map((s) =>
    [
      s.name.padEnd(7),
      fmt(val("http_req_duration", s.name, "med") / 1000, 1).padStart(7),
      fmt(val("http_req_duration", s.name, "p(95)") / 1000, 1).padStart(7),
      fmt((val("http_req_failed", s.name, "rate") || 0) * 100, 1).padStart(7),
      fmt(val("agent_cost_usd", s.name, "avg"), 4).padStart(8),
      fmt(val("agent_turns", s.name, "avg"), 2).padStart(6),
    ].join("  "),
  );
  const total = m.agent_spend_usd ? m.agent_spend_usd.values.count : 0;
  const limited = m.agent_rate_limited ? m.agent_rate_limited.values.count : 0;
  return [
    "",
    "stage    med(s)  p95(s)  fail(%)  $/req    turns",
    ...rows,
    `total spend: $${total.toFixed(4)}   upstream 429s: ${limited}`,
    "",
  ].join("\n");
}
