import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Conductor — Approval Queue",
  description: "Human-in-the-loop review for the durable job-posting intake agent.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
