#!/usr/bin/env node
/**
 * Screenshot each slide from an HTML slide deck.
 *
 * Usage: node screenshot_slides.js <slides.html> <output_dir>
 *
 * Produces output_dir/slide_01.png, slide_02.png, etc. at 1920x1080.
 * Requires: npm install playwright && npx playwright install chromium
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const htmlFile = process.argv[2];
  const outDir = process.argv[3];
  if (!htmlFile || !outDir) {
    console.error('Usage: node screenshot_slides.js <slides.html> <output_dir>');
    process.exit(1);
  }

  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1920, height: 1080 });

  await page.goto(`file://${path.resolve(htmlFile)}`);
  await page.waitForTimeout(1500); // let fonts load

  const slides = await page.$$('[id^="slide"]');
  for (let i = 0; i < slides.length; i++) {
    const slideId = `slide${i + 1}`;
    const el = await page.$(`#${slideId}`);
    if (el) {
      const padded = String(i + 1).padStart(2, '0');
      await el.screenshot({ path: path.join(outDir, `slide_${padded}.png`) });
    }
  }

  await browser.close();
  console.log(`${slides.length} slides captured -> ${outDir}`);
})();
