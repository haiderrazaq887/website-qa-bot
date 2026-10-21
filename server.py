"""Website QA server: one MCP tool, run_website_qa(url).

Loads a page in a real headless browser (Chromium via Playwright) at four screen
widths, measures things, draws red boxes around problems, saves screenshots,
checks links, and returns everything as JSON for Claude to turn into a report.
"""
import asyncio
import ipaddress
import json
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

PORT = int(os.environ.get("PORT", "8000"))
SECRET = os.environ.get("MCP_SECRET", "change-me")  # part of the connector URL
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # e.g. https://my-qa.onrender.com
SHOT_DIR = Path(os.environ.get("SHOT_DIR", "/tmp/shots"))
SHOT_DIR.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [("mobile", 375, 812), ("tablet", 768, 1024),
             ("laptop", 1280, 800), ("desktop", 1920, 1080)]
MAX_LINKS = 150

mcp = FastMCP(
    "website-qa",
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    streamable_http_path=f"/mcp/{SECRET}",
)


# ---------- safety: do not let anyone point the tool at private networks ----------
def check_url_is_public(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Please give a full URL starting with http:// or https://")
    for info in socket.getaddrinfo(p.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("That address is not a public website.")


# ---------- JavaScript that runs inside the page ----------
FIND_ISSUES_JS = r"""
() => {
  const out = {h1: [], fonts: {}, loadedFonts: [], images: [], layout: [], overflowX: false, flagged: []};
  const vw = document.documentElement.clientWidth;
  const sel = (el) => {
    if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
    let s = el.tagName.toLowerCase();
    if (el.classList.length) s += '.' + [...el.classList].slice(0, 2).join('.');
    return s;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const flag = (el, label, kind, detail) => {
    const r = el.getBoundingClientRect();
    const n = out.flagged.length + 1;
    out.flagged.push({n, kind, label, detail, selector: sel(el),
      x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height});
    el.setAttribute('data-qa-n', n);
  };

  // H1
  document.querySelectorAll('h1').forEach(h => out.h1.push((h.innerText || '').trim()));

  // Horizontal scroll
  out.overflowX = document.documentElement.scrollWidth > vw + 1;

  // Fonts used by visible text
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode;
    if (!t.textContent.trim() || !t.parentElement || !visible(t.parentElement)) continue;
    const cs = getComputedStyle(t.parentElement);
    const first = cs.fontFamily.split(',')[0].replace(/["']/g, '').trim();
    const key = first + ' | stack: ' + cs.fontFamily;
    out.fonts[key] = (out.fonts[key] || 0) + t.textContent.trim().length;
  }
  try { document.fonts.forEach(f => { if (f.status === 'loaded') out.loadedFonts.push(f.family.replace(/["']/g, '') + ' ' + f.weight); }); } catch (e) {}

  // Elements wider than the screen
  document.querySelectorAll('body *').forEach(el => {
    if (!visible(el)) return;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (cs.position === 'fixed') return;
    if (r.right > vw + 2 && r.width <= vw * 3 && out.flagged.length < 40) {
      let p = el.parentElement, clipped = false;
      while (p && p !== document.body) {
        const o = getComputedStyle(p).overflowX;
        if (o === 'hidden' || o === 'auto' || o === 'scroll' || o === 'clip') { clipped = true; break; }
        p = p.parentElement;
      }
      if (!clipped) flag(el, 'Element goes past the screen edge', 'responsiveness',
        'Right edge at ' + Math.round(r.right) + 'px but screen is ' + vw + 'px wide');
    }
  });

  // Images
  document.querySelectorAll('img').forEach(img => {
    if (!visible(img) && !(img.complete && img.naturalWidth === 0)) return;
    const r = img.getBoundingClientRect(), cs = getComputedStyle(img);
    const src = img.currentSrc || img.src;
    if (img.complete && img.naturalWidth === 0) {
      out.images.push({src, problem: 'broken image (did not load)'});
      if (r.width > 0) flag(img, 'Broken image', 'image', src);
      return;
    }
    if (!img.naturalWidth || r.width < 2) return;
    const nat = img.naturalWidth / img.naturalHeight, ren = r.width / r.height;
    const diff = Math.abs(nat - ren) / nat;
    const fit = cs.objectFit;
    if (diff > 0.08) {
      if (fit === 'cover') {
        const shownPct = Math.round(100 * Math.min(nat / ren, ren / nat));
        if (shownPct < 85) {
          out.images.push({src, problem: 'cropped: about ' + (100 - shownPct) + '% of the image is cut off (object-fit: cover)'});
          flag(img, 'Image cropped', 'image', 'About ' + (100 - shownPct) + '% of the picture is hidden. ' + src);
        }
      } else if (fit === 'fill' || fit === '') {
        out.images.push({src, problem: 'stretched/squashed: original ratio ' + nat.toFixed(2) + ', shown at ' + ren.toFixed(2)});
        flag(img, 'Image distorted', 'image', 'Original ratio ' + nat.toFixed(2) + ' shown at ' + ren.toFixed(2) + '. ' + src);
      }
    }
    // image sticking out of / clipped by its container
    const p = img.parentElement;
    if (p) {
      const pr = p.getBoundingClientRect(), po = getComputedStyle(p);
      if (po.overflow !== 'visible' && (r.right > pr.right + 3 || r.left < pr.left - 3 || r.bottom > pr.bottom + 3 || r.top < pr.top - 3)) {
        out.images.push({src, problem: 'cut off by its container box'});
        flag(img, 'Image cut off by container', 'image', src);
      }
    }
    if (r.width > img.naturalWidth * 1.6) {
      out.images.push({src, problem: 'low resolution: shown at ' + Math.round(r.width) + 'px but file is only ' + img.naturalWidth + 'px wide (may look blurry)'});
    }
    if (!img.alt) out.images.push({src, problem: 'missing alt text'});
  });

  // Spacing / alignment heuristics
  document.querySelectorAll('body *').forEach(el => {
    if (out.flagged.length >= 40 || !visible(el)) return;
    const cs = getComputedStyle(el);
    // text spilling out of its own box
    if (el.children.length === 0 && el.innerText && el.innerText.trim().length > 0 &&
        cs.overflow === 'visible' && el.scrollWidth > el.clientWidth + 4 && el.clientWidth > 0) {
      flag(el, 'Text spills outside its box', 'spacing', 'Content is ' + el.scrollWidth + 'px wide inside a ' + el.clientWidth + 'px box');
    }
  });
  document.querySelectorAll('body *').forEach(parent => {
    if (out.flagged.length >= 40) return;
    const pcs = getComputedStyle(parent);
    const kids = [...parent.children].filter(k => visible(k) && ['static', 'relative'].includes(getComputedStyle(k).position));
    if (kids.length < 2 || kids.length > 12) return;
    const rows = pcs.display === 'flex' && pcs.flexDirection.startsWith('row') && pcs.alignItems !== 'center'
                 && pcs.flexWrap === 'nowrap';
    if (rows) {
      const tops = kids.map(k => k.getBoundingClientRect().top);
      const spread = Math.max(...tops) - Math.min(...tops);
      if (spread > 1 && spread <= 12) flag(parent, 'Items in a row are slightly misaligned', 'alignment', 'Tops differ by ' + spread.toFixed(1) + 'px');
    }
    // overlapping siblings (both contain text)
    for (let i = 0; i < kids.length - 1; i++) {
      const a = kids[i].getBoundingClientRect(), b = kids[i + 1].getBoundingClientRect();
      const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (ox > 6 && oy > 6 && kids[i].innerText && kids[i + 1].innerText &&
          kids[i].innerText.trim() && kids[i + 1].innerText.trim()) {
        flag(kids[i + 1], 'Overlaps the element before it', 'spacing', 'Overlap ' + Math.round(ox) + 'x' + Math.round(oy) + 'px');
        break;
      }
    }
  });
  return out;
}
"""

DRAW_BOXES_JS = r"""
(items) => {
  items.forEach(it => {
    const d = document.createElement('div');
    d.style.cssText = `position:absolute;left:${it.x}px;top:${it.y}px;width:${it.w}px;height:${it.h}px;` +
      'border:3px solid #ff0033;background:rgba(255,0,51,0.08);z-index:2147483647;pointer-events:none;box-sizing:border-box;';
    const t = document.createElement('div');
    t.textContent = '#' + it.n;
    t.style.cssText = 'position:absolute;left:0;top:-18px;background:#ff0033;color:#fff;font:bold 12px Arial;padding:1px 5px;';
    d.appendChild(t); document.body.appendChild(d);
  });
}
"""

PAGE_INFO_JS = r"""
() => ({
  title: document.title,
  lang: document.documentElement.lang,
  metaDescription: (document.querySelector('meta[name=description]') || {}).content || '',
  text: document.body.innerText,
  links: [...document.querySelectorAll('a[href]')].map(a => ({href: a.href, text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 60)})),
})
"""


async def check_links(page_url: str, links: list[dict]) -> dict:
    host = urlparse(page_url).netloc
    seen, todo = set(), []
    for l in links:
        href = l["href"].split("#")[0]
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        todo.append({"url": href, "text": l["text"], "type": "internal" if urlparse(href).netloc == host else "external"})
    skipped = max(0, len(todo) - MAX_LINKS)
    todo = todo[:MAX_LINKS]
    sem = asyncio.Semaphore(10)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WebsiteQABot/1.0)"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers=headers) as client:
        async def one(item):
            async with sem:
                try:
                    check_url_is_public(item["url"])
                    r = await client.head(item["url"])
                    if r.status_code >= 400 or r.status_code == 405:
                        r = await client.get(item["url"])
                    item["status"] = r.status_code
                    item["final_url"] = str(r.url) if str(r.url) != item["url"] else None
                except Exception as e:  # noqa: BLE001
                    item["status"] = None
                    item["error"] = type(e).__name__
            return item
        results = await asyncio.gather(*[one(i) for i in todo])

    bad = [r for r in results if r["status"] is None or r["status"] >= 400]
    return {"checked": len(results), "not_checked_over_limit": skipped,
            "broken_or_failing": bad,
            "note": "403/429 on external sites is often bot-blocking, not a truly broken link; verify manually."}


@mcp.tool()
async def run_website_qa(url: str) -> str:
    """Run a QA audit on a live public web page.

    Opens the page in a real browser at 375, 768, 1280 and 1920 px wide and returns JSON with:
    H1 headings, fonts actually used, responsiveness problems, image problems (broken, cropped,
    stretched, blurry), spacing/alignment suspects, link status for every link, the visible page
    text (for spelling/grammar review), and screenshot URLs with red numbered boxes on problems.
    """
    check_url_is_public(url)
    run_id = uuid.uuid4().hex[:10]
    report = {"url": url, "viewports": {}, "notes": []}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        info = None
        all_fonts, all_h1 = {}, None
        for name, w, h in VIEWPORTS:
            ctx = await browser.new_context(viewport={"width": w, "height": h})
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=45000)
            except Exception:
                try:
                    await page.goto(url, wait_until="load", timeout=45000)
                    report["notes"].append(f"{name}: page never fully went quiet (network), results may be partial")
                except Exception as e:  # noqa: BLE001
                    report["viewports"][name] = {"error": f"Could not load page: {type(e).__name__}"}
                    await ctx.close()
                    continue
            # scroll to trigger lazy-loaded images, then go back to top
            await page.evaluate("async()=>{for(let y=0;y<document.body.scrollHeight;y+=600){window.scrollTo(0,y);await new Promise(r=>setTimeout(r,120));}window.scrollTo(0,0);}")
            await page.wait_for_timeout(500)

            found = await page.evaluate(FIND_ISSUES_JS)
            if info is None:
                info = await page.evaluate(PAGE_INFO_JS)
            if all_h1 is None:
                all_h1 = found["h1"]
            for k, v in found["fonts"].items():
                all_fonts[k] = all_fonts.get(k, 0) + v

            shot_url = None
            if found["flagged"]:
                await page.evaluate(DRAW_BOXES_JS, found["flagged"])
            fname = f"{run_id}-{name}.jpg"
            try:
                await page.screenshot(path=str(SHOT_DIR / fname), full_page=True, type="jpeg", quality=70)
                shot_url = f"{PUBLIC_URL}/shots/{fname}" if PUBLIC_URL else f"/shots/{fname}"
            except Exception as e:  # noqa: BLE001
                report["notes"].append(f"{name}: screenshot failed ({type(e).__name__})")

            report["viewports"][name] = {
                "width": w,
                "horizontal_scroll_bar_present": found["overflowX"],
                "screenshot_with_red_boxes": shot_url,
                "issues": [{"id": f"#{i['n']}", "kind": i["kind"], "what": i["label"], "detail": i["detail"],
                            "element": i["selector"]} for i in found["flagged"]],
                "image_findings": found["images"][:40],
            }
            await ctx.close()
        await browser.close()

    if info:
        report["page"] = {"title": info["title"], "language_attribute": info["lang"],
                          "meta_description": info["metaDescription"]}
        text = info["text"]
        report["visible_text_for_proofreading"] = text[:20000]
        if len(text) > 20000:
            report["notes"].append("Page text was longer than 20,000 characters; the rest was not included for proofreading.")
        report["links"] = await check_links(url, info["links"])
    report["h1"] = {"count": len(all_h1 or []), "texts": all_h1 or []}
    report["fonts_used_by_visible_text"] = [
        {"font_first_choice_and_stack": k, "characters": v}
        for k, v in sorted(all_fonts.items(), key=lambda kv: -kv[1])
    ]
    return json.dumps(report, indent=1)


@mcp.custom_route("/shots/{name}", methods=["GET"])
async def serve_shot(request):
    name = Path(request.path_params["name"]).name  # strips any ../ tricks
    path = SHOT_DIR / name
    if not path.exists():
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@mcp.custom_route("/", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
