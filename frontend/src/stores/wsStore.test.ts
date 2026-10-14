import { beforeEach, describe, expect, it } from "vitest";
import { useWsStore } from "./wsStore";
import { violation } from "@/test/render";

beforeEach(() => useWsStore.setState({ recent: [], violations: [], toasts: [] }));

describe("wsStore", () => {
  it("collects violation messages and keeps the three newest as toasts", () => {
    const vs = [0, 1, 2, 3].map((i) => violation({ id: `v${i}` }));
    vs.forEach((v) => useWsStore.getState().receive({ type: "violation", data: v }));
    const s = useWsStore.getState();
    expect(s.violations.map((v) => v.id)).toEqual(["v3", "v2", "v1", "v0"]);
    expect(s.toasts.map((v) => v.id)).toEqual(["v3", "v2", "v1"]);
    expect(s.recent).toEqual([]);
  });

  it("dismisses a toast but keeps the violation", () => {
    const v = violation();
    useWsStore.getState().receive({ type: "violation", data: v });
    useWsStore.getState().dismissToast(v);
    expect(useWsStore.getState().toasts).toEqual([]);
    expect(useWsStore.getState().violations).toEqual([v]);
  });

  it("still routes trace messages and ignores unknown ones", () => {
    useWsStore.getState().receive({ type: "trace", data: { id: "t1" } });
    useWsStore.getState().receive({ type: "other" });
    useWsStore.getState().receive(null);
    expect(useWsStore.getState().recent).toEqual([{ id: "t1" }]);
    expect(useWsStore.getState().violations).toEqual([]);
  });
});
