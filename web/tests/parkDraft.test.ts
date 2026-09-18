import { describe, expect, it } from "vitest";

import {
  composeSendContent,
  joinRestoredText,
  parkedPreviewText,
  restoreParkedDraft,
} from "../src/features/conversation/parkDraft";

describe("parked composer draft", () => {
  it("returns the parked text to the editor instead of discarding it", () => {
    const restored = restoreParkedDraft(
      {
        text: "预先输入的下一句",
        pending: [
          {
            localId: "img-1",
            name: "shot.png",
            previewUrl: "blob:parked",
            attachmentId: "att-1",
            state: "done",
          },
        ],
      },
      { text: "", pending: [] },
    );

    expect(restored.text).toBe("预先输入的下一句");
    expect(restored.pending).toEqual([
      {
        localId: "img-1",
        name: "shot.png",
        previewUrl: "blob:parked",
        attachmentId: "att-1",
        state: "done",
      },
    ]);
  });

  it("keeps later editor typing after the restored parked text", () => {
    expect(joinRestoredText("先发这句", "再补一句")).toBe("先发这句\n再补一句");
  });

  it("does not rewrite image tokens until the parked draft actually sends", () => {
    expect(composeSendContent("下一句", 2)).toBe("下一句 [Image #1] [Image #2]");
    expect(parkedPreviewText({ text: "  下一句  ", pending: [] })).toBe("下一句");
    expect(
      parkedPreviewText({
        text: "   ",
        pending: [
          {
            localId: "img-1",
            name: "shot.png",
            previewUrl: "blob:parked",
            attachmentId: "att-1",
            state: "done",
          },
        ],
      }),
    ).toBe("图片");
  });
});
