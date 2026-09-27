/**
 * Keenr.ai - UI Component Renderers
 */

const SOURCE_LABELS = {
  sec_edgar: '<i class="fa-solid fa-landmark"></i> SEC EDGAR',
  news_rss: '<i class="fa-solid fa-newspaper"></i> News RSS',
  exa_news: '<i class="fa-solid fa-magnifying-glass-plus"></i> Exa News',
  ir_press: '<i class="fa-solid fa-building-columns"></i> IR Press',
  earnings_deck: '<i class="fa-solid fa-chart-pie"></i> Earnings Deck',
  career_pages: '<i class="fa-solid fa-briefcase"></i> Career Pages',
  patents: '<i class="fa-solid fa-lightbulb"></i> Patents'
};

function sourceLabel(sourceType) {
  return SOURCE_LABELS[sourceType] || `<i class="fa-solid fa-circle-question"></i> ${escapeHtml(sourceType)}`;
}

// Strength bands on the points scale (see backend/app/signals/scoring.py):
// 3 per company statement, 2 per job posting, 1 per news story.
function scoreBand(points) {
  if (points >= 6) return { cls: 'score-hot', label: 'High' };
  if (points >= 3) return { cls: 'score-warm', label: 'Medium' };
  return { cls: 'score-watch', label: 'Low' };
}

/**
 * News RSS titles arrive as "[Publisher] Actual headline - Publisher".
 * Split the outlet off so the card can show it as its own byline, and strip
 * the trailing " - Publisher" duplication feeds tack on.
 */
function splitHeadline(raw) {
  if (!raw) return { publisher: '', headline: '' };
  let headline = String(raw).trim();
  let publisher = '';
  if (headline.startsWith('[')) {
    const close = headline.indexOf(']');
    if (close > 1 && close < 62) {
      publisher = headline.slice(1, close).trim();
      headline = headline.slice(close + 1).trim();
    }
  }
  // Feeds tack the outlet on the end too ("... - Nordea"). Strip that by plain
  // string compare rather than a constructed regex, so punctuation in an outlet
  // name (e.g. "Bloomberg.com") can never corrupt the pattern.
  if (publisher) {
    for (const sep of [' - ', ' \u2013 ', ' \u2014 ', ' | ']) {
      const tail = sep + publisher;
      if (headline.toLowerCase().endsWith(tail.toLowerCase())) {
        headline = headline.slice(0, headline.length - tail.length).trim();
        break;
      }
    }
  }
  // Some sources return a URL-slug title ("bajaj finserv launches finserv
  // intelligence..."). Sentence-case those rather than show them all lowercase;
  // anything with its own capitals is left exactly as published.
  if (headline && headline === headline.toLowerCase() && /[a-z]/.test(headline)) {
    headline = headline.charAt(0).toUpperCase() + headline.slice(1);
  }
  return { publisher, headline };
}

/** "2026-08-22" -> "22 Aug 2026"; returns '' for anything unparseable. */
function formatEventDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 10);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

const TIMING_LABELS = {
  '0-3mo': '<i class="fa-solid fa-fire"></i> 0–3 mo',
  '3-6mo': '<i class="fa-regular fa-clock"></i> 3–6 mo',
  '6-12mo': '<i class="fa-regular fa-hourglass"></i> 6–12 mo'
};

/** Content families, so a job ad never sits in the middle of the news stream. */
const CONTENT_FAMILY = {
  exa_news: 'news', news_rss: 'news',
  career_pages: 'hiring',
  sec_edgar: 'filings', earnings_deck: 'filings', ir_press: 'filings',
  patents: 'filings'
};
function contentFamily(sourceType) { return CONTENT_FAMILY[sourceType] || 'news'; }

/** Normalize a headline for comparison: lowercase, alphanumerics only. */
function normalizeHeadline(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').replace(/\s+/g, ' ').trim();
}

const HEADLINE_STOPWORDS = new Set(['the','a','an','and','or','for','to','of','in','on','with','at','by','from','as','is','its','it','new','you','how','why']);
function headlineTokens(s) {
  return new Set(normalizeHeadline(s).split(' ').filter(w => w.length > 2 && !HEADLINE_STOPWORDS.has(w)));
}
function jaccard(a, b) {
  if (!a.size || !b.size) return 0;
  let inter = 0;
  for (const t of a) if (b.has(t)) inter++;
  return inter / (a.size + b.size - inter);
}

/**
 * One story should be one card. Two passes:
 *   1) collapse events sharing a source document (the extractor emits one
 *      event per matched sentence — a single doc produced 67).
 *   2) merge documents telling the SAME story across feeds (Bloomberg + Exa +
 *      Google News all carry one press release). Restricted to the same
 *      company and the same content family, and gated on a high token overlap,
 *      so distinct stories are never fused.
 */
function groupEventsByDocument(events) {
  const docs = new Map();
  for (const e of events || []) {
    const key = e.raw_doc_id || e.source_url || e.id;
    if (!docs.has(key)) { docs.set(key, { rep: e, all: [e] }); continue; }
    const g = docs.get(key);
    g.all.push(e);
    if ((e.confidence || 0) > (g.rep.confidence || 0)) g.rep = e;
  }

  const cards = [...docs.values()].map(({ rep, all }) => {
    const seen = new Set();
    const initiatives = [];
    for (const e of all) {
      if (e.initiative_name && !seen.has(e.initiative_id)) {
        seen.add(e.initiative_id);
        // initiative_id must travel with the group: the overview resolves tags
        // through the taxonomy by id, and without it every tag fell back to the
        // coarse category name.
        initiatives.push({ initiative_id: e.initiative_id, initiative_name: e.initiative_name, category_id: e.category_id });
      }
    }
    const display = splitHeadline(rep.doc_headline).headline || rep.title;
    return Object.assign({}, rep, {
      _initiatives: initiatives,
      _mentions: all.length,
      _family: contentFamily(rep.source_type),
      _sources: [{ source_type: rep.source_type, url: rep.source_url }],
      _display: display,
      _tokens: headlineTokens(display),
      key_entities: [...new Set(all.flatMap(e => e.key_entities || []))],
      spend_amount: (all.find(e => e.spend_amount) || {}).spend_amount || null,
      timing_horizon: (all.find(e => e.timing_horizon) || {}).timing_horizon || null,
    });
  });

  const merged = [];
  for (const card of cards) {
    const twin = merged.find(m =>
      m.company_id === card.company_id &&
      m._family === card._family &&
      (normalizeHeadline(m._display) === normalizeHeadline(card._display) ||
       jaccard(m._tokens, card._tokens) >= 0.7)
    );
    if (!twin) { merged.push(card); continue; }

    twin._mentions += card._mentions;
    if (!twin._sources.some(s => s.source_type === card.source_type)) {
      twin._sources.push({ source_type: card.source_type, url: card.source_url });
    }
    for (const i of card._initiatives) {
      if (!twin._initiatives.some(x => x.initiative_name === i.initiative_name)) twin._initiatives.push(i);
    }
    twin.key_entities = [...new Set([...(twin.key_entities || []), ...(card.key_entities || [])])];
    twin.doc_image = twin.doc_image || card.doc_image;
    twin.spend_amount = twin.spend_amount || card.spend_amount;
    twin.timing_horizon = twin.timing_horizon || card.timing_horizon;
    // Prefer a real headline over a synthesized fallback.
    if (!splitHeadline(twin.doc_headline).headline && splitHeadline(card.doc_headline).headline) {
      twin.doc_headline = card.doc_headline;
      twin._display = splitHeadline(card.doc_headline).headline;
    }
    if ((card.confidence || 0) > (twin.confidence || 0)) twin.confidence = card.confidence;
  }
  return merged;
}

