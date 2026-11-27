import { test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeMode,
  otherMode,
  modeLabel,
  modeDescription,
  readStoredMode,
  writeStoredMode,
} from "./authzMode.ts";

/** 假 localStorage —— node 里没有真的那个。 */
function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  };
}

test("normalizeMode 认得出手动模式", () => {
  assert.equal(normalizeMode("manual"), "manual");
  assert.equal(normalizeMode("auto"), "auto");
});

test("normalizeMode 认不出来的值一律当 auto", () => {
  assert.equal(normalizeMode("whatever"), "auto");
  assert.equal(normalizeMode(undefined), "auto");
  assert.equal(normalizeMode(null), "auto");
  assert.equal(normalizeMode(42), "auto");
});

test("otherMode 在两个模式之间来回切", () => {
  assert.equal(otherMode("auto"), "manual");
  assert.equal(otherMode("manual"), "auto");
});

test("modeLabel 就是当前模式的名字", () => {
  assert.equal(modeLabel("auto"), "auto");
  assert.equal(modeLabel("manual"), "manual");
});

test("modeDescription 说明当前模式怎么批", () => {
  assert.equal(modeDescription("auto"), "auto_mode：使用 agent 自动审批执行指令");
  assert.equal(modeDescription("manual"), "manual：用户决定是否执行命令");
});

test("模式能存进 localStorage 并读回来", () => {
  const storage = fakeStorage();
  writeStoredMode("manual", storage);
  assert.equal(readStoredMode(storage), "manual");
  writeStoredMode("auto", storage);
  assert.equal(readStoredMode(storage), "auto");
});

test("localStorage 里没有东西时默认 auto", () => {
  assert.equal(readStoredMode(fakeStorage()), "auto");
});

test("localStorage 里是脏值时默认 auto", () => {
  assert.equal(readStoredMode(fakeStorage({ "labpilot.authzMode": "????" })), "auto");
});

test("localStorage 存不了时不抛异常", () => {
  const broken = {
    getItem() {
      throw new Error("denied");
    },
    setItem() {
      throw new Error("denied");
    },
  };
  writeStoredMode("manual", broken);
  assert.equal(readStoredMode(broken), "auto");
});
