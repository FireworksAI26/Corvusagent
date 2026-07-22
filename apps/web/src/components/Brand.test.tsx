import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BrandLockup, BrandMark } from "./Brand";

describe("Corvus brand assets", () => {
  it("renders the approved concentric-C mark with an accessible name", () => {
    const { container } = render(<BrandMark />);

    expect(screen.getByRole("img", { name: "Corvus" })).toBeVisible();
    expect(container.querySelectorAll("path")).toHaveLength(2);
    expect(container.querySelector("rect[data-brand-signal='true']")).toBeTruthy();
  });

  it("renders the uppercase wordmark without duplicating the accessible name", () => {
    render(<BrandLockup />);

    expect(screen.getByRole("img", { name: "Corvus" })).toBeVisible();
    expect(screen.getByText("CORVUS")).toBeVisible();
    expect(screen.getAllByRole("img")).toHaveLength(1);
  });
});
