import { createTheme } from "@mantine/core";

export const theme = createTheme({
  primaryColor: "sage",
  colors: {
    sage: [
      "#f3f7ed",
      "#e4ecd9",
      "#c8d9b4",
      "#aac58c",
      "#91b56a",
      "#81ab55",
      "#76a64a",
      "#638f3c",
      "#567f33",
      "#486d29",
    ],
  },
  fontFamily:
    'Inter, "Microsoft YaHei UI", "PingFang SC", system-ui, sans-serif',
  fontFamilyMonospace:
    '"JetBrains Mono", "Cascadia Code", Consolas, monospace',
  defaultRadius: "md",
  autoContrast: true,
  cursorType: "pointer",
});
