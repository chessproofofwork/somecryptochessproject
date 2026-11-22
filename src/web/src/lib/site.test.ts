/**
 * The five views of the one-origin web application.
 *
 * There are no hostnames to resolve any more: each view is a hash route on
 * `https://app.proofofchess.com`, and the menu switches between the four it
 * advertises. Play is intentionally route-only.
 */

import { describe, expect, it } from "vitest";

import { MENU_SITES, SITE_LABELS, SITE_TITLES, siteHash, type Site } from "./site";

const SITES: Site[] = ["explorer", "wallet", "play", "dex", "bridge"];

describe("the five views", () => {
  it("lands each view on its own hash route", () => {
    expect(siteHash("explorer")).toBe("#/");
    expect(siteHash("wallet")).toBe("#/wallet");
    expect(siteHash("play")).toBe("#/play");
    expect(siteHash("dex")).toBe("#/dex");
    expect(siteHash("bridge")).toBe("#/bridge");
  });

  it("gives every view a distinct, same-origin route", () => {
    const hashes = SITES.map(siteHash);
    expect(new Set(hashes).size).toBe(SITES.length);
    for (const hash of hashes) expect(hash.startsWith("#/")).toBe(true);
  });

  it("keeps Play out of the menu while leaving its route live", () => {
    expect(MENU_SITES).toEqual(["explorer", "wallet", "dex", "bridge"]);
    expect(MENU_SITES).not.toContain("play");
    expect(siteHash("play")).toBe("#/play");
  });

  it("labels and titles every view", () => {
    for (const site of SITES) {
      expect(SITE_LABELS[site]).not.toBe("");
      expect(SITE_TITLES[site].startsWith("ZQWX ")).toBe(true);
    }
  });
});
