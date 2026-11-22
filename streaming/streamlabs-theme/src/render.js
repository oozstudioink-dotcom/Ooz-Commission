const { chromium } = require('playwright'); const path = require('path'); const fs = require('fs');
(async () => {
  const [html, outDir, title] = process.argv.slice(2);
  fs.mkdirSync(outDir, { recursive: true });
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 640, height: 360 } });
  await p.goto('file://' + html + '#' + encodeURIComponent(title)); await p.evaluate(() => document.fonts.ready);
  await p.evaluate(() => document.getAnimations().forEach(a => a.pause()));
  const fps = 30, secs = 3.6;
  for (let i = 0; i < fps * secs; i++) {
    const t = i * 1000 / fps;
    await p.evaluate(t => document.getAnimations().forEach(a => { a.currentTime = t; }), t);
    await p.screenshot({ path: path.join(outDir, String(i).padStart(4, '0') + '.png'), omitBackground: true });
  }
  await b.close();
})();
