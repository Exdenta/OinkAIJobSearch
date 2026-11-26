// This function is deliberately self-contained: Chrome serializes it for the
// isolated world. It reads rendered DOM only and never calls a site API.
export function collectRenderedPosts(source, options = {}) {
  const limit = Math.max(1, Math.min(100, Number(options.limit) || 100));
  const detailPostId = source === "reddit" && typeof options.detailPostId === "string"
    ? options.detailPostId.toLowerCase() : "";
  const posts = [];
  const seen = new Set();
  const visible = (node) => Boolean(node && node.getClientRects().length
    && getComputedStyle(node).display !== "none" && getComputedStyle(node).visibility !== "hidden");
  const text = (node, max = 8000) => (node ? (node.innerText || node.textContent || "") : "").trim().slice(0, max);
  const isPost = (node) => node.closest('shreddit-post, .thing[data-fullname], article[data-testid="tweet"]');
  const visibleOutsidePosts = (selector) => [...document.querySelectorAll(selector)]
    .filter((node) => visible(node) && !isPost(node));
  const result = (pause = null) => ({ posts, pause, url: location.href, ready: document.readyState !== "loading" });
  if (!((source === "reddit" && ["www.reddit.com", "old.reddit.com"].includes(location.hostname))
      || (source === "x_twitter" && location.hostname === "x.com"))) {
    return result("The collection tab navigated outside its expected source.");
  }
  if (visibleOutsidePosts('iframe[src*="captcha" i], iframe[title*="challenge" i], #captcha, #px-captcha, #challenge-running, #challenge-form, .cf-challenge, input[name*="captcha" i], [data-testid*="captcha" i], form[action*="challenge" i]').length) {
    return result("CAPTCHA or browser challenge: resolve it manually, then explicitly resume this source.");
  }
  // X collection only ever opens /search; any other path means X redirected the
  // tab (e.g. logged-out users go to /i/jf/onboarding/web?mode=login).
  if (source === "x_twitter" && location.pathname !== "/search") {
    return result(`X redirected the search to ${location.pathname.slice(0, 80)}: sign in to x.com in this Chrome profile, then explicitly resume this source.`);
  }
  if (/^\/(?:login|account\/login|i\/flow\/login)(?:\/|$)/i.test(location.pathname)
      || visibleOutsidePosts('input[type="password"], [data-testid="LoginForm_Login_Button"]').length) {
    return result("Login is required: sign in manually, then explicitly resume this source.");
  }
  const notices = visibleOutsidePosts('h1, h2, [role="alert"], [role="dialog"], [aria-modal="true"], [data-testid="emptyState"], [data-testid="empty_state_header_text"], [data-testid="error-detail"], [data-testid="error-message"], #message, #error-page, .error-page, .infobar, .reddit-infobar, main > p, body > p, body > div > p');
  for (const notice of notices) {
    const message = text(notice, 1600);
    if (/captcha|verify (?:that )?you(?:'re| are) human|checking your browser|complete the security check/i.test(message)) {
      return result("CAPTCHA or browser challenge: resolve it manually, then explicitly resume this source.");
    }
    if (/rate limit (?:exceeded|reached)|too many requests|you(?:'ve| have) been blocked|blocked by (?:network security|a security)|access (?:is )?denied|request (?:has been )?blocked|whoa there, pardner|this community is (?:private|banned)|you must be invited to visit/i.test(message)) {
      return result("The site reports an access block or rate limit. Explicitly resume only after it is resolved.");
    }
    if (/(?:sign|log) in to (?:x\b|twitter\b|reddit\b)|log in or sign up to continue|you must (?:be logged|log) in/i.test(message)
        || (notice.matches('[role="dialog"], [aria-modal="true"]') && /(?:log|sign) in|create (?:an )?account/i.test(message))) {
      return result("Login is required: sign in manually, then explicitly resume this source.");
    }
  }
  const observedDate = (raw) => {
    if (typeof raw !== "string" || !raw.trim()) return "";
    let value;
    if (/^\d{10,13}(?:\.\d+)?$/.test(raw)) {
      const number = Number(raw);
      value = number < 1e12 ? number * 1000 : number;
    } else value = Date.parse(raw);
    return Number.isFinite(value) && Math.abs(value) <= 8.64e15 ? new Date(value).toISOString() : "";
  };
  const externalURL = (raw, absolute = false) => {
    try {
      const url = absolute ? new URL(raw) : new URL(raw, location.href);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password
          || /(^|\.)(?:reddit\.com|redd\.it|x\.com|twitter\.com)$/.test(url.hostname)) return "";
      return url.href.slice(0, 2000);
    } catch { return ""; }
  };
  if (source === "reddit") {
    if (detailPostId && location.pathname.match(/^\/r\/[A-Za-z0-9_]{1,21}\/comments\/([a-z0-9]+)(?:\/|$)/i)?.[1]?.toLowerCase() !== detailPostId) {
      return result();
    }
    const cards = document.querySelectorAll('shreddit-post, .thing[data-fullname^="t3_"]');
    for (const card of cards) {
      if (posts.length >= limit) break;
      if (!visible(card) || card.parentElement?.closest('shreddit-post, .thing[data-fullname]')) continue;
      const ownNodes = (selector) => [...card.querySelectorAll(selector)]
        .filter((node) => isPost(node) === card);
      const flag = (name) => card.hasAttribute(name) && !/^(?:false|0)$/i.test(card.getAttribute(name));
      if (card.matches(".stickied, .over18") || ["stickied", "is-stickied", "nsfw", "is-nsfw", "removed", "is-removed"].some(flag)
          || card.getAttribute("removed-by-category") || card.getAttribute("data-removed-by-category")) continue;
      const rawLink = card.getAttribute("permalink") || card.getAttribute("data-permalink")
        || ownNodes('a[slot="title"], a[data-click-id="comments"], a.comments, a[href*="/comments/"]')[0]?.getAttribute("href");
      let permalink;
      try { permalink = new URL(rawLink, location.href); } catch { continue; }
      if (!["www.reddit.com", "old.reddit.com", "reddit.com"].includes(permalink.hostname)
          || permalink.protocol !== "https:") continue;
      const match = permalink.pathname.match(/^\/r\/([A-Za-z0-9_]{1,21})\/comments\/([a-z0-9]+)(?:\/|$)/i);
      if (!match) continue;
      const id = match[2].toLowerCase();
      if (detailPostId && id !== detailPostId) continue;
      const observedID = card.getAttribute("id") || card.getAttribute("data-fullname") || "";
      if (/^t3_/i.test(observedID) && observedID.slice(3).toLowerCase() !== id) continue;
      const authorLink = ownNodes('a.author, a[href^="/user/"], a[href^="/u/"]')[0];
      const author = (card.getAttribute("author") || card.getAttribute("data-author")
        || authorLink?.getAttribute("href")?.match(/^\/(?:user|u)\/([^/]+)\/?$/)?.[1] || "").trim();
      const title = (card.getAttribute("post-title") || text(ownNodes('a[slot="title"], [slot="title"], a.title, h3')[0], 300)).trim().slice(0, 300);
      const body = text(ownNodes('[slot="text-body"], .usertext-body, [data-click-id="text"]').find(visible));
      if (body === "[removed]" || body === "[deleted]") continue;
      const created = observedDate(card.getAttribute("created-timestamp")
        || ownNodes("time[datetime]")[0]?.getAttribute("datetime") || "");
      if (seen.has(id) || !/^[A-Za-z0-9_-]{1,40}$/.test(author) || !created || !(title || body)) continue;
      let external = externalURL(card.getAttribute("content-href") || "");
      if (!external) {
        for (const anchor of ownNodes('a.title[href], [slot="text-body"] a[href], .usertext-body a[href]')) {
          external = externalURL(anchor.getAttribute("href") || "");
          if (external) break;
        }
      }
      seen.add(id);
      posts.push({ id, title, text: body, author, display_name: "", subreddit: match[1],
        url: `https://www.reddit.com${permalink.pathname}`, created_at: created, external_url: external });
    }
  } else {
    const quoted = (node) => node.closest('[role="link"][tabindex="0"]:not(a), [data-testid="quoteTweet"], [data-testid="quotedTweet"]');
    for (const card of document.querySelectorAll('article[data-testid="tweet"]')) {
      if (posts.length >= limit) break;
      if (!visible(card) || card.parentElement?.closest('article[data-testid="tweet"]')) continue;
      // Only the primary tweet header owns its timestamp link. A quoted card's
      // status link or timestamp must never supply the outer tweet's identity.
      const header = [...card.querySelectorAll('[data-testid="User-Name"]')].find((node) =>
        node.closest('article[data-testid="tweet"]') === card && !quoted(node));
      if (!header) continue;
      let identity = null;
      let created = "";
      for (const time of header.querySelectorAll("a[href] time[datetime]")) {
        const anchor = time.closest("a[href]");
        let url;
        try { url = new URL(anchor.getAttribute("href"), location.href); } catch { continue; }
        const match = url.pathname.match(/^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)(?:\/|$)/);
        if (url.origin === "https://x.com" && match && !quoted(time)) {
          identity = match;
          created = observedDate(time.getAttribute("datetime") || "");
          break;
        }
      }
      const bodyNode = [...card.querySelectorAll('[data-testid="tweetText"]')].find((node) =>
        node.closest('article[data-testid="tweet"]') === card && !quoted(node));
      const body = text(bodyNode);
      if (!identity || !created || !body || seen.has(identity[2])) continue;
      const [, author, id] = identity;
      const authorAnchor = [...header.querySelectorAll("a[href]")].find((anchor) => {
        try { return new URL(anchor.getAttribute("href"), location.href).pathname.toLowerCase() === `/${author.toLowerCase()}`; }
        catch { return false; }
      });
      let external = "";
      for (const anchor of bodyNode.querySelectorAll("a[href]")) {
        external = externalURL(anchor.getAttribute("data-expanded-url") || "", true)
          || externalURL(anchor.getAttribute("title") || "", true)
          || externalURL(anchor.getAttribute("href") || "");
        if (external) break;
      }
      seen.add(id);
      posts.push({ id, title: "", text: body, author, display_name: text(authorAnchor, 200), subreddit: "",
        url: `https://x.com/${author}/status/${id}`, created_at: created, external_url: external });
    }
  }
  // Capture first, scroll second: X can discard earlier cards while virtualizing.
  if (options.scroll === true && !detailPostId) window.scrollBy(0, Math.max(600, Math.round(innerHeight * 0.85)));
  return result();
}
