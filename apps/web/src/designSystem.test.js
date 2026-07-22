import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const productCss = [
  "src/styles.css",
  "src/styles/adaptive-shell.css",
  "src/styles/onboarding.css",
  "src/styles/product-workspace.css"
].map((path) => readFileSync(resolve(process.cwd(), path), "utf8")).join("\n");

describe("Corvus precision visual system", () => {
  it("uses direct solid surfaces without decorative effects", () => {
    expect(productCss).not.toMatch(/(?:linear|radial|conic)-gradient\s*\(/i);
    expect(productCss).not.toMatch(/backdrop-filter\s*:/i);
    expect(productCss).not.toMatch(/box-shadow\s*:/i);
    expect(productCss).not.toMatch(/translateY\s*\(\s*-1px\s*\)/i);
  });

  it("does not use fully rounded pills as the default product vocabulary", () => {
    expect(productCss).not.toMatch(/border-radius\s*:\s*999px/i);
  });
});
