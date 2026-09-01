import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Sidra Algo Trading",
  description: "Professional paper-first algorithmic trading command center",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const themeScript = `(function(){try{var p=localStorage.getItem('sidra-theme')||'system';var d=p==='system'?matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light':p;document.documentElement.dataset.theme=d;document.documentElement.style.colorScheme=d}catch(e){}})()`;
  return <html lang="en" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{ __html: themeScript }} /></head><body>{children}</body></html>;
}
