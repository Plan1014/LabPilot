import { test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeOptions,
  orderOptions,
  deriveAskState,
} from "./askUser.ts";

test("normalizeOptions 归一化模型给的原始选项", () => {
  assert.deepEqual(
    normalizeOptions([{ label: " A ", description: " 因为 ", recommended: true }]),
    [{ label: "A", description: "因为", recommended: true }],
  );
  assert.deepEqual(normalizeOptions([{ label: "" }, "x", null]), []);
});

test("normalizeOptions 缺 recommended 时按 false 处理", () => {
  assert.deepEqual(normalizeOptions([{ label: "A" }]), [
    { label: "A", description: undefined, recommended: false },
  ]);
});

test("normalizeOptions 最多保留 4 项", () => {
  const raw = Array.from({ length: 6 }, (_, i) => ({ label: `L${i}` }));
  assert.equal(normalizeOptions(raw).length, 4);
});

test("orderOptions 把 recommended 项排到第一位", () => {
  const out = orderOptions([
    { label: "A", description: undefined, recommended: false },
    { label: "B", description: undefined, recommended: true },
  ]);
  assert.equal(out[0].label, "B");
  assert.equal(out[1].label, "A");
});

test("orderOptions 没有 recommended 时保持原序", () => {
  const out = orderOptions([
    { label: "A", description: undefined, recommended: false },
    { label: "B", description: undefined, recommended: false },
  ]);
  assert.equal(out[0].label, "A");
  assert.equal(out[1].label, "B");
});

test("deriveAskState 从 ask_user 事件里认出待答提问", () => {
  const messages = [
    {
      id: "m1",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [
        {
          type: "ask_user",
          question: "用哪套？",
          options: [{ label: "A", recommended: true }],
          tool_call_id: "c1",
          step: 1,
        },
      ],
    },
  ];
  const state = deriveAskState(messages);
  assert.equal(state.pendingAsk.toolCallId, "c1");
  assert.equal(state.pendingAsk.question, "用哪套？");
  assert.equal(state.pendingAsk.options.length, 1);
});

test("deriveAskState 认得出回放路径的 tool_call 事件", () => {
  const messages = [
    {
      id: "m1",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [
        {
          type: "tool_call",
          name: "ask_user",
          input: { question: "Q", options: [{ label: "A" }] },
          tool_call_id: "c1",
          step: 1,
        },
      ],
    },
  ];
  const state = deriveAskState(messages);
  assert.equal(state.pendingAsk.question, "Q");
  assert.equal(state.pendingAsk.toolCallId, "c1");
});

test("deriveAskState 有 tool_result 配对时视为已回答", () => {
  const messages = [
    {
      id: "m1",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [
        {
          type: "ask_user",
          question: "Q",
          options: [],
          tool_call_id: "c1",
          step: 1,
        },
      ],
    },
    {
      id: "m2",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [
        {
          type: "tool_result",
          name: "ask_user",
          result: "参数集 A",
          tool_call_id: "c1",
          step: 1,
        },
      ],
    },
  ];
  const state = deriveAskState(messages);
  assert.equal(state.pendingAsk, null);
  assert.equal(state.answers.get("c1"), "参数集 A");
});

test("deriveAskState 连续两次提问时弹出第二个", () => {
  const messages = [
    {
      id: "m1",
      role: "assistant",
      content: "",
      isComplete: true,
      events: [
        {
          type: "ask_user",
          question: "Q1",
          options: [],
          tool_call_id: "c1",
          step: 1,
        },
        {
          type: "tool_result",
          name: "ask_user",
          result: "A",
          tool_call_id: "c1",
          step: 1,
        },
        {
          type: "ask_user",
          question: "Q2",
          options: [],
          tool_call_id: "c2",
          step: 2,
        },
      ],
    },
  ];
  assert.equal(deriveAskState(messages).pendingAsk.question, "Q2");
});

test("deriveAskState 没有提问时不报错", () => {
  const state = deriveAskState([]);
  assert.equal(state.pendingAsk, null);
  assert.deepEqual(state.asks, []);
});
