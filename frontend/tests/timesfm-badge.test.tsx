import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TimesFMBadge } from "@/components/timesfm-badge";

describe("TimesFMBadge", () => {
  it("shows the mode a trade was scored with", () => {
    render(<TimesFMBadge tag={{ enabled: true, mode: "filter" }} />);
    expect(screen.getByText("TimesFM filter")).toBeTruthy();
  });
  it("says off when the signal was scored without TimesFM", () => {
    render(<TimesFMBadge tag={{ enabled: false, mode: null }} />);
    expect(screen.getByText("TimesFM off")).toBeTruthy();
  });
  it("renders nothing for untagged trades", () => {
    const { container } = render(<TimesFMBadge tag={null} />);
    expect(container.textContent).toBe("");
  });
});
