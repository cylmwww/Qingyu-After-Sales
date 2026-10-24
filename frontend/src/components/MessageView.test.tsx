import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MessageView } from "./MessageView";
import type { Conversation } from "../types";

describe("回答展示", () => {
  it("展示回答正文和来源", () => {
    const conversation: Conversation = {
      id: "chat-1",
      title: "退货政策",
      updatedAt: 0,
      messages: [{
        id: "answer-1",
        role: "assistant",
        state: "complete",
        content: "根据退货规则，质量问题可以申请售后。",
        strategy: "multi",
        latencyMs: 4200,
        citations: [{ chunk_id: "chunk-1", document_id: "policy-1", title: "退货规则", section: null }],
      }],
    };
    const html = renderToStaticMarkup(
      <MessageView conversation={conversation} busy={false} onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain("质量问题可以申请售后");
    expect(html).toContain("退货规则");
    expect(html).toContain('aria-label="回答来源"');
  });

  it("按片段去重引用并保留首份来源和不同章节", () => {
    const conversation: Conversation = {
      id: "chat-2",
      title: "售后问题",
      updatedAt: 0,
      messages: [{
        id: "answer-2",
        role: "assistant",
        state: "complete",
        content: "可以按对应章节办理。",
        citations: [
          { chunk_id: "chunk-1", document_id: "policy-1", title: "退货规则", section: "质量问题" },
          { chunk_id: "chunk-1", document_id: "policy-1", title: "重复检索标题", section: "重复检索章节" },
          { chunk_id: "chunk-2", document_id: "policy-1", title: "退货规则", section: "非质量问题" },
        ],
      }],
    };
    const html = renderToStaticMarkup(
      <MessageView conversation={conversation} busy={false} onConfirm={() => {}} onCancel={() => {}} />,
    );

    expect(html.match(/退货规则/g)).toHaveLength(2);
    expect(html).toContain("退货规则 · 质量问题");
    expect(html).toContain("退货规则 · 非质量问题");
    expect(html).not.toContain("重复检索标题");
    expect(html).not.toContain("重复检索章节");
  });
});
