/**
 * No mojibake in the frontend sources.
 *
 * The dashboard once rendered "View all" followed by three garbage
 * characters instead of a right arrow: the UTF-8 bytes of U+2192 (E2 86 92)
 * had been decoded as Windows-1252 and saved back as U+00E2 U+2020 U+2019.
 * This test scans every text source under src/ for the patterns that kind
 * of corruption leaves behind, so it fails in CI instead of reaching users.
 *
 * Detector: the Windows-1252 character for a common UTF-8 lead byte
 * (C2, C3, E2 or F0) immediately followed by a character that a UTF-8
 * continuation byte (80-BF) becomes under Windows-1252, or the Unicode
 * replacement character U+FFFD. Real text (an umlaut, a copyright sign, a
 * middle dot, an arrow) does not match.
 *
 * Every non-ASCII character in this file is written as a code point
 * number, so the file is pure ASCII and can never match itself.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const text = (...codePoints: number[]) => String.fromCodePoint(...codePoints);

// UTF-8 lead bytes C2, C3, E2, F0 read as Windows-1252.
const LEADS = new Set([0xc2, 0xc3, 0xe2, 0xf0]);

// Bytes 0x80-0x9F read as Windows-1252 (the five undefined bytes become
// the matching C1 control, as browsers and TextDecoder do); 0xA0-0xBF map
// to themselves.
const CP1252_80_9F = [
  0x20ac, 0x0081, 0x201a, 0x0192, 0x201e, 0x2026, 0x2020, 0x2021,
  0x02c6, 0x2030, 0x0160, 0x2039, 0x0152, 0x008d, 0x017d, 0x008f,
  0x0090, 0x2018, 0x2019, 0x201c, 0x201d, 0x2022, 0x2013, 0x2014,
  0x02dc, 0x2122, 0x0161, 0x203a, 0x0153, 0x009d, 0x017e, 0x0178,
];
const CONTINUATIONS = new Set([
  ...CP1252_80_9F,
  ...Array.from({ length: 0x20 }, (_, i) => 0xa0 + i),
]);

const REPLACEMENT_CHARACTER = 0xfffd;

function hasMojibake(line: string): boolean {
  const points = Array.from(line, (ch) => ch.codePointAt(0) as number);
  return points.some(
    (point, i) =>
      point === REPLACEMENT_CHARACTER ||
      (LEADS.has(point) && i + 1 < points.length && CONTINUATIONS.has(points[i + 1])),
  );
}

const TEXT_EXTENSIONS = new Set([".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".json", ".md"]);

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return TEXT_EXTENSIONS.has(path.extname(name)) ? [full] : [];
  });
}

describe("source encoding", () => {
  it("the detector catches the known corruption and spares real text", () => {
    // The dashboard bug: "View all " + mis-decoded U+2192.
    expect(hasMojibake("View all " + text(0xe2, 0x2020, 0x2019))).toBe(true);
    expect(hasMojibake(text(0xe2, 0x20ac, 0x201d))).toBe(true); // mis-decoded em dash
    expect(hasMojibake("caf" + text(0xc3, 0xa9))).toBe(true); // mis-decoded e-acute
    expect(hasMojibake("a " + text(0xc2, 0xb7) + " b")).toBe(true); // mis-decoded middle dot
    expect(hasMojibake(text(REPLACEMENT_CHARACTER))).toBe(true);

    const fine = [
      "View all " + text(0x2192),
      "Z" + text(0xfc) + "rich",
      "TU M" + text(0xfc) + "nchen",
      text(0xa9) + " 2026",
      "a " + text(0xb7) + " b",
      "caf" + text(0xe9),
      "Line two " + text(0x2013) + " final",
    ];
    for (const line of fine) {
      expect(hasMojibake(line), line).toBe(false);
    }
  });

  it("no file under src/ contains mojibake", () => {
    const files = sourceFiles(SRC_DIR);
    expect(files.length).toBeGreaterThan(20); // the walk really found the sources

    const offenders: string[] = [];
    for (const file of files) {
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, index) => {
          if (hasMojibake(line)) {
            offenders.push(`${path.relative(SRC_DIR, file)}:${index + 1}: ${line.trim()}`);
          }
        });
    }
    expect(offenders).toEqual([]);
  });
});
