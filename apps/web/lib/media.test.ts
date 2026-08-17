import { describe, expect, it } from "vitest";
import { mediaUrl } from "./media";

describe("mediaUrl", () => {
  it("builds a stable demo media path", () => {
    expect(mediaUrl("look-01.png")).toMatch(/\/demo-media\/look-01\.png$/);
  });
});
