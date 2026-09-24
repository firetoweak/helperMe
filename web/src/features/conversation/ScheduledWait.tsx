import { Group, Text } from "@mantine/core";
import { IconClock } from "@tabler/icons-react";
import { useEffect, useState } from "react";

export function ScheduledWait({ dueAt }: { dueAt: string }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [dueAt]);

  const seconds = Math.max(0, Math.ceil((Date.parse(dueAt) - now) / 1000));
  const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
  const rest = String(seconds % 60).padStart(2, "0");
  const label =
    seconds === 0
      ? "Scheduled time reached…"
      : `Waiting up to ${minutes}m ${rest}s`;

  return (
    <Group className="scheduled-wait" gap="xs" wrap="nowrap">
      <IconClock size={15} />
      <Text aria-live="off" ff="monospace" size="sm">
        {label}
      </Text>
    </Group>
  );
}