const Components = {
  /**
   * Render a single Signal Card with quote highlight, IT offering fit, badges, citations.
   */
  /* ================= OVERVIEW (BFSI Insights) =================
     Tile layout modelled on the reference dashboard. Every figure is computed
     from the loaded window and region; tiles we have no data source for
     (share prices, macro news) are deliberately absent, not simulated. */

  /** Short label, hue and icon per taxonomy initiative. */
  INS_THEMES: {
    ai_genai_ml:                 { label: 'AI & GenAI',         hue: '#8b5cf6', icon: 'fa-brain' },
    cloud_migration:             { label: 'Cloud',              hue: '#06b6d4', icon: 'fa-cloud' },
    data_platform_analytics:     { label: 'Data & Analytics',   hue: '#3b82f6', icon: 'fa-database' },
    cybersecurity_zero_trust:    { label: 'Cybersecurity',      hue: '#f43f5e', icon: 'fa-shield-halved' },
    core_system_modernization:   { label: 'Core Modernization', hue: '#64748b', icon: 'fa-server' },
    erp_crm_enterprise_apps:     { label: 'Enterprise Apps',    hue: '#6366f1', icon: 'fa-table-cells' },
    devsecops_platform_eng:      { label: 'DevOps',             hue: '#10b981', icon: 'fa-code-branch' },
    digital_transformation:      { label: 'Digital',            hue: '#22d3ee', icon: 'fa-mobile-screen-button' },
    cost_optimization:           { label: 'Cost Optimization',  hue: '#f59e0b', icon: 'fa-arrow-trend-down' },
    esg_sustainability_tech:     { label: 'ESG',                hue: '#34d399', icon: 'fa-leaf' },
    executive_leadership_change: { label: 'Leadership',         hue: '#a78bfa', icon: 'fa-user-tie' },
    ma_integration:              { label: 'M&A',                hue: '#fb923c', icon: 'fa-code-merge' },
    capex_it_budget:             { label: 'Tech Budget',        hue: '#f59e0b', icon: 'fa-coins' },
    vendor_partnership_rfp:      { label: 'Partnership / RFP',  hue: '#a78bfa', icon: 'fa-handshake' },
    regulatory_compliance_tech:  { label: 'Regulatory',         hue: '#60a5fa', icon: 'fa-scale-balanced' },
    _results:                    { label: 'Results',            hue: '#10b981', icon: 'fa-chart-line' },
    _default:                    { label: 'Signal',             hue: '#06b6d4', icon: 'fa-bolt' },
  },

  /**
   * The extractor tags ANY mention of a C-level title as a leadership change -
   * a CTO quoted about a partnership, a CISO's LinkedIn profile. Measured on the
   * last 800 mentions: 68 tagged, 17 unique stories that describe an actual move.
   * A move needs a verb that moves someone; job ads and profiles never qualify.
   */
  INS_MOVE: /\b(appoint(s|ed|ment)?|name[sd]\b|hire[sd]?\b|joins?\b|joined|promot(ed|ion|es)|elevat(ed|es)|steps? down|stepp(ed|ing) down|resign(s|ed|ation)?|retir(es|ed|ement)|succeed(s|ed)?|successor|takes? over|to (lead|head)|replac(es|ed|ing)|new (group )?(ceo|cio|cto|ciso|cdo|coo|cfo|chief|head|president|chair))/i,

  insIsLeadershipMove(e) {
    return e.initiative_id === 'executive_leadership_change'
      && e.source_type !== 'career_pages'
      && this.INS_MOVE.test(`${e.doc_headline || ''} ${e.quote_text || ''}`);
  },

  insTheme(initiativeId) {
    return this.INS_THEMES[initiativeId] || this.INS_THEMES._default;
  },

  /**
   * Thumbnail: category artwork always renders; a real article image, when we
   * have one, sits on top and removes itself if it fails to load - so a broken
   * or hotlink-blocked image degrades to the artwork instead of a broken icon.
   */
  insThumb(initiativeId, imageUrl, size = 'md') {
    const t = this.insTheme(initiativeId);
    const img = imageUrl
      ? `<img src="${escapeHtml(imageUrl)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`
      : '';
    return `<div class="ins-thumb ins-thumb--${size}" style="--hue:${t.hue}">
      <i class="fa-solid ${t.icon}" aria-hidden="true"></i>${img}</div>`;
  },

  insDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? String(iso).slice(0, 10)
      : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  },

  insEmpty(msg) { return `<p class="ins-empty">${escapeHtml(msg)}</p>`; },

  /** One entry per source document, newest first. */
  insUniqueDocs(events) {
    const seen = new Set();
    const out = [];
    for (const e of [...events].sort((a, b) =>
      String(b.doc_published_at || b.occurred_at).localeCompare(String(a.doc_published_at || a.occurred_at)))) {
      const k = e.raw_doc_id || e.source_url || e.id;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(e);
    }
    return out;
  },

  // ---------------------------------------------------------------- tiles --

  /** Byline under a tile summary, so AI-written prose is never mistaken for a computed figure. */
  insSummaryNote(summary, computedNote) {
    if (summary && summary.source === 'ai') {
      return `<span class="ins-summary-src"><i class="fa-solid fa-wand-magic-sparkles"></i> Written by AI from the figures on this tile</span>`;
    }
    return `<span class="ins-summary-src">${escapeHtml(computedNote)}</span>`;
  },

  /**
   * The insight block every tile carries: the analyst sentence, the sources it
   * was written from (each a link to the real article), and a way to see them all.
   */
  insTileInsight(tile, key) {
    if (!tile || !tile.summary) return '';
    const sources = (tile.evidence || []).slice(0, 4).map(e => `
      <a class="ins-src-chip" href="${escapeHtml(e.url)}" target="_blank" rel="noopener"
         title="${escapeHtml(e.headline)}${e.publisher ? ' — ' + escapeHtml(e.publisher) : ''}">
        ${escapeHtml(e.company)} <i class="fa-solid fa-arrow-up-right-from-square"></i></a>`).join('');
    const more = (tile.evidence || []).length;
    return `
      <div class="ins-insight">
        <p class="ins-insight-text">${escapeHtml(tile.summary.text || '')}</p>
        ${sources ? `<div class="ins-src-row"><span class="ins-src-label">Sources</span>${sources}</div>` : ''}
        <div class="ins-insight-foot">
          ${this.insSummaryNote(tile.summary, tile.stat ? `Computed from ${escapeHtml(tile.stat)}` : 'Computed from this window')}
          ${more ? `<button type="button" class="ins-evidence-btn" data-evidence="${escapeHtml(key)}">
            View Evidence (${more}) <i class="fa-solid fa-arrow-right"></i></button>` : ''}
        </div>
      </div>`;
  },

  /** Full source list behind a tile. */
  drawerEvidence(tile) {
    const items = (tile && tile.evidence) || [];
    if (!items.length) return `<p class="ins-empty">No sources for this tile in the selected window.</p>`;
    return `
      <p class="ins-modal-lede">${escapeHtml(tile.summary ? tile.summary.text : '')}</p>
      <ul class="ins-results-list">` + items.map(e => `
        <li>
          ${this.insThumb(e.initiative_id, null, 'sm')}
          <div class="ins-rl-body">
            <div class="ins-rl-top">
              <a href="#" class="ins-row-co" onclick="App.closeResultsModal(); App.goToCompany('${e.company_id}'); return false;">${escapeHtml(e.company)}</a>
              <span class="ins-rl-date">${escapeHtml(this.insDate(e.date))}</span>
              ${e.publisher ? `<span class="ins-news-pub">${escapeHtml(e.publisher)}</span>` : ''}
            </div>
            <a class="ins-rl-title" href="${escapeHtml(e.url)}" target="_blank" rel="noopener">${escapeHtml(e.headline)}
              <i class="fa-solid fa-arrow-up-right-from-square"></i></a>
            <span class="ins-rl-meta">${escapeHtml(e.initiative || '')}${e.source_type ? ' · ' + escapeHtml(String(e.source_type).replace(/_/g, ' ')) : ''}</span>
          </div>
        </li>`).join('') + `</ul>`;
  },

  insTech(ins, regional) {
    const tech = (ins.tech || []).filter(t => t.companies > 0);
    if (!tech.length) return this.insEmpty('No technology initiatives in this window.');
    const lead = tech[0];
    // A true mix: every company-and-initiative pair counts once, so the slices
    // add up to 100% and a donut is honest. ("Share of companies" cannot be a
    // donut - a company pursues several initiatives, so those shares sum to
    // well over 100%; that figure stays in the summary text.)
    const pairs = tech.reduce((a, t) => a + t.companies, 0);
    const top = tech.slice(0, 5);
    const rest = tech.slice(5).reduce((a, t) => a + t.companies, 0);
    const slices = top.map(t => ({ name: t.name, n: t.companies, hue: this.insTheme(t.id).hue }));
    if (rest) slices.push({ name: 'Other technology initiatives', n: rest, hue: '#64748b' });
    const R = 46, C = 2 * Math.PI * R, GAP = slices.length > 1 ? 1.6 : 0;
    let at = 0;
    const segs = slices.map((sl, k) => {
      const len = C * sl.n / pairs;
      const seg = `<circle cx="60" cy="60" r="${R}" class="ins-ring-fill ins-mix-seg" style="stroke:${sl.hue};--i:${k}"
          stroke-dasharray="${Math.max(0.1, len - GAP).toFixed(2)} ${C.toFixed(2)}"
          transform="rotate(${(-90 + (at / C) * 360).toFixed(2)} 60 60)">
          <title>${escapeHtml(sl.name)}: ${Math.round(100 * sl.n / pairs)}% of technology signals (${sl.n} companies)</title></circle>`;
      at += len;
      return seg;
    }).join('');
    const ring = `
      <svg class="ins-ring ins-mix" viewBox="0 0 120 120" role="img"
           aria-label="${escapeHtml(slices.map(sl => `${sl.name} ${Math.round(100 * sl.n / pairs)}%`).join(', '))}">
        <circle cx="60" cy="60" r="${R}" class="ins-ring-track"/>
        ${segs}
        <text x="60" y="61" class="ins-ring-num">${pairs}</text>
        <text x="60" y="71" class="ins-ring-lbl">signals</text>
      </svg>`;
    const legend = slices.map(sl => `
      <li title="${sl.n} companies"><span class="ins-sw" style="background:${sl.hue}"></span>
        <span class="ins-leg-name">${escapeHtml(sl.name)}</span>
        <span class="ins-leg-val">${Math.round(100 * sl.n / pairs)}%</span></li>`).join('');

    let compare = '';
    if (regional && regional.am && regional.row) {
      const a = (regional.am.tech || []).find(x => x.id === lead.id);
      const r = (regional.row.tech || []).find(x => x.id === lead.id);
      if (a && r) compare = ` Americas ${a.share}% against ${r.share}% in the Rest of World.`;
    }
    const second = tech[1];
    return `
      <div class="ins-tech">
        ${ring}
        <div class="ins-tech-right">
          <p class="ins-mix-cap">Mix of ${pairs} technology signals</p>
          <ul class="ins-legend">${legend}</ul>
          ${this.insTileInsight((ins.tiles || {}).tech, 'tech')}
          ${compare ? `<p class="ins-summary-extra">${escapeHtml(compare.trim())}</p>` : ''}

        </div>
      </div>`;
  },

  insBusiness(ins) {
    const rows = [...(ins.business || [])].sort((a, b) => b.share - a.share).slice(0, 4);
    if (!rows.length || !rows.some(r => r.companies)) return this.insEmpty('No business moves in this window.');
    const max = Math.max(1, ...rows.map(r => r.share));
    const bars = rows.map(r => `
      <div class="ins-bar">
        <div class="ins-bar-top"><span>${escapeHtml(this.insTheme(r.id).label)}</span>
          <b style="color:${this.insTheme(r.id).hue}">${r.share}%</b></div>
        <div class="ins-bar-track"><div class="ins-bar-fill" style="width:${(r.share / max) * 100}%;background:${this.insTheme(r.id).hue}"></div></div>
      </div>`).join('');
    const lead = rows[0];
    const deal = (ins.business || []).find(b => b.id === 'vendor_partnership_rfp');
    return `
      ${bars}
      ${this.insTileInsight((ins.tiles || {}).business, 'business')}`;
  },

  insResults(data) {
    const items = (data.items || []).slice(0, 12);
    if (!items.length) return this.insEmpty('No companies reported results in this window.');
    // The headline leads, as it does on every other tile. It used to show the
    // document kind ("Results release") as the subtitle, which said nothing a
    // reader could act on - the news is "Progressive's combined ratio improved
    // to 86.2", not the fact that a release exists. The endpoint already
    // returns a headline, preferring a written release or article over a bare
    // filing name, so nothing new had to be fetched for this.
    return `<ul class="ins-rows ins-fill" data-min="4">` + items.map(i => {
      const down = i.delta_pct != null && i.delta_pct < 0;
      return `
        <li class="ins-result${down ? ' is-down' : ''}">
          <a class="ins-res-title" href="${escapeHtml(i.url)}" target="_blank" rel="noopener">${escapeHtml(i.headline)}
            <i class="fa-solid fa-arrow-up-right-from-square"></i></a>
          <span class="ins-res-meta">
            <a href="#" class="ins-row-co" onclick="App.goToCompany('${i.company_id}'); return false;">${escapeHtml(i.company_name)}</a>
            <span class="ins-res-dot">&middot;</span><span>${escapeHtml(this.insDate(i.reported_at))}</span>
            ${i.publisher ? `<span class="ins-res-dot">&middot;</span><span>${escapeHtml(i.publisher)}</span>` : ''}
            ${i.delta_pct != null ? `<span class="ins-pill ${down ? 'down' : 'up'}">${down ? '' : '+'}${i.delta_pct}%</span>` : ''}
          </span>
        </li>`;
    }).join('') + `</ul>
      <p class="ins-foot">${data.total_companies} ${data.total_companies === 1 ? 'company' : 'companies'} reported in this window</p>`;
  },

  /**
   * A document's headline for display. When a search result arrived with no
   * title the adapters store "News for <company>" - a placeholder, not a
   * headline - so the extracted sentence is shown instead.
   */
  insHeadline(e) {
    const h = (splitHeadline(e.doc_headline).headline || e.title || '').trim();
    if (!/^news for /i.test(h)) return h;
    const q = String(e.quote_text || '').replace(/\s+/g, ' ').trim();
    return q ? (q.length > 170 ? q.slice(0, 167) + '…' : q) : h;
  },

  insRegulatory(events) {
    const docs = this.insUniqueDocs(events).slice(0, 8);
    if (!docs.length) return this.insEmpty('No regulatory mandates reported in this window.');
    return `<ul class="ins-rows ins-fill" data-min="3">` + docs.map(e => {
      const h = this.insHeadline(e);
      return `
        <li class="ins-reg">
          <a class="ins-reg-title" href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener">${escapeHtml(h)}</a>
          <span class="ins-reg-meta">${escapeHtml(e.company_name || '')} &middot; ${escapeHtml(this.insDate(e.doc_published_at || e.occurred_at))}</span>
        </li>`;
    }).join('') + `</ul>`;
  },

  insDeals(events) {
    const docs = this.insUniqueDocs(events).slice(0, 10);
    if (!docs.length) return this.insEmpty('No budgets or vendor deals disclosed in this window.');
    const srcLabel = { sec_edgar: 'SEC filing', earnings_deck: 'Earnings deck', ir_press: 'IR release',
                       career_pages: 'Job posting', exa_news: 'News', news_rss: 'News' };
    // `ins-deals`: these rows are cards, so they grow to share the tile's
    // height (see styles.css) instead of being spaced apart - and use the room
    // for the full headline and where it came from.
    return `<ul class="ins-rows ins-deals ins-fill" data-min="4">` + docs.map(e => {
      const t = this.insTheme(e.initiative_id);
      const h = this.insHeadline(e);
      const pill = e.spend_amount
        ? `<span class="ins-pill up">${escapeHtml(e.spend_amount)}</span>`
        : `<span class="ins-pill">${escapeHtml(t.label)}</span>`;
      return `
        <li class="ins-row">
          <span class="ins-deal-dot" style="background:${t.hue}"></span>
          <div class="ins-row-main">
            <a href="#" class="ins-row-co" onclick="App.goToCompany('${e.company_id}'); return false;">${escapeHtml(e.company_name || '')}</a>
            <a class="ins-row-sub" href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener" title="${escapeHtml(h)}">${escapeHtml(h)}</a>
            <span class="ins-deal-meta">${escapeHtml(srcLabel[e.source_type] || 'Source')} &middot; ${escapeHtml(this.insDate(e.doc_published_at || e.occurred_at))} &middot; ${escapeHtml(t.label)}</span>
          </div>
          ${pill}
        </li>`;
    }).join('') + `</ul>`;
  },

  insLeadership(events) {
    const docs = this.insUniqueDocs(events).slice(0, 8);
    if (!docs.length) return this.insEmpty('No leadership moves in this window.');
    return `<ul class="ins-lead ins-fill" data-min="3">` + docs.map(e => {
      // Say what moved. "Generali set to take a 20% stake" is the article; the
      // move is "the appointment of Gloria Brocchi as general manager".
      const headline = splitHeadline(e.doc_headline).headline || e.title;
      const quote = String(e.quote_text || '').replace(/\s+/g, ' ').trim();
      const h = this.INS_MOVE.test(headline) || !quote ? headline : quote;
      const initials = (e.company_name || '?').replace(/[^A-Za-z ]/g, ' ').trim()
        .split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
      return `
        <li>
          <span class="ins-avatar">${escapeHtml(initials)}</span>
          <div>
            <a href="#" class="ins-lead-co" onclick="App.goToCompany('${e.company_id}'); return false;">${escapeHtml(e.company_name || '')}</a>
            <a class="ins-lead-title" href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener">${escapeHtml(h)}</a>
            <span class="ins-lead-meta">${escapeHtml(this.insDate(e.doc_published_at || e.occurred_at))}</span>
          </div>
        </li>`;
    }).join('') + `</ul>`;
  },

  insNews(cards) {
    const items = cards.slice(0, 10);
    if (!items.length) return this.insEmpty('No news in this window.');
    return `<ul class="ins-news ins-fill" data-min="4">` + items.map(c => {
      const parts = splitHeadline(c.doc_headline);
      const headline = parts.headline || c.title || '';
      const lead = (c._initiatives && c._initiatives[0]) || { initiative_id: c.initiative_id };
      const t = this.insTheme(lead.initiative_id);
      let desc = String(c.quote_text || '').replace(/\s+/g, ' ').trim();
      const norm = x => String(x).toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
      if (desc && norm(desc).startsWith(norm(headline).slice(0, 50))) desc = '';
      return `
        <li>
          <a class="ins-news-thumb" href="${escapeHtml(c.source_url)}" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true">
            ${this.insThumb(lead.initiative_id, c.doc_image)}
          </a>
          <div class="ins-news-body">
            <span class="ins-news-cat" style="color:${t.hue}">${escapeHtml(t.label)}</span>
            <a class="ins-news-title" href="${escapeHtml(c.source_url)}" target="_blank" rel="noopener">${escapeHtml(headline)}</a>
            ${desc ? `<p class="ins-news-desc">${escapeHtml(desc)}</p>` : ''}
            <span class="ins-news-meta">
              <a href="#" onclick="App.goToCompany('${c.company_id}'); return false;">${escapeHtml(c.company_name || '')}</a>
              &middot; ${escapeHtml(this.insDate(c.doc_published_at || c.occurred_at))}${parts.publisher ? ' &middot; ' + escapeHtml(parts.publisher) : ''}
            </span>
          </div>
        </li>`;
    }).join('') + `</ul>`;
  },

  /* ================= OPPORTUNITY RADAR =================
     An opportunity is one company pursuing one initiative - the same unit the
     company page calls a signal, so the numbers reconcile between pages.
     No monetary values: see routes_opportunities.py for why. */

  /** "High" / "Medium" / "Low", with how it was earned on hover. */
  oppStrength(strength, explained) {
    const s = strength || 'Low';
    return `<span class="opp-strength opp-strength-${s.toLowerCase()}"${explained ? ` title="${escapeHtml(explained)}"` : ''}>${escapeHtml(s)}</span>`;
  },

  oppStrengthBars(strength) {
    const level = { High: 3, Medium: 2, Low: 1 }[strength] || 1;
    const hue = { High: 'var(--accent-emerald-light)', Medium: 'var(--accent-amber)', Low: 'var(--text-dim)' }[strength];
    return `<span class="opp-bars" title="${escapeHtml(strength)}">${[1, 2, 3].map(i =>
      `<i style="${i <= level ? `background:${hue}` : ''}"></i>`).join('')}</span>`;
  },

  oppChange(pct) {
    if (pct == null) return '';
    const up = pct >= 0;
    return `<span class="cp-delta ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${Math.abs(pct)}%</span>`;
  },

  oppKpis(d) {
    const t = d.totals || {};
    const c = d.comparison || {};
    const card = (value, label, extra, note) => `
      <div class="opp-kpi">
        <div class="opp-kpi-top"><span class="opp-kpi-val">${value}</span>${extra || ''}</div>
        <span class="opp-kpi-label">${escapeHtml(label)}</span>
        ${note ? `<span class="opp-kpi-note">${escapeHtml(note)}</span>` : ''}
      </div>`;
    return `
      ${card(t.opportunities ?? 0, 'Total opportunities', this.oppChange(c.opportunities_change_pct),
             c.basis ? `${c.previous_opportunities} in the previous window` : null)}
      ${card(t.high_priority ?? 0, 'High strength', this.oppChange(c.high_priority_change_pct),
             c.basis ? `${c.previous_high_priority} in the previous window` : null)}
      ${card(t.companies ?? 0, 'Companies in play', '', 'with at least one opportunity')}
      ${card(t.medium ?? 0, 'Medium strength', '', `${t.emerging ?? 0} more are Low`)}`;
  },

  /** One-paragraph summary shown at the top of a tile, with its source noted. */
  oppTileSummary(summary) {
    if (!summary || !summary.text) return '';
    return `<p class="opp-tile-summary">${escapeHtml(summary.text)}
      ${this.insSummaryNote(summary, 'Computed from this window')}</p>`;
  },

  oppThemes(themes, summary) {
    if (!themes || !themes.length) return this.insEmpty('No themes in this window.');
    return this.oppTileSummary(summary) + `<div class="opp-themes">` + themes.slice(0, 6).map(t => {
      const th = this.insTheme(t.initiative_id);
      const cls = t.priority === 'High' ? 'high' : (t.priority === 'Medium' ? 'medium' : 'emerging');
      return `
        <button type="button" class="opp-theme" data-theme="${escapeHtml(t.initiative_id)}">
          <span class="opp-theme-icon" style="background:${th.hue}22;color:${th.hue}">
            <i class="fa-solid ${th.icon}"></i></span>
          <span class="opp-theme-name">${escapeHtml(t.name)}</span>
          <span class="opp-theme-n">${t.companies} ${t.companies === 1 ? 'company' : 'companies'}</span>
          <span class="opp-theme-co">${t.high_count} high priority · top score ${t.top_score}</span>
          <span class="opp-tag opp-tag-${cls}">${escapeHtml(t.priority)} priority</span>
        </button>`;
    }).join('') + `</div>
      <p class="cp-note">Click a theme to see every company pursuing it.</p>`;
  },

  oppMarket(items) {
    if (!items || !items.length) return this.insEmpty('No market signals in this window.');
    return `<ul class="opp-market">` + items.map(m => {
      const th = this.insTheme(m.initiative_id);
      return `
        <li>
          <span class="opp-market-icon" style="background:${th.hue}22;color:${th.hue}">
            <i class="fa-solid ${th.icon}"></i></span>
          <div class="opp-market-body">
            <a href="${escapeHtml(m.url)}" target="_blank" rel="noopener">${escapeHtml(splitHeadline(m.title).headline || m.title)}</a>
            <span class="opp-market-meta">
              <a href="#" onclick="App.goToCompany('${m.company_id}'); return false;">${escapeHtml(m.company_name || '')}</a>
              · ${escapeHtml(this.insDate(m.date))}
            </span>
          </div>
          <span class="opp-market-tag">${escapeHtml(m.tag)}</span>
        </li>`;
    }).join('') + `</ul>`;
  },

  oppFeatured(items, summary) {
    if (!items || !items.length) return this.insEmpty('No opportunities in this window.');
    const sourceLabels = {
      sec_edgar: 'SEC filing', earnings_deck: 'Earnings deck', news_rss: 'News',
      exa_news: 'News', ir_press: 'IR release', career_pages: 'Hiring',
    };
    return this.oppTileSummary(summary) + `
      <div class="opp-table-wrap">
      <table class="opp-table">
        <thead><tr>
          <th>Opportunity</th><th>Theme</th><th>Strength</th><th>Timing</th>
          <th>Evidence</th><th>Why now?</th>
        </tr></thead>
        <tbody>` + items.map(f => {
      const th = this.insTheme(f.initiative_id);
      const triggers = [...new Set((f.triggers || []).map(s => sourceLabels[s] || s))];
      const docs = f.document_count || 0;
      return `<tr class="opp-row" data-ev-co="${f.company_id}" data-ev-init="${escapeHtml(f.initiative_id)}"
                  title="Click to see every document behind this">
          <td>
            <a href="#" class="opp-co" onclick="App.goToCompany('${f.company_id}'); return false;">${escapeHtml(f.company_name)}</a>
            <span class="opp-offer">${escapeHtml(f.it_offering || '')}</span>
          </td>
          <td><span class="opp-theme-pill" style="background:${th.hue}1f;color:${th.hue}">${escapeHtml(th.label)}</span></td>
          <td>${this.oppStrengthBars(f.strength)}${this.oppStrength(f.strength, f.points_explained)}</td>
          <td><span class="opp-timing">${escapeHtml(f.timing_window || '—')}</span></td>
          <td><button type="button" class="opp-ev-btn" data-ev-co="${f.company_id}" data-ev-init="${escapeHtml(f.initiative_id)}">
              ${docs} ${docs === 1 ? 'document' : 'documents'} <i class="fa-solid fa-chevron-right"></i></button>
            <span class="opp-triggers">${triggers.map(t => `<i>${escapeHtml(t)}</i>`).join('')}</span></td>
          <td class="opp-why">${escapeHtml((f.why_now || []).slice(0, 2).join(' · '))}</td>
        </tr>`;
    }).join('') + `</tbody></table></div>`;
  },

  oppAccounts(accounts) {
    if (!accounts || !accounts.length) return this.insEmpty('No accounts in this window.');
    return `<div class="opp-accounts">` + accounts.slice(0, 6).map(a => `
      <button type="button" class="opp-account" onclick="App.goToCompany('${a.company_id}')">
        <span class="ins-avatar">${escapeHtml(this.cpMonogram(a.company_name))}</span>
        <span class="opp-account-name">${escapeHtml(a.company_name)}</span>
        <span class="opp-account-n">${a.opportunities} ${a.opportunities === 1 ? 'opportunity' : 'opportunities'}${a.high_count ? ` · ${a.high_count} high` : ''}</span>
        <span class="opp-account-themes">${a.themes.slice(0, 2).map(t => `<i>${escapeHtml(t)}</i>`).join('')}</span>
        <span class="opp-account-foot">${this.oppStrengthBars(a.strength)} <b>${a.best_score}</b></span>
      </button>`).join('') + `</div>`;
  },

  /**
   * Rising Fast: the same (company, initiative) pair scored against itself a
   * window ago. A pair with no prior score is labelled "New" rather than
   * excluded - a fresh 82 and an existing 40->95 are both real "why now"
   * stories, just different ones, so they are shown distinctly rather than
   * folded into one number.
   */
  oppRising(rows) {
    if (!rows || !rows.length) {
      return this.insEmpty('Nothing has gained ground since the previous window. '
        + 'This needs two comparable windows, so it is empty on "All time".');
    }
    return `<ul class="ins-rows">` + rows.map(r => `
      <li class="opp-rising-row">
        <div class="ins-row-main">
          <a href="#" class="ins-row-co" onclick="App.goToCompany('${r.company_id}'); return false;">${escapeHtml(r.company_name)}</a>
          <span class="opp-rising-theme">${escapeHtml(r.theme)}</span>
        </div>
        <div class="opp-rising-right">
          <span class="opp-rising-badge ${r.is_new ? 'is-new' : 'is-up'}">
            ${r.is_new ? `New · ${r.strength}`
              : (r.previous_strength !== r.strength ? `${r.previous_strength} &rarr; ${r.strength}` : r.strength)}</span>
          <span class="opp-rising-sub">${r.is_new ? 'first appearance this window' : `+${r.delta} ${r.delta === 1 ? 'point' : 'points'}`}${r.document_delta > 0 ? ` &middot; +${r.document_delta} new ${r.document_delta === 1 ? 'document' : 'documents'}` : ''}</span>
        </div>
      </li>`).join('') + `</ul>`;
  },

  /**
   * Competitive Pressure: for a few of the top-scored initiatives, a small
   * ranked group of every company active on it in the same industry (or BFSI
   * overall when an industry has too few active companies to compare within).
   * Reuses the peer_context the engine already computes per signal - this
   * just turns it into a leaderboard instead of leaving it unrendered.
   */
  oppCompetitive(groups) {
    if (!groups || !groups.length) {
      return this.insEmpty('No initiative yet has more than one active company to compare.');
    }
    // One scale for every group, so a long bar means the same thing in each.
    const max = Math.max(1, ...groups.flatMap(g => g.rows.map(r => r.intent_score || 0)));
    return `<div class="opp-lb-chart">` + groups.map(g => `
      <div class="opp-lb">
        <div class="opp-lb-head">
          <span class="opp-lb-theme">${escapeHtml(g.theme)}</span>
          <span class="opp-lb-scope">within ${g.scope === 'industry' ? 'the same industry' : 'BFSI overall'}</span>
        </div>
        <div class="opp-cp-bars">` + g.rows.map(r => {
          const band = (r.strength || 'Low').toLowerCase();
          const pts = `${r.intent_score} ${r.intent_score === 1 ? 'point' : 'points'}`;
          const width = Math.max(4, Math.round(100 * (r.intent_score || 0) / max));
          return `
          <div class="opp-bar-row${r.company_id === g.leader_id ? ' is-leader' : ''}" title="${escapeHtml(r.name)}: ${escapeHtml(r.strength || '')} (${pts})">
            <a href="#" class="opp-bar-name" onclick="App.goToCompany('${r.company_id}'); return false;">${escapeHtml(r.name)}</a>
            <span class="opp-bar-track"><span class="opp-bar-fill fill-${band}" style="width:${width}%"></span></span>
            ${this.oppStrength(r.strength, pts)}
          </div>`;
        }).join('') + `</div>
      </div>`).join('') + `</div>`;
  },

  /**
   * People to Tap: one row per person, in the order of the opportunities they
   * relate to. Companies with nobody on file are simply absent - the server
   * looks them up in the background and they appear on a later load.
   */
  oppPeople(people, recs, pending) {
    const initials = n => String(n || '?').replace(/[^A-Za-z ]/g, ' ').trim().split(/\s+/)
      .slice(0, 2).map(w => w[0]).join('').toUpperCase();
    const list = (people || []).length ? `<div class="opp-people-list">` + people.map(l => {
      const opps = (l.opportunities || []).map(o =>
        `${escapeHtml(o.theme)} ${this.oppStrength(o.strength)}`).join('<span class="opp-ctx-sep">+</span>');
      return `
        <div class="opp-person-row">
          <span class="ins-avatar">${escapeHtml(initials(l.name))}</span>
          <div class="opp-person-main">
            <a class="opp-person-name" href="${escapeHtml(l.linkedin_url)}" target="_blank" rel="noopener"
               title="Open LinkedIn profile">${escapeHtml(l.name)} <i class="fa-brands fa-linkedin"></i></a>
            <span class="opp-person-title" title="${escapeHtml(l.headline)}">${escapeHtml(l.headline)}</span>
            <span class="opp-person-ctx"><a href="#" class="ins-row-co" onclick="App.goToCompany('${l.company_id}'); return false;">${escapeHtml(l.company_name)}</a>
              <span class="opp-ctx-sep">&middot;</span>${opps}</span>
            <span class="opp-person-why is-${escapeHtml(l.match)}">${escapeHtml(l.reason)}</span>
          </div>
        </div>`;
    }).join('') + `</div>`
      : this.insEmpty(pending ? 'Looking up leaders for these companies - they appear here once found.'
                              : 'No verified leaders on file for the top opportunities yet.');
    return list;
  },

  /**
   * Recommended Targets: which opportunities to go after and why. Each play
   * comes from the figures on this page (see routes_opportunities.py); the
   * accounts are clickable, so a salesperson can go straight to the company.
   */
  oppTargets(recs) {
    if (!recs || !recs.length) return this.insEmpty('No clear plays in this window yet.');
    const icon = { engage_now: 'fa-bolt', lead_theme: 'fa-layer-group', momentum: 'fa-arrow-trend-up',
                   peer_pressure: 'fa-people-arrows' };
    return `<div class="opp-targets">` + recs.map(r => `
      <div class="opp-target">
        <span class="opp-target-icon"><i class="fa-solid ${icon[r.kind] || 'fa-crosshairs'}"></i></span>
        <div class="opp-target-main">
          <span class="opp-target-title">${escapeHtml(r.title)}</span>
          <span class="opp-target-detail">${escapeHtml(r.detail)}</span>
          ${r.offering ? `<span class="opp-target-offer"><b>Lead with</b> ${escapeHtml(r.offering)}</span>` : ''}
          <div class="opp-target-accounts">` + (r.accounts || []).map(a => `
            <a href="#" class="opp-target-acct" onclick="App.goToCompany('${a.company_id}'); return false;"
               title="${escapeHtml(a.theme)}${a.points_explained ? ' - ' + escapeHtml(a.points_explained) : ''}">
              ${escapeHtml(a.company_name)}
              ${r.kind === 'lead_theme' || r.kind === 'peer_pressure' ? '' : `<span class="opp-target-theme">${escapeHtml(a.theme)}</span>`}
              ${a.from ? `<span class="opp-target-move">${escapeHtml(a.from)} &rarr;</span>` : ''}${this.oppStrength(a.strength)}</a>`).join('') + `
          </div>
        </div>
      </div>`).join('') + `</div>`;
  },

  /** Drawer: every document behind one opportunity, with the points each earned. */
  drawerOppEvidence(d) {
    const docs = d.documents || [];
    if (!docs.length) return `<p class="ins-empty">No documents for this opportunity in the selected window.</p>`;
    const sum = docs.map(x => x.points).join(' + ');
    return `
      <div class="ev-summary">
        <a href="#" class="ev-co" onclick="App.closeResultsModal(); App.goToCompany('${d.company_id}'); return false;">${escapeHtml(d.company_name || '')}</a>
        <span class="ev-theme">${escapeHtml(d.theme)}</span>
        ${this.oppStrength(d.strength)}
        <span class="ev-math">${escapeHtml(sum)} = <b>${d.points} ${d.points === 1 ? 'point' : 'points'}</b>
          from ${docs.length} ${docs.length === 1 ? 'document' : 'documents'}</span>
      </div>
      <ul class="ev-list">` + docs.map(x => `
        <li class="ev-item">
          <span class="ev-pts">+${x.points}</span>
          <div class="ev-body">
            <div class="ev-meta">
              <span class="ev-src ev-src-${escapeHtml(x.source_type)}">${escapeHtml(x.source_label)}</span>
              <span>${escapeHtml(this.insDate(x.date))}</span>
              ${x.publisher ? `<span>${escapeHtml(x.publisher)}</span>` : ''}
            </div>
            <a class="ev-title" href="${escapeHtml(x.url)}" target="_blank" rel="noopener">${escapeHtml(x.title || x.url)}
              <i class="fa-solid fa-arrow-up-right-from-square"></i></a>
            ${x.quote ? `<blockquote class="ev-quote">"${escapeHtml(x.quote)}"</blockquote>` : ''}
          </div>
        </li>`).join('') + `</ul>`;
  },

  // ======================================================== BFSI solar system
  GX_HUE: { 'Banking': '#38bdf8', 'Insurance': '#a78bfa', 'Capital Markets & Wealth': '#34d399',
            'Payments & Fintech': '#f59e0b' },
  GX_STRENGTH: { High: '#4ade80', Medium: '#fbbf24', Low: '#94a3b8' },

  _gxNum(x) { return Math.round(x * 10) / 10; },

  /** A fixed starfield, the same on every render. */
  _gxField(W, H) {
    let seed = 7, out = '';
    const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < 110; i++) {
      const r = rnd() < 0.85 ? 0.7 : 1.3;
      out += `<circle cx="${this._gxNum(rnd() * W)}" cy="${this._gxNum(rnd() * H)}" r="${r}" class="gx-dust" style="opacity:${this._gxNum(0.2 + rnd() * 0.5)}"/>`;
    }
    return out;
  },

  _gxDefs(verticals) {
    return `<defs>` + verticals.map((v, i) => `
      <radialGradient id="gxStar${i}" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stop-color="#fffbe6"/><stop offset="45%" stop-color="${this.GX_HUE[v.name] || '#fcd34d'}"/>
        <stop offset="100%" stop-color="${this.GX_HUE[v.name] || '#fcd34d'}" stop-opacity="0"/>
      </radialGradient>`).join('') + `</defs>`;
  },

  _gxTip(txt) { return escapeHtml(txt); },

  /** Level 0: every vertical as a star with its planets in orbit. */
  galaxyOverview(data) {
    const W = 1200, H = 560, vs = data.verticals, step = W / vs.length;
    const systems = vs.map((v, i) => {
      const cx = step * (i + 0.5), cy = 250;
      const rs = 16 + 2.6 * Math.sqrt(v.count), R = rs + 64, hue = this.GX_HUE[v.name];
      const planets = v.planets.map((p, j) => {
        const a = (j / v.planets.length) * Math.PI * 2 - Math.PI / 2;
        const px = cx + R * Math.cos(a), py = cy + R * Math.sin(a), pr = 5 + 1.6 * Math.sqrt(p.count || 1);
        return `<circle class="gx-planet" data-gx="planet" data-v="${i}" data-p="${j}" cx="${this._gxNum(px)}" cy="${this._gxNum(py)}"
                  r="${this._gxNum(pr)}" fill="${hue}"
                  data-tip="${this._gxTip(`${p.name} - ${p.count} ${p.count === 1 ? 'company' : 'companies'} · ${p.high} High · ${p.medium} Medium`)}"/>`;
      }).join('');
      return `
        <g class="gx-system">
          <circle cx="${cx}" cy="${cy}" r="${this._gxNum(R)}" class="gx-orbit"/>
          <g class="gx-rot" style="animation-duration:${90 + i * 18}s;transform-origin:${cx}px ${cy}px">${planets}</g>
          <circle cx="${cx}" cy="${cy}" r="${this._gxNum(rs * 1.9)}" fill="url(#gxStar${i})" class="gx-glow"/>
          <circle cx="${cx}" cy="${cy}" r="${this._gxNum(rs)}" fill="url(#gxStar${i})" class="gx-star" data-gx="star" data-v="${i}"
                  data-tip="${this._gxTip(`${v.name} - ${v.count} companies in ${v.planets.length} sub-industries. Click to open.`)}"/>
          <text x="${cx}" y="${this._gxNum(cy + R + 34)}" class="gx-star-name" data-gx="star" data-v="${i}">${escapeHtml(v.name)}</text>
          <text x="${cx}" y="${this._gxNum(cy + R + 54)}" class="gx-star-sub">${v.count} companies · ${v.high} High · ${v.medium} Medium</text>
        </g>`;
    }).join('');
    return `<svg class="gx-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="BFSI verticals">
      ${this._gxDefs(vs)}${this._gxField(W, H)}${systems}</svg>`;
  },

  /** Level 1: one vertical, its sub-industries each on their own orbit. */
  galaxySystem(data, vi) {
    const W = 1200, H = 620, cx = 600, cy = 310, v = data.verticals[vi], hue = this.GX_HUE[v.name];
    const n = v.planets.length, rs = 46;
    const rings = v.planets.map((p, k) => {
      const r = n === 1 ? 170 : 100 + k * (170 / (n - 1));
      const a = k * 2.4 - Math.PI / 2;
      const px = cx + r * Math.cos(a), py = cy + r * Math.sin(a), pr = 8 + 2.2 * Math.sqrt(p.count || 1);
      const dur = 80 + k * 22;
      return `
        <circle cx="${cx}" cy="${cy}" r="${this._gxNum(r)}" class="gx-orbit"/>
        <g class="gx-rot" style="animation-duration:${dur}s;transform-origin:${cx}px ${cy}px">
          <circle class="gx-planet" data-gx="planet" data-v="${vi}" data-p="${k}" cx="${this._gxNum(px)}" cy="${this._gxNum(py)}"
                  r="${this._gxNum(pr)}" fill="${hue}"
                  data-tip="${this._gxTip(`${p.name} - ${p.count} companies · ${p.high} High · ${p.medium} Medium. Click to see them.`)}"/>
          <g class="gx-up" style="animation-duration:${dur}s;transform-origin:${this._gxNum(px)}px ${this._gxNum(py)}px">
            <text x="${this._gxNum(px)}" y="${this._gxNum(py + pr + 15)}" class="gx-planet-name" data-gx="planet" data-v="${vi}" data-p="${k}">${escapeHtml(p.name)}</text>
            <text x="${this._gxNum(px)}" y="${this._gxNum(py + pr + 29)}" class="gx-planet-sub">${p.count} · ${p.high} High</text>
          </g>
        </g>`;
    }).join('');
    return `<svg class="gx-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(v.name)} sub-industries">
      ${this._gxDefs(data.verticals)}${this._gxField(W, H)}${rings}
      <circle cx="${cx}" cy="${cy}" r="${rs * 1.8}" fill="url(#gxStar${vi})" class="gx-glow"/>
      <circle cx="${cx}" cy="${cy}" r="${rs}" fill="url(#gxStar${vi})" class="gx-star"/>
      <text x="${cx}" y="${cy + 5}" class="gx-core-label">${escapeHtml(v.name)}</text>
    </svg>`;
  },

  /** Level 2: one sub-industry, its companies as satellites (strongest innermost). */
  galaxyPlanet(data, vi, pi) {
    const W = 1200, H = 620, cx = 600, cy = 310;
    const v = data.verticals[vi], p = v.planets[pi], hue = this.GX_HUE[v.name];
    const radii = [118, 180, 242, 300];
    const cap = radii.map(r => Math.max(6, Math.floor((2 * Math.PI * r) / 80)));
    const rings = [];
    let i = 0;
    for (let k = 0; k < radii.length && i < p.companies.length; k++) {
      rings.push(p.companies.slice(i, i + cap[k]));
      i += cap[k];
    }
    const body = rings.map((cos, k) => {
      const r = radii[k], dur = 110 + k * 35;
      const sats = cos.map((c, j) => {
        const a = (j / cos.length) * Math.PI * 2 - Math.PI / 2 + k * 0.35;
        const sx = cx + r * Math.cos(a), sy = cy + r * Math.sin(a);
        const sr = 6 + Math.min(c.points || 0, 10) * 0.6;
        const col = this.GX_STRENGTH[c.strength] || '#475569';
        const label = c.label.length > 17 ? c.label.slice(0, 16) + '…' : c.label;
        const tip = `${c.name}${c.country ? ' · ' + c.country : ''} - ` +
          (c.strength ? `${c.strength} (${c.points} ${c.points === 1 ? 'point' : 'points'}) on ${c.top_theme}` : 'no opportunity in this window');
        return `
          <circle class="gx-sat" data-gx="sat" data-id="${c.id}" cx="${this._gxNum(sx)}" cy="${this._gxNum(sy)}" r="${this._gxNum(sr)}"
                  fill="${col}" data-tip="${this._gxTip(tip)}"/>
          <g class="gx-up" style="animation-duration:${dur}s;transform-origin:${this._gxNum(sx)}px ${this._gxNum(sy)}px">
            <text x="${this._gxNum(sx)}" y="${this._gxNum(sy + sr + 12)}" class="gx-sat-name" data-gx="sat" data-id="${c.id}">${escapeHtml(label)}</text>
          </g>`;
      }).join('');
      return `<circle cx="${cx}" cy="${cy}" r="${r}" class="gx-orbit"/>
        <g class="gx-rot" style="animation-duration:${dur}s;transform-origin:${cx}px ${cy}px">${sats}</g>`;
    }).join('');
    const svg = `<svg class="gx-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(p.name)} companies">
      ${this._gxDefs(data.verticals)}${this._gxField(W, H)}${body}
      <circle cx="${cx}" cy="${cy}" r="58" fill="${hue}" class="gx-planet-core"/>
      <text x="${cx}" y="${cy - 2}" class="gx-core-count">${p.count}</text>
      <text x="${cx}" y="${cy + 18}" class="gx-core-sub">${p.count === 1 ? 'company' : 'companies'}</text>
    </svg>`;
    const list = `
      <aside class="gx-list">
        <h4 class="gx-list-title">${escapeHtml(p.name)}</h4>
        <p class="gx-list-sub">${p.count} companies · ${p.high} High · ${p.medium} Medium</p>
        <ul>` + p.companies.map(c => `
          <li><a href="#" data-gx="sat" data-id="${c.id}">
            <span class="gx-dot" style="background:${this.GX_STRENGTH[c.strength] || '#475569'}"></span>
            <span class="gx-li-name">${escapeHtml(c.name)}</span>
            ${c.strength ? this.oppStrength(c.strength, `${c.points} points · ${c.top_theme}`) : '<span class="gx-none">-</span>'}
          </a></li>`).join('') + `</ul>
      </aside>`;
    return svg + list;
  },

  galaxyLegend() {
    return `<div class="gx-legend">
      <span><i style="background:#4ade80"></i>High</span><span><i style="background:#fbbf24"></i>Medium</span>
      <span><i style="background:#94a3b8"></i>Low</span><span><i style="background:#475569"></i>No opportunity</span>
      <span class="gx-legend-note">Satellite size = points · hover to pause the orbits</span></div>`;
  },

  galaxyView(data, st) {
    let stage;
    if (st.v != null && st.p != null) stage = this.galaxyPlanet(data, st.v, st.p);
    else if (st.v != null) stage = this.galaxySystem(data, st.v);
    else stage = this.galaxyOverview(data);
    return `<div class="gx-stage ${st.p != null ? 'has-list' : ''}">${stage}<div class="gx-tip" hidden></div></div>
      ${st.p != null ? this.galaxyLegend() : ''}`;
  },

  galaxyCrumbs(data, st) {
    const parts = [`<a href="#" data-gx-crumb="root">BFSI</a>`];
    if (st.v != null) parts.push(`<a href="#" data-gx-crumb="v">${escapeHtml(data.verticals[st.v].name)}</a>`);
    if (st.p != null) parts.push(`<span>${escapeHtml(data.verticals[st.v].planets[st.p].name)}</span>`);
    return parts.join('<i class="fa-solid fa-chevron-right"></i>');
  },

  /**
   * Show as many list items as fit each tile. Tiles in a row share the tallest
   * one's height; a shorter tile used to spread its 3-4 items apart, leaving
   * wide empty bands. Now it reveals more of the items it was given, one at a
   * time, and stops at the first that would make the tile taller - so the row
   * height never changes and the tile ends up full of content, not gaps.
   */
  /**
   * Entrance animation for freshly rendered content. CSS does the motion (see
   * "Loading and entrance animation" in styles.css); this only numbers each
   * item within its own list as --i, so lists reveal top to bottom, and counts
   * the headline numbers up from zero. The final value is already in the page,
   * so nothing is lost if the animation never runs (e.g. reduced motion).
   */
  FX_ITEMS: '.ins-body > ul > li, .ins-bar, .ins-legend li, .opp-kpi, .opp-table tbody tr, .opp-lb, .opp-bar-row, '
          + '.opp-rising-row, .opp-person-row, .opp-target, .ev-item, .cp-prio > li, .gx-list li',

  animateIn(el) {
    if (!el) return;
    el.querySelectorAll(this.FX_ITEMS).forEach(node => {
      const i = Array.prototype.indexOf.call(node.parentNode.children, node);
      node.style.setProperty('--i', Math.min(i, 14));
    });
    let k = 0;
    el.querySelectorAll('.gx-planet, .gx-sat').forEach(node => node.style.setProperty('--i', Math.min(k++, 45)));
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    el.querySelectorAll('.opp-kpi-val, .gx-core-count').forEach(node => this.countUp(node));
  },

  /** Count a number up from zero; leaves non-numeric text alone. */
  countUp(node) {
    const text = node.textContent.trim();
    if (!/^\d{1,6}$/.test(text)) return;
    const target = parseInt(text, 10), start = performance.now(), dur = 900;
    if (target === 0) return;
    const tick = now => {
      const t = Math.min(1, (now - start) / dur);
      node.textContent = String(Math.round(target * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(tick); else node.textContent = String(target);
    };
    node.textContent = '0';
    requestAnimationFrame(tick);
    // A background tab pauses requestAnimationFrame; never leave a 0 on screen.
    setTimeout(() => { node.textContent = String(target); }, dur + 400);
  },

  /**
   * Company page masonry. A company's tiles vary far more than the dashboard's
   * - a quiet or non-US company may have no SEC financials, no leadership
   * changes and one deal - and in fixed rows each tile was stretched to its
   * tallest neighbour, leaving up to 440px of empty card. Here each tile, in
   * priority order, goes into whichever column is currently shortest, so tiles
   * keep their natural height; the last tile of each column then grows the
   * little that is left, so the columns end level.
   */
  layoutMasonry(scope) {
    (scope || document).querySelectorAll('.cp-masonry').forEach(m => {
      if (!m.offsetParent) return;
      const tiles = [...m.querySelectorAll('.ins-tile')];
      tiles.forEach((t, i) => { if (t.dataset.order == null) t.dataset.order = i; });
      tiles.sort((a, b) => a.dataset.order - b.dataset.order);
      const w = m.getBoundingClientRect().width;
      const n = w >= 1050 ? 3 : (w >= 680 ? 2 : 1);
      if (m.dataset.cols === String(n) && m.querySelector(':scope > .cp-col')) return;
      m.dataset.cols = String(n);
      [...m.children].forEach(ch => ch.remove());
      const cols = Array.from({ length: n }, () => {
        const c = document.createElement('div');
        c.className = 'cp-col';
        m.appendChild(c);
        return c;
      });
      // Measure what each column holds, not its box: the columns stretch to
      // equal height, so comparing boxes put every tile in the first column.
      // Growth (which levels the column ends) is switched off while placing,
      // or every column would look equally full.
      m.classList.add('is-placing');
      // Measure every tile at column width, then place the largest first into
      // the shortest column (the classic longest-first balancing), and finally
      // restore reading order inside each column.
      tiles.forEach(t => cols[0].appendChild(t));
      const size = new Map(tiles.map(t => [t, t.getBoundingClientRect().height]));
      const load = cols.map(() => 0);
      [...tiles].sort((a, b) => size.get(b) - size.get(a)).forEach(t => {
        const k = load.indexOf(Math.min(...load));
        cols[k].appendChild(t);
        load[k] += size.get(t) + 18;
      });
      cols.forEach(c => [...c.children]
        .sort((a, b) => a.dataset.order - b.dataset.order)
        .forEach(t => c.appendChild(t)));
      m.classList.remove('is-placing');
    });
  },

  /** Start every fillable list at its minimum, so an unfitted list never inflates its row. */
  compactLists(scope) {
    (scope || document).querySelectorAll('ul.ins-fill').forEach(list =>
      [...list.children].slice(+list.dataset.min || 0).forEach(li => { li.hidden = true; }));
  },

  fillLists(scope) {
    const lists = [...(scope || document).querySelectorAll('ul.ins-fill')].filter(l => l.offsetParent);
    this.compactLists(scope);
    lists.forEach(list => {
      const tile = list.closest('.ins-tile');
      if (!tile || tile.classList.contains('is-retracted')) return;
      const base = tile.getBoundingClientRect().height;
      for (const li of [...list.children].slice(+list.dataset.min || 0)) {
        li.hidden = false;
        if (tile.getBoundingClientRect().height > base + 0.5) { li.hidden = true; break; }
      }
    });
  },

  /** Cap a list at `n` rows and scroll the rest. Measured, since rows vary in height. */
  fitRows(list, rowSelector, n) {
    if (!list) return;
    const rows = list.querySelectorAll(rowSelector);
    list.style.maxHeight = '';
    if (rows.length <= n) return;
    const top = rows[0].offsetTop;
    list.style.maxHeight = `${rows[n].offsetTop - top}px`;
  },

  oppBreakdown(rows, total) {
    if (!rows || !rows.length) return this.insEmpty('Nothing to break down yet.');
    const palette = ['#22d3ee', '#8b5cf6', '#f59e0b', '#10b981', '#60a5fa', '#f43f5e'];
    let acc = 0;
    const stops = rows.map((r, i) => {
      const from = acc; acc += r.share;
      return `${palette[i % palette.length]} ${from}% ${acc}%`;
    }).join(', ');
    return `
      <div class="opp-donut-wrap">
        <div class="ins-donut opp-donut" style="background: conic-gradient(${stops})">
          <div class="opp-donut-hole"><strong>${total}</strong><span>opportunities</span></div>
        </div>
      </div>
      <ul class="ins-legend">` + rows.map((r, i) => `
        <li><span class="ins-sw" style="background:${palette[i % palette.length]}"></span>
          <span class="ins-leg-name">${escapeHtml(r.label)}</span>
          <span class="ins-leg-val">${r.share}%</span></li>`).join('') + `</ul>`;
  },

  oppInsight(insight, filters) {
    if (!insight) return this.insEmpty('Not enough activity to draw a conclusion.');
    const chips = (insight.target_accounts || []).map(a =>
      `<button type="button" class="opp-target" onclick="App.goToCompany('${a.id}')">${escapeHtml(a.name)}</button>`).join('');
    const prio = (filters && filters.priority) || [];
    return `
      <div class="opp-insight">
        <h4>${escapeHtml(insight.headline)}</h4>
        <p>${escapeHtml((insight.summary && insight.summary.text) || insight.detail)}</p>
        ${this.insSummaryNote(insight.summary, 'Computed from this window')}
        ${insight.bullets && insight.bullets.length ? `
          <div class="opp-means"><span>What this means for you</span>
            <ul>${insight.bullets.map(b => `<li>${escapeHtml(b)}</li>`).join('')}</ul></div>` : ''}
        ${chips ? `<div class="opp-targets"><span>Target accounts</span><div>${chips}</div></div>` : ''}
        ${prio.length ? `<div class="opp-prio-split">${prio.map(p =>
          `<span><b>${p.count}</b> ${escapeHtml(p.level)}</span>`).join('')}</div>` : ''}
      </div>`;
  },

  oppNextSteps(steps) {
    if (!steps || !steps.length) return this.insEmpty('No actions to suggest yet.');
    return `<ol class="opp-steps">` + steps.map(s => `
      <li><b>${escapeHtml(s.title)}</b><span>${escapeHtml(s.detail)}</span></li>`).join('') + `</ol>
      <p class="cp-note">Computed from the data above — not a model-written recommendation.</p>`;
  },

  /** Drawer: companies behind a trend, ranked by their best signal. */
  drawerCompanies(signals, taxonomy) {
    const byCo = new Map();
    for (const s of signals) {
      const cur = byCo.get(s.company_id);
      if (!cur || s.intent_score > cur.intent_score) byCo.set(s.company_id, s);
    }
    const rows = [...byCo.values()].sort((a, b) => b.intent_score - a.intent_score);
    if (!rows.length) return `<p class="ins-empty">No companies match in this window.</p>`;
    return `<p class="ins-modal-lede">${rows.length} ${rows.length === 1 ? 'company' : 'companies'},
      ranked by their strongest signal. Open one to see the full profile.</p>
      <ul class="ins-results-list">` + rows.map(s => {
      const band = scoreBand(s.intent_score);
      return `<li>
        <span class="score-badge ${band.cls} ins-hot-score" title="${escapeHtml(s.points_explained || '')}">${band.label}</span>
        <div class="ins-rl-body">
          <div class="ins-rl-top">
            <a href="#" class="ins-row-co" onclick="App.closeResultsModal(); App.goToCompany('${s.company_id}'); return false;">${escapeHtml(s.company_name || '')}</a>
            ${s.company_region ? `<span class="ins-rl-date">${escapeHtml(s.company_region)}</span>` : ''}
          </div>
          <span class="ins-rl-title">${escapeHtml(((taxonomy || {})[s.initiative_id] || {}).name || s.initiative_name || '')}</span>
          <span class="ins-rl-meta">${escapeHtml(s.points_explained || '')}${s.timing_window ? ' · ' + escapeHtml(s.timing_window) : ''}</span>
        </div></li>`;
    }).join('') + `</ul>`;
  },

  /** Drawer: a list of stories, newest first. */
  drawerStories(rows, grouped) {
    const items = grouped ? rows : this.insUniqueDocs(rows);
    if (!items.length) return `<p class="ins-empty">Nothing in this window.</p>`;
    return `<p class="ins-modal-lede">${items.length} ${items.length === 1 ? 'story' : 'stories'} in this window.</p>
      <ul class="ins-results-list">` + items.map(e => {
      const parts = splitHeadline(e.doc_headline);
      const th = this.insTheme(e.initiative_id || ((e._initiatives || [{}])[0] || {}).initiative_id);
      return `<li>
        ${this.insThumb(e.initiative_id || ((e._initiatives || [{}])[0] || {}).initiative_id, e.doc_image, 'sm')}
        <div class="ins-rl-body">
          <div class="ins-rl-top">
            <a href="#" class="ins-row-co" onclick="App.closeResultsModal(); App.goToCompany('${e.company_id}'); return false;">${escapeHtml(e.company_name || '')}</a>
            <span class="ins-rl-date">${escapeHtml(this.insDate(e.doc_published_at || e.occurred_at))}</span>
            <span class="ins-news-cat" style="color:${th.hue}">${escapeHtml(th.label)}</span>
          </div>
          <a class="ins-rl-title" href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener">${escapeHtml(parts.headline || e.title || '')}</a>
          ${e.spend_amount ? `<span class="ins-rl-meta">Disclosed: ${escapeHtml(e.spend_amount)}</span>` : ''}
        </div></li>`;
    }).join('') + `</ul>`;
  },

  /** Full list behind Recent Results' View All. */
  insResultsList(data) {
    const items = data.items || [];
    if (!items.length) return this.insEmpty('No companies reported results in this window.');
    return `
      <p class="ins-modal-lede">${data.total_companies} ${data.total_companies === 1 ? 'company has' : 'companies have'}
        published results in this window. A percentage appears only when the headline states one.</p>
      <ul class="ins-results-list">` + items.map(i => `
        <li>
          ${this.insThumb('_results', i.image, 'sm')}
          <div class="ins-rl-body">
            <div class="ins-rl-top">
              <a href="#" class="ins-row-co" onclick="App.closeResultsModal(); App.goToCompany('${i.company_id}'); return false;">${escapeHtml(i.company_name)}</a>
              <span class="ins-rl-date">${escapeHtml(this.insDate(i.reported_at))}</span>
              ${i.delta_pct != null ? `<span class="ins-pill ${i.delta_pct >= 0 ? 'up' : 'down'}">${i.delta_pct >= 0 ? '+' : ''}${i.delta_pct}%</span>` : ''}
            </div>
            <a class="ins-rl-title" href="${escapeHtml(i.url)}" target="_blank" rel="noopener">${escapeHtml(i.headline)}</a>
            <span class="ins-rl-meta">${escapeHtml((i.kinds || [i.kind]).join(' · '))}${i.documents > 1 ? ` &middot; ${i.documents} documents` : ''}${i.region ? ' &middot; ' + escapeHtml(i.region) : ''}</span>
          </div>
        </li>`).join('') + `</ul>`;
  },

  renderSignalCard(event) {
    // `event` may be a single event or a document group produced by
    // groupEventsByDocument() — the group carries the same fields plus
    // `_initiatives` and `_groupCount`.
    const isHighConf = event.confidence >= 0.8;
    const confClass = isHighConf ? 'conf-high' : 'conf-med';
    const confPercent = Math.round(event.confidence * 100);
    const sourceIcon = sourceLabel(event.source_type);

    // The real article headline, when we have it. `event.title` is a
    // synthesized "<Company> <Initiative> Signal" label — a fallback, not news.
    const { publisher, headline } = splitHeadline(event.doc_headline);
    const displayTitle = headline || event.title;
    const isSynthetic = !headline;

    const dateStr = formatEventDate(event.doc_published_at || event.occurred_at);

    const entitiesHtml = (event.key_entities || []).map(ent =>
      `<span class="entity-pill">${escapeHtml(ent)}</span>`
    ).join('');

    const spendHtml = event.spend_amount
      ? `<span class="spend-badge"><i class="fa-solid fa-dollar-sign"></i> ${escapeHtml(event.spend_amount)}</span>`
      : '';

    // Explicitly prefixed: this is a forecast horizon for the initiative, NOT
    // the publication date. An unlabelled "2026" here read as a date.
    const timingHtml = event.timing_horizon
      ? `<span class="timing-badge" title="Stated or inferred timing for this initiative — not the publication date"><i class="fa-regular fa-calendar"></i> Timing: ${escapeHtml(event.timing_horizon)}</span>`
      : '';

    // Every initiative this document matched, so one article is one card.
    const inits = event._initiatives && event._initiatives.length
      ? event._initiatives
      : [{ initiative_name: event.initiative_name, category_id: event.category_id }];
    const initHtml = inits.map(i =>
      `<span class="initiative-pill ${escapeHtml(i.category_id || '')}">${escapeHtml(i.initiative_name || '')}</span>`
    ).join('');

    const srcCount = (event._sources || []).length;
    const moreHtml = event._mentions > 1
      ? `<span class="dup-note">${event._mentions} mentions${srcCount > 1 ? ` across ${srcCount} sources` : ' in this document'}</span>`
      : '';

    const formattedQuote = highlightTerms(event.quote_text);

    return `
      <article class="signal-card glass-card ${event.category_id}" data-id="${event.id}">
        <div class="signal-card-top">
          <div class="company-badge-wrap">
            <a href="#" class="company-name-link" onclick="App.goToCompany('${event.company_id}'); return false;">
              ${escapeHtml(event.company_name || 'Enterprise')}
            </a>
            <span class="ticker-pill">${escapeHtml(event.company_ticker || '')}</span>
            <span class="sector-pill">${escapeHtml(event.company_sector || 'BFSI')}</span>
          </div>

          <div class="signal-meta-right">
            ${(event._sources || [{ source_type: event.source_type }])
                .map(sc => `<span class="source-tag">${sourceLabel(sc.source_type)}</span>`).join('')}
            <span class="confidence-badge ${confClass}">${confPercent}% Conf</span>
          </div>
        </div>

        <h3 class="signal-title${isSynthetic ? ' signal-title-synthetic' : ''}">${escapeHtml(displayTitle)}</h3>

        <div class="signal-byline">
          ${dateStr ? `<span class="byline-date"><i class="fa-regular fa-calendar-days"></i> ${escapeHtml(dateStr)}</span>` : ''}
          ${publisher ? `<span class="byline-publisher">${escapeHtml(publisher)}</span>` : ''}
          ${moreHtml}
        </div>

        <div class="signal-initiatives">${initHtml}</div>

        <div class="signal-quote-box">
          "${formattedQuote}"
        </div>

        <div class="signal-footer-details">
          <div class="it-offering-box">
            <span class="it-offering-label"><i class="fa-solid fa-bullseye"></i> IT Vendor Fit:</span>
            <span>${escapeHtml(event.it_offering || 'Enterprise IT Modernization')}</span>
          </div>

          <div class="signal-pills-wrap">
            ${spendHtml}
            ${timingHtml}
            ${entitiesHtml}
            <a href="${event.source_url}" target="_blank" rel="noopener" class="source-link-btn">
              <i class="fa-solid fa-arrow-up-right-from-square"></i> Read source
            </a>
          </div>
        </div>
      </article>
    `;
  },

  /**
   * Render Company Card for the Master Grid
   */
  renderCompanyCard(company) {
    const aliases = (company.aliases || []).slice(0, 3).join(', ');

    return `
      <div class="company-grid-card glass-card" onclick="App.openCompanyDetail('${company.id}')">
        <div class="comp-card-header">
          <div>
            <h4 class="comp-card-name">${escapeHtml(company.name)}</h4>
            <span class="sector-pill" style="margin-top: 4px; display: inline-block;">${escapeHtml(company.industry || company.sector)}</span>
          </div>
          <span class="ticker-pill">${company.ticker ? escapeHtml(company.ticker) : 'News Only'}</span>
        </div>

        <p class="comp-card-desc">${escapeHtml(company.description || 'Public company with active disclosures.')}</p>

        <div class="comp-card-footer">
          <span class="comp-cik">${company.cik ? 'CIK: ' + escapeHtml(company.cik) : 'No SEC CIK'}</span>
          <span class="comp-signal-count"><i class="fa-solid fa-bolt"></i> ${company.events_count || 0} Signals</span>
        </div>
      </div>
    `;
  },

  /**
   * Render Jobs / Pipeline audit log table
   */
  renderJobsTable(jobs) {
    if (!jobs || jobs.length === 0) {
      return `<tr><td colspan="8" style="text-align:center; color: var(--text-dim); padding: 30px;">No ingestion pipeline runs yet. Click "Run Ingestion" to start.</td></tr>`;
    }

    return jobs.map(j => {
      const isSuccess = j.status === 'completed';
      const statusBadge = isSuccess
        ? `<span class="confidence-badge conf-high"><i class="fa-solid fa-check"></i> Completed</span>`
        : `<span class="confidence-badge conf-med"><i class="fa-solid fa-spinner fa-spin"></i> ${escapeHtml(j.status)}</span>`;

      const startTime = j.started_at ? new Date(j.started_at).toLocaleTimeString() : '-';
      const targetStr = j.target_ticker || 'Batch Universe';

      return `
        <tr>
          <td><code style="font-family: var(--font-mono); font-size: 11px;">${j.id.slice(0, 8)}</code></td>
          <td><strong>${escapeHtml(j.job_type)}</strong></td>
          <td><span class="ticker-pill">${escapeHtml(targetStr)}</span></td>
          <td>${statusBadge}</td>
          <td><strong>${j.items_ingested}</strong> docs</td>
          <td><strong class="text-cyan">${j.events_extracted}</strong> signals</td>
          <td>${startTime}</td>
          <td>${j.finished_at ? 'Done' : 'Running...'}</td>
        </tr>
      `;
    }).join('');
  },

  /**
   * Render Taxonomy Matrix
   */
  renderTaxonomyTree(taxonomyData) {
    if (!taxonomyData || !taxonomyData.categories) return '<p>No taxonomy loaded.</p>';

    return taxonomyData.categories.map(cat => `
      <div class="taxonomy-cat-block">
        <div class="tax-cat-header">
          <i class="fa-solid fa-folder-open text-cyan"></i>
          <span>${escapeHtml(cat.name)}</span>
          <span class="sector-pill">${cat.initiatives.length} Initiatives</span>
        </div>
        <p class="text-dim" style="font-size: 12px; margin-bottom: 10px;">${escapeHtml(cat.description)}</p>

        <div class="tax-init-grid">
          ${cat.initiatives.map(init => `
            <div class="tax-init-card">
              <strong>${escapeHtml(init.name)}</strong>
              <span><i class="fa-solid fa-bullseye"></i> ${escapeHtml(init.it_offering)}</span>
            </div>
          `).join('')}
        </div>
      </div>
    `).join('');
  },

  /**
   * Render Detailed Company Modal View
   */
  renderCompanyDetailModal(data, timeline) {
    const comp = data.company;
    const events = data.recent_events || [];
    const docs = data.raw_documents || [];
    const signals = timeline?.signals || [];

    const eventsListHtml = events.length > 0
      ? events.map(e => Components.renderSignalCard(e)).join('')
      : '<p class="text-dim" style="padding: 20px; text-align:center;">No signals extracted for this company in the selected date range.</p>';

    const docsListHtml = docs.map(d => `
      <div style="display:flex; justify-content:space-between; align-items:center; padding: 10px 14px; background: var(--bg-surface-elevated); border-radius: var(--radius-sm); margin-bottom: 8px;">
        <div>
          <strong style="display:block; font-size: 13px;">${escapeHtml(d.title)}</strong>
          <span class="text-dim" style="font-size: 11px;">${escapeHtml(d.doc_type)} • ${d.filing_date ? d.filing_date.split('T')[0] : 'Recent'}</span>
        </div>
        <a href="${d.url}" target="_blank" class="source-link-btn"><i class="fa-solid fa-external-link"></i> View</a>
      </div>
    `).join('');

    return `
      <div style="margin-bottom: 20px;">
        <p style="font-size: 14px; color: var(--text-secondary); line-height: 1.5; margin-bottom: 14px;">${escapeHtml(comp.description || '')}</p>
        <div style="display: flex; gap: 20px; font-size: 13px; color: var(--text-dim);">
          <span><strong>CIK:</strong> ${comp.cik || 'Not SEC-registered (news-only)'}</span>
          <span><strong>Industry:</strong> ${comp.industry}</span>
          <span><strong>Sector:</strong> ${comp.sector}</span>
        </div>
      </div>

      <div style="display: flex; gap: 10px; margin-bottom: 16px;">
        <button class="btn btn-sm btn-primary" onclick="App.triggerIngestForCompany('${comp.id}', '${escapeHtml(comp.name)}')">
          <i class="fa-solid fa-bolt"></i> Ingest Latest ${comp.cik ? 'Filings & News' : 'News'} for ${escapeHtml(comp.ticker || comp.name)}
        </button>
      </div>

      ${signals.length ? `
      <h4 style="font-family: var(--font-display); font-size: 16px; margin: 16px 0 12px;">
        <i class="fa-solid fa-ranking-star text-cyan"></i> Ranked Signals (${signals.length})
      </h4>
      <div class="signals-list">
        ${signals.slice(0, 3).map(s => Components.renderIntentSignalCard(s)).join('')}
      </div>` : ''}

      ${timeline ? `
      <h4 style="font-family: var(--font-display); font-size: 16px; margin: 24px 0 12px;">
        <i class="fa-solid fa-timeline text-emerald"></i> Activity Timeline (${timeline.event_count} events)
      </h4>
      <div class="company-timeline">
        ${Components.renderCompanyTimeline(timeline)}
      </div>` : `
      <h4 style="font-family: var(--font-display); font-size: 16px; margin: 16px 0 12px;"><i class="fa-solid fa-bolt text-cyan"></i> Extracted Market Signals (${events.length})</h4>
      <div class="signals-list">
        ${eventsListHtml}
      </div>`}

      <h4 style="font-family: var(--font-display); font-size: 16px; margin: 24px 0 12px;"><i class="fa-solid fa-file-invoice text-purple"></i> Ingested Raw Documents (${docs.length})</h4>
      <div>
        ${docsListHtml || '<p class="text-dim">No raw documents stored yet.</p>'}
      </div>
    `;
  },

  // ==========================================================
  // Phase 3 — Ranked Signals, Watchlists, Leads, Alerts
  // ==========================================================

  renderIntentSignalCard(signal) {
    const band = scoreBand(signal.intent_score);
    const timingHtml = signal.timing_window
      ? `<span class="timing-pill tw-${signal.timing_window}" title="${escapeHtml(signal.timing_estimate || '')}">${TIMING_LABELS[signal.timing_window] || escapeHtml(signal.timing_window)}</span>`
      : '';
    const spendHtml = signal.stated_spend
      ? `<span class="spend-badge"><i class="fa-solid fa-dollar-sign"></i> ${escapeHtml(signal.stated_spend)}</span>` : '';
    const statedTimingHtml = signal.stated_timing
      ? `<span class="timing-badge"><i class="fa-regular fa-calendar"></i> ${escapeHtml(signal.stated_timing)}</span>` : '';
    const sourcesHtml = (signal.source_types || []).map(s =>
      `<span class="source-tag">${sourceLabel(s)}</span>`).join('');
    const peersHtml = (signal.peer_context || []).length
      ? `<div class="peer-row"><span class="peer-label"><i class="fa-solid fa-people-group"></i> Peers on this initiative:</span>
          ${signal.peer_context.map(p => `<a href="#" class="peer-chip" onclick="App.openCompanyDetail('${p.company_id}'); return false;" title="${escapeHtml(p.scope)} peer">${escapeHtml(p.ticker || p.name)} <strong>${p.intent_score}</strong></a>`).join('')}
         </div>`
      : '';
    const delta = (signal.previous_intent_score !== null && signal.previous_intent_score !== undefined && signal.previous_intent_score !== signal.intent_score)
      ? `<span class="score-delta ${signal.intent_score > signal.previous_intent_score ? 'up' : 'down'}">${signal.intent_score > signal.previous_intent_score ? '▲' : '▼'} ${Math.abs(signal.intent_score - signal.previous_intent_score)}</span>`
      : '';

    return `
      <article class="signal-card glass-card ${escapeHtml(signal.category_id)}" data-signal-id="${signal.id}">
        <div class="signal-card-top">
          <div class="company-badge-wrap">
            <a href="#" class="company-name-link" onclick="App.openCompanyDetail('${signal.company_id}'); return false;">
              ${escapeHtml(signal.company_name || 'Enterprise')}
            </a>
            <span class="ticker-pill">${escapeHtml(signal.company_ticker || '')}</span>
            <span class="sector-pill">${escapeHtml(signal.company_industry || signal.company_sector || '')}</span>
          </div>
          <div class="signal-meta-right">
            ${delta}
            <span class="score-badge ${band.cls}" title="Intent score 0–100">
              <span class="score-num">${signal.intent_score}</span>
              <span class="score-label">${band.label}</span>
            </span>
          </div>
        </div>

        <h3 class="signal-title">${escapeHtml(signal.initiative_name)}
          <span class="sector-pill" style="margin-left:8px;">${escapeHtml(signal.category_name)}</span>
        </h3>

        <div class="signal-pills-wrap" style="margin: 8px 0 12px;">
          ${timingHtml}
          <span class="corroboration-pill" title="Distinct source types">
            <i class="fa-solid fa-layer-group"></i> ${signal.distinct_source_count} source type${signal.distinct_source_count === 1 ? '' : 's'} · ${signal.event_count} event${signal.event_count === 1 ? '' : 's'}
          </span>
          ${spendHtml}
          ${statedTimingHtml}
        </div>

        <div class="signal-footer-details">
          <div class="it-offering-box">
            <span class="it-offering-label"><i class="fa-solid fa-bullseye"></i> IT Vendor Fit:</span>
            <span>${escapeHtml(signal.it_offering || 'Enterprise IT Modernization')}</span>
          </div>
          <div class="signal-pills-wrap">${sourcesHtml}</div>
        </div>

        ${peersHtml}

        <div class="why-toggle-row">
          <button class="btn btn-sm btn-outline" onclick="App.toggleSignalWhy('${signal.id}')">
            <i class="fa-solid fa-circle-question"></i> Why this signal?
          </button>
          <span class="text-dim" style="font-size:12px;">Last evidence ${signal.last_seen_at ? signal.last_seen_at.split('T')[0] : 'n/a'}</span>
        </div>
        <div class="why-panel hidden" id="why-${signal.id}"></div>
      </article>
    `;
  },

  renderSignalBreakdown(detail) {
    const bd = detail.score_breakdown || {};
    const comps = bd.components || {};
    const maxes = bd.component_max || { evidence: 40, corroboration: 25, recency: 20, spend_timing: 15 };
    const labels = {
      evidence: 'Evidence (weighted mentions)',
      corroboration: 'Corroboration (independent sources)',
      recency: 'Recency (freshness)',
      spend_timing: 'Stated spend & timeline'
    };
    const bars = Object.keys(labels).map(k => {
      const val = comps[k] ?? 0;
      const max = maxes[k] || 1;
      const pct = Math.round((val / max) * 100);
      return `
        <div class="breakdown-row">
          <div class="breakdown-label"><span>${labels[k]}</span><span class="breakdown-val">${val} / ${max}</span></div>
          <div class="breakdown-bar"><div class="breakdown-fill" style="width:${pct}%"></div></div>
        </div>`;
    }).join('');

    const reasons = (bd.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('');
    const citations = (detail.citations || []).map(c => `
      <div class="citation-item">
        <div class="citation-top">
          <span class="source-tag">${sourceLabel(c.source_type)}</span>
          <span class="text-dim" style="font-size:11px;">${c.occurred_at ? c.occurred_at.split('T')[0] : ''} · ${Math.round((c.confidence || 0) * 100)}% conf</span>
        </div>
        <div class="signal-quote-box" style="margin:6px 0;">"${highlightTerms(c.quote_text)}"</div>
        <a href="${c.source_url}" target="_blank" rel="noopener" class="source-link-btn"><i class="fa-solid fa-arrow-up-right-from-square"></i> ${escapeHtml(c.title || 'Source')}</a>
      </div>`).join('');

    return `
      <div class="why-grid">
        <div>
          <h4 class="why-heading"><i class="fa-solid fa-chart-simple text-cyan"></i> Score breakdown → <strong>${detail.intent_score}</strong></h4>
          ${bars}
          <h4 class="why-heading" style="margin-top:14px;"><i class="fa-solid fa-list-check text-emerald"></i> Reasons</h4>
          <ul class="why-reasons">${reasons || '<li>No reasons recorded.</li>'}</ul>
          <p class="text-dim" style="font-size:12px; margin-top:10px;"><i class="fa-regular fa-clock"></i> ${escapeHtml(detail.timing_estimate || '')}</p>
        </div>
        <div>
          <h4 class="why-heading"><i class="fa-solid fa-quote-left text-purple"></i> Cited evidence (${(detail.citations || []).length} of ${detail.contributing_event_count} events)</h4>
          ${citations || '<p class="text-dim">No citations available.</p>'}
        </div>
      </div>`;
  },

  renderWatchlistCard(wl) {
    const scope = wl.company_ids.length
      ? `${wl.company_ids.length} target account${wl.company_ids.length === 1 ? '' : 's'}`
      : (wl.icp_sectors.length ? `All companies in ${wl.icp_sectors.join(', ')}` : 'No scope set');
    const inits = wl.icp_initiative_ids.length ? `${wl.icp_initiative_ids.length} initiative type${wl.icp_initiative_ids.length === 1 ? '' : 's'}` : 'All initiatives';
    return `
      <div class="watchlist-card" data-id="${wl.id}">
        <div class="watchlist-head">
          <div>
            <strong>${escapeHtml(wl.name)}</strong>
            ${wl.description ? `<p class="text-dim" style="font-size:12px; margin-top:2px;">${escapeHtml(wl.description)}</p>` : ''}
          </div>
          <span class="confidence-badge conf-med" title="Alert threshold">Alert ≥ ${wl.alert_threshold}</span>
        </div>
        <div class="watchlist-meta">
          <span><i class="fa-solid fa-building"></i> ${escapeHtml(scope)}</span>
          <span><i class="fa-solid fa-sitemap"></i> ${escapeHtml(inits)}</span>
        </div>
        <div class="watchlist-actions">
          <button class="btn btn-sm btn-primary" onclick="App.viewWatchlistLeads('${wl.id}')"><i class="fa-solid fa-ranking-star"></i> View leads</button>
          <button class="btn btn-sm btn-outline" onclick="App.openWatchlistModal('${wl.id}')"><i class="fa-solid fa-pen"></i> Edit</button>
          <button class="btn btn-sm btn-outline" onclick="App.deleteWatchlist('${wl.id}', '${escapeHtml(wl.name)}')"><i class="fa-solid fa-trash"></i></button>
        </div>
      </div>`;
  },

  renderLeadTable(result, title) {
    const leads = result.leads || [];
    if (!leads.length) {
      return `<div class="lead-empty text-dim"><i class="fa-solid fa-magnifying-glass"></i> No companies match this profile yet — lower the min score or widen the ICP.</div>`;
    }
    const rows = leads.map((l, i) => {
      const band = scoreBand(l.lead_score);
      const best = l.best_signal;
      const cites = (l.best_signal_citations || []).slice(0, 2).map(c => `
        <div class="lead-cite">
          <span class="source-tag">${sourceLabel(c.source_type)}</span>
          <span class="lead-cite-quote">"${escapeHtml((c.quote_text || '').slice(0, 140))}${(c.quote_text || '').length > 140 ? '…' : ''}"</span>
          <a href="${c.source_url}" target="_blank" rel="noopener" class="source-link-btn"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>
        </div>`).join('');
      const others = l.matching_signals.slice(1, 4).map(s => `<span class="sector-pill">${escapeHtml(s.initiative_name)} · ${s.intent_score}</span>`).join(' ');
      return `
        <tr>
          <td><strong>#${i + 1}</strong></td>
          <td>
            <a href="#" class="company-name-link" onclick="App.openCompanyDetail('${l.company.id}'); return false;">${escapeHtml(l.company.name)}</a>
            <div><span class="ticker-pill">${escapeHtml(l.company.ticker || '')}</span> <span class="sector-pill">${escapeHtml(l.company.industry || l.company.sector || '')}</span></div>
          </td>
          <td><span class="score-badge ${band.cls}"><span class="score-num">${l.lead_score}</span><span class="score-label">${band.label}</span></span></td>
          <td>
            <strong>${escapeHtml(best.initiative_name)}</strong>
            <div style="margin-top:4px;">${best.timing_window ? `<span class="timing-pill tw-${best.timing_window}">${TIMING_LABELS[best.timing_window]}</span>` : ''} ${others}</div>
            <div class="text-dim" style="font-size:11px; margin-top:4px;">${escapeHtml(best.it_offering || '')}</div>
          </td>
          <td>${cites || '<span class="text-dim">—</span>'}</td>
        </tr>`;
    }).join('');
    return `
      <div class="lead-summary">
        <strong>${escapeHtml(title || 'Ranked leads')}</strong>
        <span class="text-dim">${result.total_companies} companies · ${result.total_matching_signals} matching signals</span>
      </div>
      <div class="table-responsive">
        <table class="data-table lead-table">
          <thead><tr><th>#</th><th>Company</th><th>Lead score</th><th>Best matching signal</th><th>Cited evidence</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  },

  // ==========================================================
  // Phase 4 — Analytics, source health, timeline
  // ==========================================================

  renderHeatmap(data) {
    if (!data.rows.length || !data.cols.length) {
      return `<div class="lead-empty text-dim"><i class="fa-solid fa-table-cells"></i> No events in this date range to chart.</div>`;
    }
    const max = data.max || 1;
    const head = `<div class="heat-cell heat-corner"></div>` +
      data.cols.map(c => `<div class="heat-cell heat-col-label" title="${escapeHtml(c)}">${escapeHtml(c)}</div>`).join('');

    const body = data.rows.map((r, ri) => {
      const cells = data.cols.map((c, ci) => {
        const n = data.matrix[ri][ci];
        // Perceptual-ish ramp: sqrt keeps low-but-nonzero cells visible
        const intensity = n ? 0.12 + 0.88 * Math.sqrt(n / max) : 0;
        const style = n
          ? `background: rgba(6, 182, 212, ${intensity.toFixed(3)}); color: ${intensity > 0.55 ? '#04121a' : 'var(--text-primary)'};`
          : '';
        return `<div class="heat-cell heat-value ${n ? 'has-value' : 'empty'}" style="${style}"
                     title="${escapeHtml(r)} × ${escapeHtml(c)}: ${n} events"
                     ${n ? `onclick="App.drillIntoHeatmapCell('${escapeHtml(r)}', '${escapeHtml(c)}')"` : ''}>${n || ''}</div>`;
      }).join('');
      return `<div class="heat-cell heat-row-label" title="${escapeHtml(r)}">${escapeHtml(r)}
                <span class="heat-row-total">${data.row_totals[ri]}</span>
              </div>${cells}`;
    }).join('');

    return `
      <div class="heatmap-scroll">
        <div class="heatmap-grid" style="grid-template-columns: 180px repeat(${data.cols.length}, minmax(78px, 1fr));">
          ${head}${body}
        </div>
      </div>
      <div class="heatmap-legend text-dim">
        <span>${data.total} events</span>
        <span class="legend-scale"><span>0</span><span class="legend-bar"></span><span>${max}</span></span>
      </div>`;
  },

  renderSourceHealth(data) {
    const rows = data.sources.map(s => {
      const dot = s.status === 'healthy' ? 'dot-green' : (s.status === 'stale' ? 'dot-amber' : 'dot-red');
      const age = s.age_hours === null ? 'never' :
        (s.age_hours < 24 ? `${s.age_hours}h ago` : `${(s.age_hours / 24).toFixed(1)}d ago`);
      const rate = s.success_rate === null ? '<span class="text-dim">no runs yet</span>' : `${s.success_rate}%`;
      return `
        <tr>
          <td><span class="health-dot ${dot}"></span> ${sourceLabel(s.source_type)}</td>
          <td><span class="${s.status === 'stale' ? 'text-yellow' : 'text-dim'}">${escapeHtml(s.status.replace('_',' '))}</span></td>
          <td>${escapeHtml(age)}</td>
          <td>${s.documents_in_window} <span class="text-dim">/ ${s.total_documents}</span></td>
          <td>${s.events_in_window}</td>
          <td>${rate}</td>
          <td class="health-error">${s.last_error ? `<span title="${escapeHtml(s.last_error)}">${escapeHtml(s.last_error.slice(0, 60))}…</span>` : '<span class="text-dim">—</span>'}</td>
        </tr>`;
    }).join('');

    return `
      <div class="health-header">
        <h3><i class="fa-solid fa-heart-pulse text-emerald"></i> Source Health
          <span class="text-dim" style="font-weight:400; font-size:12px;">
            ${data.healthy_count} healthy · ${data.stale_count} stale · SLA ${data.sla_hours}h
          </span>
        </h3>
        <span class="text-dim" style="font-size:12px;">
          ${data.scheduler?.next_run ? `Next run ${new Date(data.scheduler.next_run).toLocaleString()}` : 'Scheduler idle'}
        </span>
      </div>
      <div class="table-responsive">
        <table class="data-table health-table">
          <thead><tr>
            <th>Source</th><th>Status</th><th>Last document</th>
            <th>Docs (range/all)</th><th>Events in range</th><th>Success rate</th><th>Last error</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  },

  renderCompanyTimeline(data) {
    if (!data.months.length) {
      return `<div class="lead-empty text-dim"><i class="fa-regular fa-calendar-xmark"></i> No events for this company in the selected date range.</div>`;
    }
    return data.months.map(m => {
      const label = new Date(m.month + '-01').toLocaleDateString(undefined, { year: 'numeric', month: 'long' });
      const items = m.events.map(e => `
        <div class="timeline-item">
          <div class="timeline-dot"></div>
          <div class="timeline-body">
            <div class="timeline-top">
              <span class="source-tag">${sourceLabel(e.source_type)}</span>
              <span class="text-dim" style="font-size:11px;">${(e.occurred_at || '').slice(0, 10)} · ${Math.round((e.confidence || 0) * 100)}%</span>
            </div>
            <strong class="timeline-title">${escapeHtml(e.initiative_name)}</strong>
            <p class="timeline-quote">"${escapeHtml((e.quote_text || '').slice(0, 180))}${(e.quote_text || '').length > 180 ? '…' : ''}"</p>
            <a href="${e.source_url}" target="_blank" rel="noopener" class="source-link-btn">
              <i class="fa-solid fa-arrow-up-right-from-square"></i> Source
            </a>
          </div>
        </div>`).join('');
      return `<div class="timeline-month">
                <div class="timeline-month-label">${escapeHtml(label)} <span class="text-dim">(${m.events.length})</span></div>
                ${items}
              </div>`;
    }).join('');
  },

  // ==========================================================
  // Phase 5 — Vendor graph & tenders
  // ==========================================================

  renderVendorGraph(data) {
    if (!data.vendors?.length) {
      return `<div class="lead-empty text-dim"><i class="fa-solid fa-diagram-project"></i> No vendor mentions in this range. Widen the date range or ingest more companies.</div>`;
    }
    const maxMentions = Math.max(...data.vendors.map(v => v.mentions));
    const cats = Object.entries(data.by_category || {})
      .map(([c, n]) => `<span class="sector-pill">${escapeHtml(c)} · ${n}</span>`).join(' ');

    const rows = data.vendors.map(v => {
      const width = Math.round((v.mentions / maxMentions) * 100);
      const chips = v.companies.slice(0, 10).map(c =>
        `<a href="#" class="peer-chip" onclick="App.openCompanyDetail('${c.company_id}'); return false;" title="${escapeHtml(c.name)} — ${c.mentions} mention(s)">${escapeHtml(c.ticker || c.name.slice(0, 14))}</a>`
      ).join('');
      const more = v.companies.length > 10 ? `<span class="text-dim" style="font-size:11px;">+${v.companies.length - 10} more</span>` : '';
      const inits = v.initiatives.slice(0, 3).map(i => `<span class="sector-pill">${escapeHtml(i.name)} · ${i.count}</span>`).join(' ');
      return `
        <div class="vendor-row">
          <div class="vendor-head">
            <div>
              <strong class="vendor-name">${escapeHtml(v.vendor)}</strong>
              <span class="sector-pill">${escapeHtml(v.category)}</span>
            </div>
            <span class="text-dim" style="font-size:12px;">
              <strong class="text-cyan">${v.company_count}</strong> companies · ${v.mentions} mentions
            </span>
          </div>
          <div class="breakdown-bar"><div class="breakdown-fill" style="width:${width}%"></div></div>
          <div class="vendor-companies">${chips}${more}</div>
          <div class="vendor-initiatives">${inits}</div>
        </div>`;
    }).join('');

    return `
      <div class="vendor-legend">${cats}</div>
      <div class="vendor-list">${rows}</div>
      <p class="text-dim" style="font-size:11px; margin-top:10px;">
        ${data.total_vendors} vendors across ${data.events_scanned} events. Aliases are merged
        (Azure/Microsoft Azure, AWS/Amazon Web Services, GCP/Google Cloud).
      </p>`;
  },

  renderTenders(data) {
    if (!data.tenders?.length) {
      return `<div class="lead-empty text-dim"><i class="fa-solid fa-gavel"></i> No tenders for these filters. Hit Refresh to pull the latest notices from TED.</div>`;
    }
    const rows = data.tenders.map(t => {
      const deadline = t.deadline_at ? t.deadline_at.slice(0, 10) : null;
      const daysLeft = deadline ? Math.ceil((new Date(deadline) - new Date()) / 86400000) : null;
      const urgency = daysLeft === null ? '' :
        (daysLeft < 0 ? 'closed' : (daysLeft <= 14 ? 'urgent' : 'open'));
      const deadlineHtml = deadline
        ? `<span class="deadline-pill ${urgency}">${daysLeft < 0 ? 'Closed' : daysLeft + 'd left'} · ${deadline}</span>`
        : '<span class="text-dim">no deadline</span>';
      return `
        <tr>
          <td style="white-space:nowrap;">${(t.published_at || '').slice(0, 10)}</td>
          <td><span class="ticker-pill">${escapeHtml(t.buyer_country || '—')}</span></td>
          <td>
            <strong>${escapeHtml(t.buyer_name || 'Unnamed buyer')}</strong>
            ${t.matched_company_name ? `<div><span class="sector-pill">tracked: ${escapeHtml(t.matched_company_name)}</span></div>` : ''}
          </td>
          <td>${escapeHtml((t.title || '').slice(0, 110))}</td>
          <td>${deadlineHtml}</td>
          <td>${t.value_amount ? escapeHtml(t.value_amount) : '<span class="text-dim">—</span>'}</td>
          <td><a href="${t.url}" target="_blank" rel="noopener" class="source-link-btn"><i class="fa-solid fa-arrow-up-right-from-square"></i></a></td>
        </tr>`;
    }).join('');
    return `
      <div class="table-responsive">
        <table class="data-table">
          <thead><tr>
            <th>Published</th><th>Country</th><th>Buyer</th><th>Tender</th><th>Deadline</th><th>Value</th><th></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  },

  // ==========================================================
  // Phase 6 — Company page & global search
  // ==========================================================

  /* ================= COMPANY PAGE =================
     Laid out after the user's Trend Radar reference. Sections with no data
     source (employee count, CEO name, share price, website) are absent rather
     than filled with placeholders. */

  cpMoney(value, currency) {
    if (value == null) return '—';
    const sym = { USD: '$', EUR: '€', GBP: '£', JPY: '¥', CHF: 'CHF ', CAD: 'C$', AUD: 'A$' }[currency] || (currency ? currency + ' ' : '');
    const abs = Math.abs(value);
    if (abs >= 1e12) return `${sym}${(value / 1e12).toFixed(2)}T`;
    if (abs >= 1e9) return `${sym}${(value / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${sym}${(value / 1e6).toFixed(0)}M`;
    return `${sym}${value.toLocaleString()}`;
  },

  cpDelta(pct, { goodWhenUp = true } = {}) {
    if (pct == null) return '';
    const up = pct >= 0;
    const cls = (up === goodWhenUp) ? 'up' : 'down';
    return `<span class="cp-delta ${cls}">${up ? '▲' : '▼'} ${Math.abs(pct)}% YoY</span>`;
  },

  cpMonogram(name) {
    return String(name || '?').replace(/[^A-Za-z ]/g, ' ').trim()
      .split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
  },

  renderCompanyPage(data) {
    const c = data.company || {};
    const t = data.totals || {};
    const band = t.top_score != null ? scoreBand(t.top_score) : null;

    const keySignals = (data.key_signals || []).map(k => `
      <li><span class="cp-key-dot" style="background:${this.insTheme(
        k.category_id === 'tech_initiatives' ? 'cloud_migration'
          : k.category_id === 'spending_signals' ? 'vendor_partnership_rfp'
          : k.category_id === 'organizational_change' ? 'executive_leadership_change'
          : k.category_id === 'regulatory_compliance' ? 'regulatory_compliance_tech'
          : 'digital_transformation').hue}"></span>
        <span class="cp-key-label">${escapeHtml(k.label)}</span>
        <span class="cp-key-n">${k.count}</span></li>`).join('');

    return `
      <div class="cp">
        <div class="cp-back">
          <button type="button" class="cp-back-btn" onclick="App.goBack()">
            <i class="fa-solid fa-arrow-left"></i> Back</button>
        </div>

        <header class="cp-hero">
          <div class="cp-id">
            <span class="cp-logo">${escapeHtml(this.cpMonogram(c.name))}</span>
            <div class="cp-id-text">
              <h2>${escapeHtml(c.name || '')}${c.ticker ? `<span class="cp-ticker">${escapeHtml(c.ticker)}</span>` : ''}</h2>
              <p class="cp-meta">${escapeHtml(c.sub_industry || c.industry || '')}${c.vertical ? ' · ' + escapeHtml(c.vertical) : ''}${c.country ? ' · ' + escapeHtml(c.country) : ''}${c.region ? ' · ' + escapeHtml(c.region) : ''}</p>
              ${c.description && !/company in the BFSI sector\.?$/i.test(c.description) ? `<p class="cp-desc">${escapeHtml(c.description)}</p>` : ''}
              ${this.cpHeroFacts(data)}
            </div>
          </div>
          <aside class="cp-key">
            <h4>Key signals <span>(last ${data.signal_days} days)</span></h4>
            ${keySignals ? `<ul>${keySignals}</ul>` : `<p class="ins-empty">No activity in the last ${data.signal_days} days.</p>`}
            ${band ? `<div class="cp-topscore"><span class="score-badge ${band.cls}">${band.label}</span>
                <span>strongest opportunity (${t.top_score} ${t.top_score === 1 ? 'point' : 'points'}) · ${t.signals} ${t.signals === 1 ? 'opportunity' : 'opportunities'}</span></div>` : ''}
          </aside>
        </header>

        <div class="cp-masonry">
          <section class="ins-tile cp-w4" id="cpFinancials">
            <header class="ins-head"><h3><i class="fa-solid fa-chart-column"></i> Executive Snapshot <button type="button" class="ins-info" aria-label="About this tile" data-tip="Headline financials from the company&#x27;s latest SEC filings (XBRL data): revenue, net income, assets, equity and returns, with the year-on-year change. Only companies that file with the SEC report here.">i</button></h3></header>
            <div class="ins-body">${this.cpFinancials(data.financials)}</div>
          </section>

          <section class="ins-tile cp-w4" id="cpPriorities">
            <header class="ins-head"><h3><i class="fa-solid fa-bullseye"></i> Strategic Priorities <button type="button" class="ins-info" aria-label="About this tile" data-tip="The company&#x27;s opportunities in the selected date range, strongest first. Strength counts unique documents: 3 points per filing, earnings deck or IR release, 2 per job posting, 1 per news story. High is 6+, Medium 3-5, Low 1-2.">i</button></h3></header>
            <div class="ins-body">${this.cpPriorities(data.priorities)}</div>
          </section>

          <section class="ins-tile cp-w4" id="cpTechnology">
            <header class="ins-head"><h3><i class="fa-solid fa-microchip"></i> Technology &amp; Transformation <button type="button" class="ins-info" aria-label="About this tile" data-tip="Technology initiatives mentioned in the company&#x27;s documents in the date range, grouped by area, with the IT services that fit each.">i</button></h3></header>
            <div class="ins-body">${this.cpTechnology(data.technology)}</div>
          </section>

          <section class="ins-tile cp-w4" id="cpLeadership">
            <header class="ins-head"><h3><i class="fa-solid fa-user-tie"></i> Leadership Changes <button type="button" class="ins-info" aria-label="About this tile" data-tip="Technology leadership changes reported in the date range, then the named technology leaders on file for this company, taken from their public LinkedIn profiles.">i</button></h3></header>
            <div class="ins-body">${this.cpLeadership(data.leadership, data.leaders)}</div>
          </section>

          <section class="ins-tile cp-w4" id="cpDeals">
            <header class="ins-head"><h3><i class="fa-solid fa-handshake"></i> Partnerships / Deals / RFPs <button type="button" class="ins-info" aria-label="About this tile" data-tip="Partnerships, RFPs, stated budgets and technology-related M&amp;A in the date range, and the technology suppliers named in the company&#x27;s documents.">i</button></h3></header>
            <div class="ins-body">${this.cpDeals(data.deals, data.vendors)}</div>
          </section>

          <section class="ins-tile cp-w4" id="cpWhy">
            <header class="ins-head"><h3><i class="fa-solid fa-lightbulb"></i> Why This Matters <button type="button" class="ins-info" aria-label="About this tile" data-tip="Statements of fact drawn from the tiles on this page - the strongest opportunity, the busiest technology theme and recent moves. Not written by a model.">i</button></h3></header>
            <div class="ins-body">${this.cpWhy(data.why_it_matters)}</div>
          </section>

        </div>
        <div class="cp-full">
          <section class="ins-tile cp-w12" id="cpNews">
            <header class="ins-head"><h3><i class="fa-regular fa-newspaper"></i> Recent News &amp; Updates <button type="button" class="ins-info" aria-label="About this tile" data-tip="The latest news and regulatory items about the company, newest first.">i</button></h3></header>
            <div class="ins-body">${this.cpNews(data.news, data.regulatory)}</div>
          </section>
        </div>
      </div>`;
  },

  /** The header's working area: counts, top opportunities and the key contact. */
  cpHeroFacts(data) {
    const pr = data.priorities || [], leaders = data.leaders || [], vendors = data.vendors || [];
    const high = pr.filter(p => p.priority === 'High').length;
    const news = (data.news || []).length;
    const facts = [
      `<b>${pr.length}</b> ${pr.length === 1 ? 'opportunity' : 'opportunities'}${high ? ` · <b>${high}</b> High` : ''}`,
      leaders.length ? `<b>${leaders.length}</b> ${leaders.length === 1 ? 'leader' : 'leaders'} on file` : '',
      vendors.length ? `<b>${vendors.length}</b> ${vendors.length === 1 ? 'supplier' : 'suppliers'} named` : '',
      news ? `<b>${news}</b> recent ${news === 1 ? 'story' : 'stories'}` : '',
    ].filter(Boolean).map(f => `<span class="cp-fact">${f}</span>`).join('');
    const tops = pr.slice(0, 3).map(p => `
      <a href="#cpPriorities" class="cp-top-opp" onclick="document.getElementById('cpPriorities')?.scrollIntoView({behavior:'smooth', block:'start'}); return false;"
         title="${escapeHtml(p.points_explained || '')}">${escapeHtml(p.initiative_name)} ${this.oppStrength(p.priority)}</a>`).join('');
    const lead = leaders[0];
    return `
      <div class="cp-facts">${facts}</div>
      ${tops ? `<div class="cp-hero-row"><span class="cp-hero-lbl">Top opportunities</span><div class="cp-top-opps">${tops}</div></div>` : ''}
      ${lead ? `<div class="cp-hero-row"><span class="cp-hero-lbl">Key contact</span>
        <a class="cp-contact" href="${escapeHtml(lead.linkedin_url)}" target="_blank" rel="noopener">${escapeHtml(lead.name)}
          <i class="fa-brands fa-linkedin"></i></a><span class="cp-contact-role">${escapeHtml(lead.headline)}</span></div>` : ''}`;
  },

  cpFinancials(f) {
    if (!f || !f.available) {
      return `<p class="ins-empty">${escapeHtml((f && f.reason) || 'No financial filings available.')}</p>
        <p class="cp-note">Figures come from SEC filings, so they exist only for companies that file with the SEC.</p>`;
    }
    const cards = (f.metrics || []).map(m => `
      <div class="cp-fin">
        <span class="cp-fin-val">${this.cpMoney(m.value, m.currency)}</span>
        <span class="cp-fin-label">${escapeHtml(m.label)}</span>
        ${this.cpDelta(m.change_pct)}
      </div>`).join('');
    const roe = f.roe_pct != null ? `
      <div class="cp-fin">
        <span class="cp-fin-val">${f.roe_pct}%</span>
        <span class="cp-fin-label">Return on equity</span>
        <span class="cp-fin-sub">net income ÷ average equity</span>
      </div>` : '';
    const first = (f.metrics || [])[0];
    return `<div class="cp-fins">${cards}${roe}</div>
      <p class="cp-note">${escapeHtml(f.source)}${first ? ` · FY${first.fy}, period ending ${first.period_end} (${first.form})` : ''}</p>`;
  },

  cpPriorities(items) {
    if (!items || !items.length) return `<p class="ins-empty">No ranked priorities in this window.</p>`;
    return `<ul class="cp-prio">` + items.map(p => `
      <li>
        <span class="cp-prio-icon" style="background:${this.insTheme(p.initiative_id).hue}22;color:${this.insTheme(p.initiative_id).hue}">
          <i class="fa-solid ${this.insTheme(p.initiative_id).icon}"></i></span>
        <div class="cp-prio-body">
          <div class="cp-prio-top">
            <span class="cp-prio-name">${escapeHtml(p.initiative_name)}</span>
            <span class="cp-prio-tag cp-prio-${p.priority.toLowerCase()}">${p.priority} strength</span>
          </div>
          ${p.it_offering ? `<p class="cp-prio-offer">${escapeHtml(p.it_offering)}</p>` : ''}
          <p class="cp-prio-evidence"><b>Evidence:</b> ${escapeHtml(p.points_explained || `${p.intent_score} points`)}
            ${p.timing_window ? ` · timing ${escapeHtml(p.timing_window)}` : ''}</p>
        </div>
      </li>`).join('') + `</ul>`;
  },

  cpTechnology(tech) {
    if (!tech || !tech.length) return `<p class="ins-empty">No technology initiatives in this window.</p>`;
    const total = tech.reduce((a, t) => a + t.mentions, 0);
    const tabs = tech.slice(0, 5).map((t, i) => `
      <button type="button" class="cp-tech-tab${i === 0 ? ' active' : ''}" data-tech="${escapeHtml(t.initiative_id)}">
        ${escapeHtml(this.insTheme(t.initiative_id).label)} <b>${t.mentions}</b></button>`).join('');
    const panes = tech.slice(0, 5).map((t, i) => `
      <div class="cp-tech-pane${i === 0 ? ' active' : ''}" data-tech="${escapeHtml(t.initiative_id)}">
        <p class="cp-tech-offer"><b>Fits:</b> ${escapeHtml(t.it_offering || '')}</p>
        <ul class="cp-tech-moves">${t.moves.map(m => `
          <li><a href="${escapeHtml(m.url)}" target="_blank" rel="noopener">${escapeHtml(splitHeadline(m.title).headline || m.title)}</a>
            <span>${escapeHtml(this.insDate(m.date))}</span></li>`).join('')}</ul>
      </div>`).join('');
    return `
      <div class="cp-tech-total"><b>${total}</b> technology mentions across ${tech.length}
        ${tech.length === 1 ? 'initiative' : 'initiatives'}</div>
      <div class="cp-tech-tabs">${tabs}</div>
      <div class="cp-tech-panes">${panes}</div>`;
  },

  cpLeadership(items, leaders) {
    const moves = (items || []).length ? this._cpMoves(items)
      : `<p class="ins-empty cp-quiet">No leadership changes reported in this window.</p>`;
    const people = (leaders || []).length ? `
      <h4 class="cp-sub">Technology leaders on file</h4>
      <ul class="cp-people">` + leaders.map(l => {
        const ini = String(l.name || '?').replace(/[^A-Za-z ]/g, ' ').trim().split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
        return `<li>
          <span class="ins-avatar">${escapeHtml(ini)}</span>
          <div class="cp-person">
            <a class="opp-person-name" href="${escapeHtml(l.linkedin_url)}" target="_blank" rel="noopener">${escapeHtml(l.name)} <i class="fa-brands fa-linkedin"></i></a>
            <span class="opp-person-title" title="${escapeHtml(l.headline)}">${escapeHtml(l.headline)}</span>
            ${(l.areas || []).length ? `<span class="cp-areas">${l.areas.map(a => `<i>${escapeHtml(a)}</i>`).join('')}</span>` : ''}
          </div></li>`;
      }).join('') + `</ul>` : '';
    return moves + people;
  },

  _cpMoves(items) {
    return `<ul class="ins-lead">` + items.map(m => {
      const h = splitHeadline(m.title).headline || m.title;
      const text = this.INS_MOVE.test(h) ? h : (m.quote || h);
      return `<li>
        <span class="ins-avatar"><i class="fa-solid fa-user"></i></span>
        <div>
          <a class="ins-lead-title" href="${escapeHtml(m.url)}" target="_blank" rel="noopener">${escapeHtml(String(text).slice(0, 150))}</a>
          <span class="ins-lead-meta">${escapeHtml(this.insDate(m.date))}</span>
        </div></li>`;
    }).join('') + `</ul>`;
  },

  cpDeals(items, vendors) {
    const deals = (items || []).length ? this._cpDealRows(items)
      : `<p class="ins-empty cp-quiet">No partnerships, RFPs or budgets disclosed in this window.</p>`;
    const sup = (vendors || []).length ? `
      <h4 class="cp-sub">Technology suppliers named</h4>
      <div class="cp-vendors">` + vendors.map(v => `
        <span class="cp-vendor" title="${escapeHtml(v.category)} - named in ${v.mentions} ${v.mentions === 1 ? 'passage' : 'passages'}">
          ${escapeHtml(v.name)}<b>${v.mentions}</b></span>`).join('') + `</div>` : '';
    return deals + sup;
  },

  _cpDealRows(items) {
    return `<ul class="ins-rows">` + items.map(d => {
      const th = this.insTheme(d.initiative_id);
      return `<li class="ins-row">
        <span class="cp-deal-kind" style="background:${th.hue}22;color:${th.hue}">${escapeHtml(th.label)}</span>
        <div class="ins-row-main">
          <a class="ins-row-co" href="${escapeHtml(d.url)}" target="_blank" rel="noopener">${escapeHtml(splitHeadline(d.title).headline || d.title)}</a>
          <span class="ins-row-sub">${escapeHtml(this.insDate(d.date))}</span>
        </div>
        ${d.spend_amount ? `<span class="ins-pill up">${escapeHtml(d.spend_amount)}</span>` : ''}
      </li>`;
    }).join('') + `</ul>`;
  },

  cpWhy(items) {
    if (!items || !items.length) return `<p class="ins-empty">Not enough activity to draw conclusions yet.</p>`;
    return `<ul class="cp-why">` + items.map(w => `
      <li>
        <span class="cp-why-tag">${escapeHtml(w.tag)}</span>
        <div><b>${escapeHtml(w.title)}</b><p>${escapeHtml(w.detail)}</p></div>
      </li>`).join('') + `</ul>
      <p class="cp-note">Computed from the sections above — not a model-written summary.</p>`;
  },

  cpNews(news, regulatory) {
    const items = [...(news || []), ...(regulatory || [])]
      .sort((a, b) => String(b.date).localeCompare(String(a.date)))
      .slice(0, 8);
    if (!items.length) return `<p class="ins-empty">No news in this window.</p>`;
    return `<div class="cp-news">` + items.map(n => {
      const parts = splitHeadline(n.title);
      const th = this.insTheme(n.initiative_id);
      return `
        <a class="cp-news-card" href="${escapeHtml(n.url)}" target="_blank" rel="noopener">
          ${this.insThumb(n.initiative_id, n.image, 'md')}
          <span class="cp-news-cat" style="color:${th.hue}">${escapeHtml(th.label)}</span>
          <span class="cp-news-title">${escapeHtml(parts.headline || n.title)}</span>
          <span class="cp-news-meta">${parts.publisher ? escapeHtml(parts.publisher) + ' · ' : ''}${escapeHtml(this.insDate(n.date))}</span>
        </a>`;
    }).join('') + `</div>`;
  },

  renderGlobalSearchResults(companies, term) {
    const cos = companies.slice(0, 8).map(c => `
      <div class="gs-item" onmousedown="App.goToCompany('${c.id}')">
        <div class="gs-item-main">
          <i class="fa-solid fa-building text-dim"></i>
          <span class="gs-item-name">${escapeHtml(c.name)}</span>
        </div>
        <div style="display:flex; gap:6px; align-items:center; flex-shrink:0;">
          ${c.ticker ? `<span class="ticker-pill">${escapeHtml(c.ticker)}</span>` : ''}
          ${c.region ? `<span class="sector-pill">${escapeHtml(c.region)}</span>` : ''}
        </div>
      </div>`).join('');

    return `
      ${cos ? `<div class="gs-section-label">Companies</div>${cos}` : `<div class="gs-empty">No companies match "${escapeHtml(term)}"</div>`}
      <div class="gs-section-label">Signals</div>
      <div class="gs-item" onmousedown="App.searchSignalsFor('${escapeHtml(term).replace(/'/g, "\\'")}')">
        <div class="gs-item-main">
          <i class="fa-solid fa-magnifying-glass text-cyan"></i>
          <span class="gs-item-name">Search signals for "<strong>${escapeHtml(term)}</strong>"</span>
        </div>
      </div>`;
  },

  renderAlertItem(alert) {
    const band = scoreBand(alert.intent_score);
    const when = alert.created_at ? new Date(alert.created_at).toLocaleString() : '';
    return `
      <div class="alert-item ${alert.is_read ? '' : 'unread'}" data-id="${alert.id}">
        <div class="alert-top">
          <span class="score-badge ${band.cls}"><span class="score-num">${alert.intent_score}</span></span>
          <div class="alert-body">
            <a href="#" class="company-name-link" onclick="App.openCompanyDetail('${alert.company_id}'); return false;">${escapeHtml(alert.company_name || '')}</a>
            <span class="text-dim"> — ${escapeHtml(alert.initiative_name || alert.initiative_id)}</span>
            <p class="alert-msg">${escapeHtml(alert.message)}</p>
            <div class="alert-meta text-dim">
              ${alert.watchlist_name ? `<span><i class="fa-solid fa-list-ul"></i> ${escapeHtml(alert.watchlist_name)}</span>` : ''}
              <span><i class="fa-regular fa-clock"></i> ${when}</span>
              ${(alert.delivered_channels || []).includes('slack') ? '<span><i class="fa-brands fa-slack"></i> Slack</span>' : ''}
            </div>
          </div>
          ${alert.is_read ? '' : `<button class="btn-text" title="Mark read" onclick="App.markAlertRead('${alert.id}')"><i class="fa-solid fa-check"></i></button>`}
        </div>
      </div>`;
  }
};

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function highlightTerms(text) {
  if (!text) return '';
  const escaped = escapeHtml(text);
  const keywords = ['cloud', 'AWS', 'Azure', 'Google Cloud', 'AI', 'GenAI', 'modernization', 'cybersecurity', 'Basel III', 'First Republic', 'invest', 'transform', 'partnership'];
  let result = escaped;
  keywords.forEach(kw => {
    const reg = new RegExp(`\\b(${kw})\\b`, 'gi');
    result = result.replace(reg, '<span class="highlight">$1</span>');
  });
  return result;
}
