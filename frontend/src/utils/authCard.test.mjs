import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AUTH_CHOICES,
  authPayload,
  authPlaceholder,
  authRequestsFromPending,
  canRemember,
  deriveAuthState,
} from "./authCard.ts";

function authEvent(over = {}) {
  return {
    type: "auth_request",
    tool_call_id: "c1",
    question: "要执行 python tmp/x.py 吗？",
    reason: "写目标不明确",
    pattern: "bash:python tmp/x.py",
    batch_id: "b1",
    step: 0,
    ...over,
  };
}

test("AUTH_CHOICES 固定三项且顺序不变", () => {
  assert.deepEqual(
    AUTH_CHOICES.map((c) => c.value),
    ["allow_once", "allow_always", "deny"],
  );
  assert.deepEqual(
    AUTH_CHOICES.map((c) => c.label),
    ["允许一次", "允许并记住", "拒绝"],
  );
});

test("authPayload 给出决定的中文说明", () => {
  assert.equal(authPayload("allow_once"), "允许一次");
  assert.equal(authPayload("allow_always"), "允许并记住");
  assert.equal(authPayload("deny"), "拒绝");
});

test("canRemember 只在有模式时才为 true", () => {
  const withPattern = {
    toolCallId: "c1",
    question: "q",
    reason: "r",
    pattern: "bash:python tmp/x.py",
    batchId: "b1",
  };
  assert.equal(canRemember(withPattern), true);
  assert.equal(canRemember({ ...withPattern, pattern: null }), false);
  assert.equal(canRemember({ ...withPattern, pattern: "  " }), false);
});

test("deriveAuthState 从 auth_request 事件里认出待批调用", () => {
  const messages = [
    {
      id: "m1",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [authEvent()],
    },
  ];
  const state = deriveAuthState(messages);
  assert.equal(state.requests.length, 1);
  assert.equal(state.pending.toolCallId, "c1");
  assert.equal(state.pending.question, "要执行 python tmp/x.py 吗？");
  assert.equal(state.pending.pattern, "bash:python tmp/x.py");
  assert.equal(state.pending.batchId, "b1");
});

test("deriveAuthState 有对应 tool_result 时视为已裁决", () => {
  const messages = [
    {
      id: "m1",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [
        authEvent(),
        {
          type: "tool_result",
          result: "授权拒绝：用户拒绝了该操作",
          tool_call_id: "c1",
          step: 0,
        },
      ],
    },
  ];
  const state = deriveAuthState(messages);
  assert.equal(state.pending, null);
  assert.equal(state.decisions.get("c1"), "deny");
});

test("deriveAuthState 从 tool_result 的 auth_choice 读出决定", () => {
  const messages = [
    {
      id: "m1",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [
        authEvent(),
        {
          type: "tool_result",
          result: "ok",
          tool_call_id: "c1",
          auth_choice: "allow_always",
          step: 0,
        },
      ],
    },
  ];
  const state = deriveAuthState(messages);
  assert.equal(state.decisions.get("c1"), "allow_always");
  assert.equal(state.pending, null);
});

test("deriveAuthState 没有提问时不报错", () => {
  const state = deriveAuthState([]);
  assert.deepEqual(state.requests, []);
  assert.equal(state.pending, null);
});

test("authRequestsFromPending 把 /auth/pending 的返回转成待批卡片", () => {
  const raw = {
    pending: [
      {
        tool_call_id: "c1",
        tool_name: "bash",
        question: "要执行吗？",
        reason: "不确定",
        pattern: "bash:python tmp/x.py",
        batch_id: "b1",
        status: "pending",
      },
    ],
  };
  const out = authRequestsFromPending(raw);
  assert.equal(out.length, 1);
  assert.equal(out[0].toolCallId, "c1");
  assert.equal(out[0].question, "要执行吗？");
  assert.equal(out[0].pattern, "bash:python tmp/x.py");
  assert.equal(out[0].batchId, "b1");
});

test("authRequestsFromPending 对畸形输入返回空数组", () => {
  assert.deepEqual(authRequestsFromPending(null), []);
  assert.deepEqual(authRequestsFromPending("x"), []);
  assert.deepEqual(authRequestsFromPending({}), []);
  assert.deepEqual(authRequestsFromPending({ pending: "x" }), []);
  assert.deepEqual(authRequestsFromPending({ pending: [null, {}, { tool_call_id: 1 }] }), []);
});

test("authPlaceholder 在有待批操作时说明敲字等于拒绝", () => {
  assert.equal(authPlaceholder(false), "");
  assert.ok(authPlaceholder(true).includes("拒绝"));
});
