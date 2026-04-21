#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

function isoDateDaysAgo(days) {
  const now = new Date();
  now.setHours(0, 0, 0, 0);
  now.setDate(now.getDate() - days);
  return now.toISOString().slice(0, 10);
}

function loadNote(filePath) {
  const text = fs.readFileSync(filePath, "utf8");
  if (!text.startsWith("---\n")) {
    throw new Error(`${filePath} does not start with frontmatter`);
  }
  const rest = text.slice(4);
  const splitIdx = rest.indexOf("\n---\n");
  if (splitIdx === -1) {
    throw new Error(`${filePath} is missing closing frontmatter fence`);
  }
  const frontmatter = rest.slice(0, splitIdx);
  const body = rest.slice(splitIdx + 5);
  return { text, frontmatter, body };
}

function parseSimpleFrontmatter(frontmatter) {
  const data = {};
  let currentListKey = null;
  for (const rawLine of frontmatter.split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    if (!line.trim()) {
      continue;
    }
    if (line.startsWith("- ") && currentListKey) {
      data[currentListKey] ??= [];
      data[currentListKey].push(line.slice(2).trim().replace(/^['"]|['"]$/g, ""));
      continue;
    }
    const match = line.match(/^([A-Za-z0-9_]+):\s*(.*)$/);
    if (!match) {
      continue;
    }
    const [, key, valueRaw] = match;
    let value = valueRaw.trim();
    currentListKey = null;
    if (value === "") {
      currentListKey = key;
      data[key] = [];
      continue;
    }
    if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
      value = value.slice(1, -1);
    }
    data[key] = value;
  }
  return data;
}

function cleanText(text) {
  return String(text || "")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1")
    .replace(/\*\*/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function isBoilerplate(text) {
  const cleaned = cleanText(text).toLowerCase();
  if (!cleaned) {
    return true;
  }
  const parenCount = (cleaned.match(/\(/g) || []).length;
  return (
    cleaned.includes("by clicking continue to join or sign in") ||
    cleaned.includes("you agree to linkedin") ||
    cleaned.includes("sign in to view more content") ||
    cleaned.includes("join now to view more content") ||
    cleaned.includes("create your free account or sign in") ||
    cleaned.includes("explore content categories") ||
    cleaned.includes("more from this author") ||
    cleaned.includes("skip to main content") ||
    cleaned.includes("cookie policy") ||
    cleaned.includes("privacy policy") ||
    cleaned.includes("terms of service") ||
    cleaned.includes("available mtproto servers") ||
    cleaned.includes("log in") ||
    cleaned.includes("sign up") ||
    cleaned.includes("open app") ||
    cleaned.includes("download app") ||
    cleaned.includes("javascript is not available") ||
    cleaned.includes("wait a moment and try again") ||
    cleaned.includes("enable javascript") ||
    cleaned.includes("create account") ||
    parenCount >= 5 ||
    /^[a-z\u00c0-\u024f\s]+\([a-z\u00c0-\u024f\s]+\)(\s+[a-z\u00c0-\u024f\s]+\([a-z\u00c0-\u024f\s]+\))+$/i.test(cleaned)
  );
}

function truncate(text, limit = 220) {
  const cleaned = cleanText(text);
  if (cleaned.length <= limit) {
    return cleaned;
  }
  return `${cleaned.slice(0, limit - 3).trimEnd()}...`;
}

function dedupe(lines, limit = 6, minLength = 20) {
  const out = [];
  const seen = new Set();
  for (const line of lines) {
    const cleaned = truncate(line);
    if (cleaned.length < minLength || isBoilerplate(cleaned)) {
      continue;
    }
    const key = cleaned.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    if (!key || seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(cleaned);
    if (out.length >= limit) {
      break;
    }
  }
  return out;
}

function replaceOrInsertSection(body, heading, bullets, beforeHeading = "Source Context") {
  const normalizedBullets = dedupe(
    bullets.map((bullet) => cleanText(bullet)).filter(Boolean),
    Math.max(1, bullets.length),
    1,
  );
  const section = normalizedBullets.length
    ? `## ${heading}\n\n${normalizedBullets.map((bullet) => `- ${bullet}`).join("\n")}\n`
    : "";
  const regex = new RegExp(`\\n## ${heading.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\n\\n[\\s\\S]*?(?=\\n## |$)`);
  if (regex.test(body)) {
    const replaced = body.replace(regex, section ? `\n${section}` : "");
    return replaced.replace(/\n{3,}/g, "\n\n").trimEnd() + "\n";
  }
  if (!section) {
    return body;
  }
  const anchor = `\n## ${beforeHeading}\n`;
  if (body.includes(anchor)) {
    return body.replace(anchor, `\n${section}\n${anchor}`).replace(/\n{3,}/g, "\n\n").trimEnd() + "\n";
  }
  return `${body.trimEnd()}\n\n${section}`;
}

function renderNote(frontmatter, body) {
  return `---\n${frontmatter}\n---\n${body.startsWith("\n") ? body : `\n${body}`}`;
}

async function extractGeneric(page) {
  return await page.evaluate(() => {
    const text = (value) => (value || "").replace(/\s+/g, " ").trim();
    const title =
      text(document.querySelector('meta[property="og:title"]')?.getAttribute("content")) ||
      text(document.title);
    const description =
      text(document.querySelector('meta[property="og:description"]')?.getAttribute("content")) ||
      text(document.querySelector('meta[name="description"]')?.getAttribute("content"));
    const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
      .map((el) => text(el.textContent))
      .filter(Boolean)
      .slice(0, 12);
    const paragraphs = Array.from(document.querySelectorAll("p,li"))
      .map((el) => text(el.textContent))
      .filter((line) => line.length >= 40)
      .slice(0, 60);
    return { title, description, headings, paragraphs };
  });
}

async function extractX(page, url) {
  return await page.evaluate((tweetUrl) => {
    const text = (value) => (value || "").replace(/\s+/g, " ").trim();
    const tweetText =
      Array.from(document.querySelectorAll('[data-testid="tweetText"]'))
        .map((el) => text(el.textContent))
        .filter(Boolean)[0] || "";
    const title =
      text(document.querySelector('meta[property="og:title"]')?.getAttribute("content")) ||
      text(document.title);
    const description =
      text(document.querySelector('meta[property="og:description"]')?.getAttribute("content")) ||
      text(document.querySelector('meta[name="description"]')?.getAttribute("content"));
    const pathParts = new URL(tweetUrl).pathname.split("/").filter(Boolean);
    const authorHandle = pathParts.length ? `@${pathParts[0]}` : "";
    return { title, description, tweetText, authorHandle };
  }, url);
}

function hasThinRetrievedContext(body) {
  const match = body.match(/\n## Retrieved Context\n\n([\s\S]*?)(?=\n## |$)/m);
  if (!match) {
    return true;
  }
  const bullets = match[1]
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("- "))
    .map((line) => line.slice(2).trim())
    .filter(Boolean);
  if (bullets.length === 0) {
    return true;
  }
  const joined = bullets.join(" ").toLowerCase();
  return (
    bullets.length < 2 ||
    joined.includes("reference page saved as:") ||
    joined.includes("github reference to repository") ||
    joined.includes("(403).") ||
    joined.includes("(404).")
  );
}

function needsBrowserPass(note, data = parseSimpleFrontmatter(note.frontmatter)) {
  const body = note.body;
  const type = String(data.type || "").toLowerCase();
  return (
    type === "tweet" ||
    body.includes("(403).") ||
    body.includes("(404).") ||
    body.includes("Reference page saved as:") ||
    body.includes("GitHub reference to repository") ||
    body.includes("fetch_failed:") ||
    body.includes("## Source Context") && hasThinRetrievedContext(body)
  );
}

function discoveredOn(frontmatter) {
  const data = parseSimpleFrontmatter(frontmatter);
  return data.discovered_on || "";
}

async function enrichNote(browser, filePath) {
  const note = loadNote(filePath);
  const data = parseSimpleFrontmatter(note.frontmatter);
  const url = data.url || "";
  if (!url) {
    return { changed: false, filePath, reason: "missing_url" };
  }
  if (!needsBrowserPass(note, data)) {
    return { changed: false, filePath, reason: "not_targeted" };
  }

  const page = await browser.newPage();
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(2500);
    const isX = /https?:\/\/(www\.)?(x\.com|twitter\.com)\//i.test(url);
    let summary = [];
    let signals = [];
    let browserLines = [];

    if (isX) {
      const extracted = await extractX(page, url);
      if (extracted.tweetText) {
        summary.push(`Post text: ${extracted.tweetText}`);
      }
      if (extracted.description && extracted.description !== extracted.tweetText) {
        summary.push(`Post summary: ${extracted.description}`);
      }
      if (extracted.authorHandle) {
        signals.push(`Author handle: ${extracted.authorHandle}`);
      }
      if (extracted.title) {
        signals.push(`Page title: ${extracted.title}`);
      }
      browserLines = [
        `Browser checked page on ${new Date().toISOString().slice(0, 10)}.`,
        `Browser URL: ${page.url()}`,
      ];
    } else {
      const extracted = await extractGeneric(page);
      if (extracted.description) {
        summary.push(extracted.description);
      }
      summary.push(...extracted.paragraphs.slice(0, 4));
      signals.push(...extracted.headings.slice(0, 6).map((line) => `Page section: ${line}`));
      if (extracted.title) {
        browserLines = [
          `Browser checked page on ${new Date().toISOString().slice(0, 10)}.`,
          `Browser URL: ${page.url()}`,
          `Browser title: ${truncate(extracted.title)}`,
        ];
      } else {
        browserLines = [
          `Browser checked page on ${new Date().toISOString().slice(0, 10)}.`,
          `Browser URL: ${page.url()}`,
        ];
      }
    }

    summary = dedupe(summary, 4, 30);
    signals = dedupe(signals, 6, 12);
    if (!summary.length && !signals.length) {
      return { changed: false, filePath, reason: "no_browser_signal" };
    }

    let body = note.body;
    body = replaceOrInsertSection(body, "Retrieved Context", summary);
    body = replaceOrInsertSection(body, "What's In It", signals);
    body = replaceOrInsertSection(body, "Browser Enrichment", browserLines, "Linked Topics");
    const nextText = renderNote(note.frontmatter, body);
    if (nextText !== note.text) {
      fs.writeFileSync(filePath, nextText);
      return { changed: true, filePath, reason: "enriched", summaryCount: summary.length, signalCount: signals.length };
    }
    return { changed: false, filePath, reason: "no_diff" };
  } catch (error) {
    return { changed: false, filePath, reason: `error:${error instanceof Error ? error.message : String(error)}` };
  } finally {
    await page.close();
  }
}

function candidateFiles(vaultRoot) {
  const lookbackDays = Math.max(0, Number(process.argv[5] || "30"));
  const earliest = isoDateDaysAgo(lookbackDays);
  const folders = ["items/tweets", "items/resources", "items/articles"];
  const files = [];
  for (const folder of folders) {
    const dir = path.join(vaultRoot, folder);
    if (!fs.existsSync(dir)) continue;
    for (const name of fs.readdirSync(dir)) {
      if (!name.endsWith(".md")) continue;
      const filePath = path.join(dir, name);
      const note = loadNote(filePath);
      const data = parseSimpleFrontmatter(note.frontmatter);
      if (!needsBrowserPass(note, data)) continue;
      const discovered = discoveredOn(note.frontmatter);
      if (discovered && discovered < earliest) continue;
      files.push({ filePath, discoveredOn: discovered });
    }
  }
  files.sort((a, b) => String(b.discoveredOn).localeCompare(String(a.discoveredOn)) || a.filePath.localeCompare(b.filePath));
  return files.map((entry) => entry.filePath);
}

async function main() {
  const vaultRoot = process.argv[2] ? path.resolve(process.argv[2]) : process.cwd();
  const limit = Number(process.argv[3] || "60");
  const concurrency = Math.max(1, Number(process.argv[4] || "4"));
  const lookbackDays = Math.max(0, Number(process.argv[5] || "30"));
  const browser = await chromium.launch({ headless: true });
  const files = candidateFiles(vaultRoot).slice(0, limit);
  const results = new Array(files.length);
  let nextIndex = 0;

  async function worker() {
    while (true) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= files.length) {
        return;
      }
      results[index] = await enrichNote(browser, files[index]);
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, files.length) }, () => worker()));
  await browser.close();
  const changed = results.filter((result) => result.changed).length;
  process.stdout.write(JSON.stringify({ checked: results.length, changed, lookbackDays, results }, null, 2) + "\n");
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack || error.message : String(error)}\n`);
  process.exit(1);
});
