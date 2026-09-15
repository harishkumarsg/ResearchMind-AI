import type { SVGProps } from "react";

type BrandMarkProps = Omit<SVGProps<SVGSVGElement>, "children"> & {
  /** Rendered width and height in pixels. */
  size?: number;
  /** "brand" paints the mark in the primary teal; "current" inherits the surrounding text colour. */
  tone?: "brand" | "current";
  /** Accessible name. Omit it when the mark sits next to visible brand text. */
  label?: string;
};

/**
 * The ResearchMind Lab symbol: a citation bracket holding a page, the source
 * an answer cites.
 *
 * Drawn on a 24-unit grid with whole-unit edges so it stays crisp at 24px and
 * 48px. The favicon (public/favicon.svg) is a separate 16-unit drawing snapped
 * to whole pixels for tab-bar sizes.
 */
export function BrandMark({
  size = 24,
  tone = "brand",
  label,
  className,
  ...props
}: BrandMarkProps) {
  const classes = ["shrink-0", tone === "brand" ? "text-primary" : "", className]
    .filter(Boolean)
    .join(" ");
  const accessibility = label
    ? { role: "img", "aria-label": label }
    : { "aria-hidden": true as const };

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      focusable="false"
      className={classes}
      {...accessibility}
      {...props}
    >
      <path d="M3 3h6v3H6v12h3v3H3z" />
      <path d="M11 6h7l3 3v9H11z" />
    </svg>
  );
}
