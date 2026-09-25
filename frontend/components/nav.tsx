"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import { setTheme, useIsDark } from "@/lib/theme";
import { Button } from "@/components/ui/button";
import { TimesFMIndicator } from "@/components/timesfm";

const LINKS = [
  ["/signals", "Signals"],
  ["/top", "Top signals"],
  ["/portfolio", "Paper Portfolio"],
  ["/history", "Trade History"],
  ["/news", "News Feed"],
  ["/backtest", "Backtest & Calibration"],
  ["/sources", "Source Health"],
  ["/settings", "Settings"],
] as const;

export function Nav() {
  const path = usePathname();
  const dark = useIsDark();
  const toggle = () => setTheme(!dark);

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-2">
        <Link href="/signals" className="shrink-0 font-semibold tracking-tight">
          Catalyst<span className="text-accent">Edge</span>
        </Link>
        <nav className="flex min-w-0 flex-1 gap-1 overflow-x-auto" aria-label="Main">
          {LINKS.map(([href, label]) => (
            <Link
              key={href}
              href={href}
              className={cn(
                "whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm text-muted hover:text-foreground",
                path?.startsWith(href) && "bg-foreground/10 text-foreground",
              )}
            >
              {label}
            </Link>
          ))}
        </nav>
        <TimesFMIndicator />
        <Button variant="ghost" size="icon" onClick={toggle} aria-label="Toggle light or dark mode">
          {/* Both icons render; CSS picks one, so server and client HTML always match. */}
          <Sun className="hidden h-4 w-4 dark:block" aria-hidden />
          <Moon className="block h-4 w-4 dark:hidden" aria-hidden />
        </Button>
      </div>
    </header>
  );
}

export function Footer() {
  return (
    <footer className="mt-12 border-t border-border py-6 text-center text-xs text-subtle">
      <strong>Not financial advice.</strong> CatalystEdge is a research and paper-trading tool. Simulated results do not
      predict future returns. Confidence is labelled UNCALIBRATED until outcome evidence supports it.
    </footer>
  );
}
