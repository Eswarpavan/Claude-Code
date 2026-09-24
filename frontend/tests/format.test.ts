import { describe, expect, it } from "vitest";
import { ago, capBucket, pct, title, usd } from "@/lib/format";

describe("format", () => {
  it("formats money and percentages", () => {
    expect(usd(1234.5)).toBe("$1,234.50");
    expect(usd(null)).toBe("–");
    expect(pct(3.456)).toBe("+3.5%");
    expect(pct(-2)).toBe("-2.0%");
    expect(pct(2, 1, false)).toBe("2.0%");
  });
  it("describes time since news", () => {
    expect(ago(0.2)).toBe("12 min ago");
    expect(ago(3)).toBe("3.0 h ago");
    expect(ago(30)).toBe("30 h ago");
    expect(ago(null)).toBe("–");
  });
  it("titles and caps", () => {
    expect(title("m_and_a_target")).toBe("M And A Target");
    expect(capBucket(20e9)).toBe("large");
    expect(capBucket(100e6)).toBe("micro");
  });
});
