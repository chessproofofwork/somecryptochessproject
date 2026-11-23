/** The description is off-chain editorial data, so its lookup is pinned here. */
import { describe, expect, it } from "vitest";

import { tokenDescription } from "./tokenDescriptions";
import type { TokenView } from "../types";

const C960 = {
  name: "token-0960",
  ticket: "0960",
  token_id: "3f7df4bfbdf50d69f4d2b08777358d15eab68e62d0c19fd9f2e9c32f27289243",
} as TokenView;

describe("token descriptions", () => {
  it("finds C960 by ticket, internal name and token id", () => {
    expect(tokenDescription(C960)?.body).toContain("Chess960");
    expect(
      tokenDescription({ ...C960, name: "other", token_id: "cd".repeat(32) })?.body,
    ).toContain("Chess960");
    expect(
      tokenDescription({ ...C960, ticket: null, token_id: "ef".repeat(32) })?.body,
    ).toContain("Chess960");
  });

  it("does not invent prose for an unknown token", () => {
    expect(
      tokenDescription({ ...C960, name: "token-0042", ticket: "0042", token_id: "ab".repeat(32) }),
    ).toBeUndefined();
  });
});
