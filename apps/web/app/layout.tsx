import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Intraday Sentinel",
  description: "Paper-first NSE intraday scanner",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
