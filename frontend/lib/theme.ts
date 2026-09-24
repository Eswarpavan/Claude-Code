"use client";

import * as React from "react";

const NAMES = ["--series-1", "--series-2", "--foreground", "--muted", "--subtle", "--grid", "--axis", "--card"] as const;
export type ThemeColors = Record<(typeof NAMES)[number], string>;

function subscribe(onChange: () => void) {
  const obs = new MutationObserver(onChange);
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => obs.disconnect();
}

/** True when the dark theme is active; follows the toggle live. Dark is the default. */
export function useIsDark(): boolean {
  return React.useSyncExternalStore(
    subscribe,
    () => document.documentElement.classList.contains("dark"),
    () => true,
  );
}

const noopSubscribe = () => () => {};

/** Chart colors as concrete values (SVG presentation attributes cannot use var()); null during server render. */
export function useThemeColors(): ThemeColors | null {
  const dark = useIsDark();
  const hydrated = React.useSyncExternalStore(noopSubscribe, () => true, () => false);
  return React.useMemo(() => {
    if (!hydrated) return null;
    void dark; // re-read the CSS variables whenever the theme changes
    const cs = getComputedStyle(document.documentElement);
    return Object.fromEntries(NAMES.map((n) => [n, cs.getPropertyValue(n).trim()])) as ThemeColors;
  }, [dark, hydrated]);
}

export function setTheme(dark: boolean) {
  document.documentElement.classList.toggle("dark", dark);
  try {
    window.localStorage.setItem("catalystedge.theme", dark ? "dark" : "light");
  } catch {}
}
