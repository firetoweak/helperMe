export type ComposerImage = {
  localId: string;
  name: string;
  previewUrl: string;
  attachmentId: string | null;
  state: "uploading" | "done" | "error";
};

export type ComposerDraft = {
  text: string;
  pending: ComposerImage[];
};

export function restoreParkedDraft(
  parked: ComposerDraft,
  current: ComposerDraft,
): ComposerDraft {
  return {
    text: joinRestoredText(parked.text, current.text),
    pending: [...parked.pending, ...current.pending],
  };
}

export function joinRestoredText(parkedText: string, currentText: string): string {
  if (currentText.trim() === "") {
    return parkedText;
  }
  if (parkedText.trim() === "") {
    return currentText;
  }
  return `${parkedText.replace(/\s+$/u, "")}\n${currentText.replace(/^\s+/u, "")}`;
}

export function composeSendContent(text: string, readyCount: number): string {
  const tokens = Array.from(
    { length: readyCount },
    (_, index) => `[Image #${index + 1}]`,
  );
  return [text.trim(), ...tokens].filter(Boolean).join(" ");
}

export function parkedPreviewText(draft: ComposerDraft): string {
  const text = draft.text.trim();
  if (text !== "") {
    return text;
  }
  return draft.pending.length > 0 ? "图片" : "";
}
