import { test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeOptions,
  orderOptions,
  deriveAskState,
  shouldDeliverNotification,
  shouldKeepOnStop,
  inputPlaceholder,
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

test("shouldDeliverNotification 有待答提问时不投递通知", () => {
  assert.equal(
    shouldDeliverNotification({
      undeliveredCount: 1,
      hasPendingAsk: true,
      isStreaming: false,
      streamingMessageId: null,
    }),
    false,
  );
});

test("shouldDeliverNotification 本轮还在跑时不投递通知", () => {
  assert.equal(
    shouldDeliverNotification({
      undeliveredCount: 1,
      hasPendingAsk: false,
      isStreaming: true,
      streamingMessageId: null,
    }),
    false,
  );
  assert.equal(
    shouldDeliverNotification({
      undeliveredCount: 1,
      hasPendingAsk: false,
      isStreaming: false,
      streamingMessageId: "a1",
    }),
    false,
  );
});

test("shouldDeliverNotification 有待批鉴权时不投递通知", () => {
  // 投递通知 = 直接敲字 → 后端会把最早的待批判成拒绝
  assert.equal(
    shouldDeliverNotification({
      undeliveredCount: 1,
      hasPendingAsk: false,
      hasPendingAuth: true,
      isStreaming: false,
      streamingMessageId: null,
    }),
    false,
  );
});

test("shouldDeliverNotification 没有未投递通知时不投递", () => {
  assert.equal(
    shouldDeliverNotification({
      undeliveredCount: 0,
      hasPendingAsk: false,
      isStreaming: false,
      streamingMessageId: null,
    }),
    false,
  );
});

test("shouldDeliverNotification 一切就绪时投递通知", () => {
  assert.equal(
    shouldDeliverNotification({
      undeliveredCount: 2,
      hasPendingAsk: false,
      isStreaming: false,
      streamingMessageId: null,
    }),
    true,
  );
});

test("shouldKeepOnStop 保留还没被回答的提问", () => {
  const message = {
    id: "m1",
    role: "assistant",
    content: "",
    isComplete: false,
    events: [
      {
        type: "ask_user",
        question: "用哪套？",
        options: [],
        tool_call_id: "c1",
        step: 1,
      },
    ],
  };
  assert.equal(shouldKeepOnStop(message, new Map()), true);
  assert.equal(shouldKeepOnStop(message, new Map([["c1", "参数集 A"]])), false);
});

test("shouldKeepOnStop 保留还没裁决的鉴权请求", () => {
  const message = {
    id: "m1",
    role: "assistant",
    content: "",
    isComplete: false,
    events: [
      {
        type: "auth_request",
        question: "要执行吗？",
        tool_call_id: "c1",
        step: 0,
      },
    ],
  };
  assert.equal(shouldKeepOnStop(message, new Map()), true);
  assert.equal(
    shouldKeepOnStop(message, new Map(), new Map([["c1", "allow_once"]])),
    false,
  );
});

test("shouldKeepOnStop 普通消息不保留", () => {
  const message = {
    id: "m1",
    role: "assistant",
    content: "",
    isComplete: false,
    events: [{ type: "text", content: ["在做一件事"], step: 1 }],
  };
  assert.equal(shouldKeepOnStop(message, new Map()), false);
});

test("inputPlaceholder 有待答提问时提示这是回答", () => {
  assert.equal(inputPlaceholder(true), "回答上面的提问…");
  assert.equal(inputPlaceholder(false), "Ask LabPilot...");
});
