import { useState, type FormEvent, type KeyboardEvent } from "react";

type ComposerProps = {
  disabled: boolean;
  sending: boolean;
  running: boolean;
  cancelling: boolean;
  onSend: (text: string) => Promise<void>;
  onCancel: () => void;
};

export function Composer({
  disabled,
  sending,
  running,
  cancelling,
  onSend,
  onCancel,
}: ComposerProps) {
  const [text, setText] = useState("");

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const content = text.trim();
    if (content === "" || disabled || sending) {
      return;
    }
    setText("");
    await onSend(content);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form className="composer" onSubmit={(event) => void submit(event)}>
      <textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder="发送消息"
        rows={1}
        disabled={disabled}
      />
      <div className="composer-actions">
        {running ? (
          <button
            type="button"
            className="composer-stop"
            disabled={cancelling}
            onClick={onCancel}
          >
            停止
          </button>
        ) : null}
        <button type="submit" disabled={disabled || sending || text.trim() === ""}>
          发送
        </button>
      </div>
    </form>
  );
}
