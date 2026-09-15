import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { BrandMark } from "@/components/brand-mark";

describe("BrandMark", () => {
  it("is decorative by default and drawn from shapes, not a letter", () => {
    const { container } = render(<BrandMark />);
    const svg = container.querySelector("svg") as SVGSVGElement;

    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).not.toHaveAttribute("role");
    expect(svg.querySelectorAll("path")).toHaveLength(2);
    expect(svg.querySelector("text")).toBeNull();
    expect(svg.textContent).toBe("");
  });

  it("gets an accessible name when it is used on its own", () => {
    const { getByRole } = render(<BrandMark label="ResearchMind Lab" />);

    expect(getByRole("img", { name: "ResearchMind Lab" })).not.toHaveAttribute("aria-hidden");
  });

  it("supports a size, the brand colour or an inherited colour, and extra classes", () => {
    const { container, rerender } = render(<BrandMark size={32} className="mr-2" />);
    const svg = () => container.querySelector("svg") as SVGSVGElement;

    expect(svg()).toHaveAttribute("width", "32");
    expect(svg()).toHaveAttribute("height", "32");
    expect(svg()).toHaveClass("text-primary", "mr-2");
    expect(svg()).toHaveAttribute("fill", "currentColor");

    rerender(<BrandMark tone="current" />);
    expect(svg()).not.toHaveClass("text-primary");
    expect(svg()).toHaveAttribute("width", "24");
  });
});
