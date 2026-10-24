import { describe, expect, it } from "vitest";
import { SseDecoder } from "./sse";

describe("SseDecoder", () => {
  it("重组跨分块的 UTF-8 字符与帧边界", () => {
    const decoder = new SseDecoder();
    const bytes = new TextEncoder().encode('data: {"event":"token","text":"你好"}\n\ndata: {"event":"done","trace_id":"t"}\n\n');
    const cut = bytes.indexOf(0xe5) + 1;
    expect(decoder.feed(bytes.slice(0, cut))).toEqual([]);
    expect(decoder.feed(bytes.slice(cut, bytes.length - 1))).toEqual([
      { event: "token", text: "你好" },
    ]);
    expect(decoder.feed(bytes.slice(bytes.length - 1))).toEqual([
      { event: "done", trace_id: "t" },
    ]);
  });

  it("处理 CRLF、注释及未终止的尾帧", () => {
    const decoder = new SseDecoder();
    expect(decoder.feed(new TextEncoder().encode(': ping\r\ndata: {"event":"started","trace_id":"x"}\r\n\r\n'))).toEqual([
      { event: "started", trace_id: "x" },
    ]);
    decoder.feed(new TextEncoder().encode('data: {"event":"done","trace_id":"x"}'));
    expect(decoder.finish()).toEqual([{ event: "done", trace_id: "x" }]);
  });

  it("逐字节读取中文和 CRLF 时不丢失或重复事件", () => {
    const decoder = new SseDecoder();
    const bytes = new TextEncoder().encode(
      'data: {"event":"token","text":"你好𠀀"}\r\n\r\ndata: {"event":"done","trace_id":"逐字节"}\r\n\r\n',
    );
    const events = Array.from(bytes).flatMap((byte) => decoder.feed(new Uint8Array([byte])));
    expect([...events, ...decoder.finish()]).toEqual([
      { event: "token", text: "你好𠀀" },
      { event: "done", trace_id: "逐字节" },
    ]);
  });

  it("多行 data 合并为一个事件", () => {
    const decoder = new SseDecoder();
    expect(decoder.feed(new TextEncoder().encode(
      'data: {"event":"token",\ndata: "text":"多行回答"}\n\n',
    ))).toEqual([{ event: "token", text: "多行回答" }]);
  });

  it("忽略控制帧和坏 JSON 后仍能读取有效事件", () => {
    const decoder = new SseDecoder();
    const frames = [
      ': ping\nevent: heartbeat\nid: 123\nretry: 1000',
      'data: [DONE]',
      'data:',
      'data: {bad json}',
      'data: null',
      'data: []',
      'data: {"text":"没有事件类型"}',
      'data: {"event":42}',
      'data: {"event":"token","text":"继续回答"}',
    ];
    expect(decoder.feed(new TextEncoder().encode(`${frames.join("\n\n")}\n\n`))).toEqual([
      { event: "token", text: "继续回答" },
    ]);
    expect(decoder.finish()).toEqual([]);
  });

  it("EOF 结算跨分块的尾帧且重复结算不产生事件", () => {
    const decoder = new SseDecoder();
    expect(decoder.finish()).toEqual([]);
    expect(decoder.feed(new TextEncoder().encode('data: {"event":"token","text":"尾'))).toEqual([]);
    expect(decoder.feed(new TextEncoder().encode('帧"}'))).toEqual([]);
    expect(decoder.finish()).toEqual([{ event: "token", text: "尾帧" }]);
    expect(decoder.finish()).toEqual([]);
  });

  it("EOF 丢弃无法解析的截断尾帧", () => {
    const decoder = new SseDecoder();
    expect(decoder.feed(new TextEncoder().encode('data: {"event":"token","text":"截断'))).toEqual([]);
    expect(decoder.finish()).toEqual([]);
    expect(decoder.finish()).toEqual([]);
  });

  it("持续累积的不完整超大帧会报错", () => {
    const decoder = new SseDecoder();
    const encoder = new TextEncoder();
    expect(decoder.feed(encoder.encode('data: {"event":"token","text":"'))).toEqual([]);
    expect(decoder.feed(encoder.encode("a".repeat(600_000)))).toEqual([]);
    expect(() => decoder.feed(encoder.encode("a".repeat(600_000)))).toThrow();
  });
});
