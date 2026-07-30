import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Replayable — Run explorer",
  description: "Inspect deterministic agent replays and behavior changes.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
