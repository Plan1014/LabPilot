import type { AuthzMode } from "../types/events";

/**
 * 审批模式 —— 输入栏左下角的按钮切换，整个对话内有效。
 *
 * auto：判官自动裁决，拿不准才问用户。
 * manual：判官不下场，命令与写操作一律由用户裁决（L1 的硬拒绝照旧）。
 */

/** 存进 localStorage 的键 —— 刷新页面后模式还在。 */
export const AUTHZ_MODE_KEY = "labpilot.authzMode";

/**
 * 认不出来的模式一律当 auto。
 *
 * 模式认错的后果不对称：当成 manual 只是多问用户几次，当成 auto 则可能
 * 悄悄放行 —— 所以认不出来时保持既有行为。
 */
export function normalizeMode(raw: unknown): AuthzMode {
  return raw === "manual" ? "manual" : "auto";
}

/** 切到另一个模式。 */
export function otherMode(mode: AuthzMode): AuthzMode {
  return mode === "manual" ? "auto" : "manual";
}

/** 按钮上的字 —— 当前是什么模式就显示什么。 */
export function modeLabel(mode: AuthzMode): string {
  return mode === "manual" ? "manual" : "auto";
}

/** 展开的那行说明 —— 当前模式怎么批。 */
export function modeDescription(mode: AuthzMode): string {
  return mode === "manual"
    ? "manual：用户决定是否执行命令"
    : "auto_mode：使用 agent 自动审批执行指令";
}

/** localStorage 的一点点子集 —— 存不了就当没有。 */
interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function defaultStorage(): StorageLike | null {
  try {
    const s = (globalThis as { localStorage?: StorageLike }).localStorage;
    return s ?? null;
  } catch {
    return null;
  }
}

/** 读回上次选的模式。读不到就 auto。 */
export function readStoredMode(storage?: StorageLike | null): AuthzMode {
  const s = storage ?? defaultStorage();
  if (!s) return "auto";
  try {
    return normalizeMode(s.getItem(AUTHZ_MODE_KEY));
  } catch {
    return "auto";
  }
}

/** 存下当前模式。存不了就算了 —— 模式在本轮对话里仍然有效。 */
export function writeStoredMode(mode: AuthzMode, storage?: StorageLike | null): void {
  const s = storage ?? defaultStorage();
  if (!s) return;
  try {
    s.setItem(AUTHZ_MODE_KEY, mode);
  } catch {
    // 隐私模式下 localStorage 会抛 —— 忽略
  }
}
