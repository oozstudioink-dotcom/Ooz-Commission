// node render.js <page.html> <outDir> <hash> [width height seconds]
const { chromium } = require('playwright'); const path = require('path'); const fs = require('fs');
(async () => {
  const [html, outDir, hash, w = '800', h = '450', secs = '4'] = process.argv.slice(2);
  fs.mkdirSync(outDir, { recursive: true });
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: +w, height: +h } });
  await p.goto('file://' + html + '#' + hash);
  await p.evaluate(async () => { await document.fonts.ready; if (window.ready) await window.ready; });
  await p.evaluate(() => document.getAnimations().forEach(a => a.pause()));
  const fps = 30;
  for (let i = 0; i < fps * +secs; i++) {
    await p.evaluate(t => document.getAnimations().forEach(a => { a.currentTime = t; }), i * 1000 / fps);
    await p.screenshot({ path: path.join(outDir, String(i).padStart(4, '0') + '.png'), omitBackground: true });
  }
  await b.close();
})();
