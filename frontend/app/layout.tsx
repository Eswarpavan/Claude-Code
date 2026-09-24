import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";
import { Footer, Nav } from "@/components/nav";

export const metadata: Metadata = {
  title: "CatalystEdge",
  description: "News-first positive swing-trade signals and a $100 paper account. Not financial advice.",
};

// Applied before paint so the page never flashes the wrong theme. Dark is the default.
const themeScript = `try{var t=localStorage.getItem("catalystedge.theme");if(t!=="light")document.documentElement.classList.add("dark")}catch(e){document.documentElement.classList.add("dark")}`;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-screen antialiased">
        <Providers>
          <Nav />
          <main className="mx-auto max-w-7xl px-4 py-5">{children}</main>
          <Footer />
        </Providers>
      </body>
    </html>
  );
}
