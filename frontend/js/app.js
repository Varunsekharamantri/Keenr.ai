/**
 * Keenr.ai - Main Application Logic
 */

const API_BASE = '/api';

const App = {
  state: {
    feedFamily: (function () { try { return localStorage.getItem('msp.feedFamily') || 'all'; } catch (e) { return 'all'; } })(),
    ovRegion: (function () { try { return localStorage.getItem('msp.ovRegion') || 'all'; } catch (e) { return 'all'; } })(),
    ovResults: null,
    oppRegion: (function () { try { return localStorage.getItem('msp.oppRegion') || 'all'; } catch (e) { return 'all'; } })(),
    oppData: null,
    overviewStats: null,
    taxonomy: null,
    companies: [],
    events: [],
    jobs: [],
    filters: {
      sector: 'all',
      category: 'all',
      source: 'all',
      confidence: 0.70,
      search: '',
      region: 'all'
    },
    activeView: 'overview',   // the salesperson's front page
    categoryChart: null,
    trendChart: null,
    // Phase 3
    signals: [],
    signalFilters: { sector: 'all', category: 'all', minScore: 40, search: '', timing: 'all', region: 'all' },
    dateRange: { preset: '30d', start: null, end: null },
    watchlists: [],
    alerts: [],
    editingWatchlistId: null,
    wlSelectedCompanies: []
  },

  SECTORS: ['BFSI', 'Healthcare', 'Technology', 'Retail & Packaging', 'Manufacturing', 'Energy', 'ESG'],

  // preset -> trailing days ('all' = no lower bound, 'custom' = explicit dates)
  RANGE_PRESETS: { '7d': 7, '30d': 30, '90d': 90, '6m': 182, '12m': 365 },
  RANGE_LABELS: { '7d': 'last 7 days', '30d': 'last 30 days', '90d': 'last 90 days', '6m': 'last 6 months', '12m': 'last 12 months', 'all': 'all time' },

  async init() {
    console.log("Initializing Keenr.ai...");
    const radar = this.showRadar();
    try {
      this.loadAppConfig();
      this.restoreDateRange();
      this.setupEventListeners();
      window.addEventListener('hashchange', () => this.router());
      await this.loadTaxonomy();
      this.loadFreshness();
      await this.loadCompanies();   // BFSI by default; also backs the global search box
      // The first view: the Industry page, or a deep link like #/company/{id}.
      const first = location.hash ? this.router() : this.loadOverview();
      // Never hold the page hostage: lift the radar after 20s regardless.
      await Promise.race([Promise.resolve(first).catch(() => {}), new Promise(r => setTimeout(r, 20000))]);
    } finally {
      // At least one sweep, counted from the first paint, so a fast (cached)
      // load does not flash the radar for a split second.
      this.hideRadar(radar, Math.max(0, 900 - performance.now()));
    }
  },

  // ==========================================================
  // Radar loading screen (markup in index.html, so the first load is covered
  // before any script runs). Shown for the first load and for every date-range
  // change; the dashboard stays behind it, blurred.
  // ==========================================================
  BOOT_MESSAGES: ['Scanning BFSI signals…', 'Reading filings and news…', 'Ranking opportunities…',
                  'Mapping decision makers…', 'Writing the briefing…'],

  /** Show the radar; returns a token for hideRadar. `first` is the opening line. */
  showRadar(first) {
    const el = document.getElementById('bootLoader');
    const status = document.getElementById('bootStatus');
    const token = this._radarToken = (this._radarToken || 0) + 1;
    this._radarShownAt = performance.now();
    if (!el) return token;
    clearInterval(this._radarTicker);
    const lines = first ? [first, ...this.BOOT_MESSAGES.slice(1)] : this.BOOT_MESSAGES;
    if (status) { status.textContent = lines[0]; status.classList.remove('is-swapping'); }
    el.classList.remove('is-done');
    document.body.classList.add('radar-open');     // pauses tile entrances behind it
    let i = 0;
    this._radarTicker = setInterval(() => {
      if (!status) return;
      status.classList.add('is-swapping');
      setTimeout(() => {
        i = (i + 1) % lines.length;
        status.textContent = lines[i];
        status.classList.remove('is-swapping');
      }, 250);
    }, 1600);
    return token;
  },

  /**
   * Lift the radar - unless a newer load has shown it since (a second range
   * click while the first was loading): that one lifts it when it finishes.
   */
  hideRadar(token, minVisible = 600) {
    const wait = Math.max(0, minVisible - (performance.now() - (this._radarShownAt || 0)));
    setTimeout(() => {
      if (token !== this._radarToken) return;
      clearInterval(this._radarTicker);
      document.getElementById('bootLoader')?.classList.add('is-done');
      document.body.classList.remove('radar-open');   // tiles animate in now
    }, wait);
  },

  // ==========================================================
  // Phase 6 — hash routing (bookmarkable views)
  // ==========================================================

  /** Hash routing keeps company views shareable without server rewrite rules. */
  router() {
    if (this._suppressRouter) { this._suppressRouter = false; return; }
    const hash = (location.hash || '').replace(/^#\/?/, '');
    if (!hash) return;
    const [route, param] = hash.split('/');
    if (route === 'company' && param) {
      return this.showCompanyPage(param);
    } else if (['overview', 'opportunity', 'companies'].includes(route)) {
      this.switchView(route, { fromRouter: true });
      return this._viewLoading;
    }
  },

  goToCompany(companyId) {
    this.closeGlobalSearch();
    location.hash = `#/company/${companyId}`;
  },

  async showCompanyPage(companyId) {
    document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('viewCompanyPage')?.classList.add('active');
    if (this.state.activeView && this.state.activeView !== 'company') this.state.prevView = this.state.activeView;
    this.state.activeView = 'company';
    this.state.currentCompanyId = companyId;

    const box = document.getElementById('companyPageContent');
    box.innerHTML = `<div class="ins-loading" style="min-height:420px">Loading company…</div>`;
    window.scrollTo(0, 0);

    try {
      // One request builds the whole page, so its sections can never disagree.
      const res = await fetch(`${API_BASE}/companies/${companyId}/profile?${this.rangeParams().toString()}`);
      if (!res.ok) throw new Error(`profile -> ${res.status}`);
      box.innerHTML = Components.renderCompanyPage(await res.json());
      Components.layoutMasonry(box);
      Components.animateIn(box);
      if (!this._masonryBound) {
        this._masonryBound = true;
        let tm;
        window.addEventListener('resize', () => {
          clearTimeout(tm);
          tm = setTimeout(() => Components.layoutMasonry(document.getElementById('viewCompanyPage')), 150);
        });
      }
    } catch (e) {
      console.error('company profile failed', e);
      box.innerHTML = '<p class="ins-empty">Could not load this company.</p>';
    }
  },

  /** Technology & Transformation initiative tabs. */
  switchTechTab(initiativeId) {
    document.querySelectorAll('.cp-tech-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.tech === initiativeId));
    document.querySelectorAll('.cp-tech-pane').forEach(p =>
      p.classList.toggle('active', p.dataset.tech === initiativeId));
  },

  exportCompanyEvents(companyId) {
    const p = this.withRange(new URLSearchParams());
    // events.csv has no company filter, so scope by the company's own events
    window.location.href = `${API_BASE}/companies/${companyId}/timeline?${p.toString()}`;
  },

  // ==========================================================
  // Phase 6 — global search
  // ==========================================================

  async runGlobalSearch(term) {
    const box = document.getElementById('globalSearchResults');
    if (!box) return;
    if (!term || term.trim().length < 2) { this.closeGlobalSearch(); return; }
    try {
      const res = await fetch(`${API_BASE}/companies?search=${encodeURIComponent(term.trim())}&limit=10`);
      const companies = await res.json();
      box.innerHTML = Components.renderGlobalSearchResults(companies, term.trim());
      box.classList.remove('hidden');
    } catch (e) {
      this.closeGlobalSearch();
    }
  },

  closeGlobalSearch() {
    document.getElementById('globalSearchResults')?.classList.add('hidden');
  },

  /** Escape hatch: push the term into the Ranked Signals search. */
  searchSignalsFor(term) {
    this.closeGlobalSearch();
    const input = document.getElementById('globalSearchInput');
    if (input) input.value = '';
    this.state.signalFilters.search = term;
    this.switchView('signals');
    const sigSearch = document.getElementById('signalSearchInput');
    if (sigSearch) sigSearch.value = term;
    this.loadSignals();
  },

  setupEventListeners() {
    // View Tab Navigation
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const view = btn.dataset.view;
        this.switchView(view);
      });
    });

    // Sector Filter Buttons
    document.querySelectorAll('#sectorFilterGroup .pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#sectorFilterGroup .pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.state.filters.sector = btn.dataset.sector;
        this.loadEvents();
      });
    });

    // Source Filter Buttons
    document.querySelectorAll('#sourceFilterGroup .pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#sourceFilterGroup .pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.state.filters.source = btn.dataset.source;
        this.loadEvents();
      });
    });

    // Category Select Dropdown
    const catSelect = document.getElementById('categorySelect');
    if (catSelect) {
      catSelect.addEventListener('change', (e) => {
        this.state.filters.category = e.target.value;
        this.loadEvents();
      });
    }

    // Confidence Slider
    const confSlider = document.getElementById('confidenceSlider');
    const confVal = document.getElementById('confidenceVal');
    if (confSlider) {
      confSlider.addEventListener('input', (e) => {
        const val = e.target.value;
        confVal.textContent = `${val}%`;
        this.state.filters.confidence = parseFloat(val) / 100.0;
        this.debounce(() => this.loadEvents(), 250)();
      });
    }

    // Search Input
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        this.state.filters.search = e.target.value;
        this.debounce(() => this.loadEvents(), 300)();
      });
    }

    // Reset Filters
    const btnReset = document.getElementById('btnResetFilters');
    if (btnReset) {
      btnReset.addEventListener('click', () => {
        this.resetFilters();
      });
    }

    // Company page: technology initiative tabs.
    document.getElementById('viewCompanyPage')?.addEventListener('click', (ev) => {
      const tab = ev.target.closest('.cp-tech-tab');
      if (tab) this.switchTechTab(tab.dataset.tech);
    });

    // Opportunity Radar: region toggle and theme drill-down.
    document.getElementById('oppRegion')?.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.ins-seg-btn');
      if (!btn) return;
      this.state.oppRegion = btn.dataset.region;
      try { localStorage.setItem('msp.oppRegion', this.state.oppRegion); } catch (e) {}
      this.loadOpportunities();
    });
    document.getElementById('viewOpportunity')?.addEventListener('click', (ev) => {
      const theme = ev.target.closest('.opp-theme');
      if (theme) this.openThemeDrawer(theme.dataset.theme);
    });

    // Overview: region toggle (All / Americas / Rest of World), persisted.
    document.getElementById('insRegion')?.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.ins-seg-btn');
      if (!btn) return;
      this.state.ovRegion = btn.dataset.region;
      try { localStorage.setItem('msp.ovRegion', this.state.ovRegion); } catch (e) {}
      this.state.ovResults = null;
      this.loadOverview();
    });
    // Retract a tile. The chevron collapses its body so a long section can be
    // folded away; the choice is remembered, because a user who hides a section
    // does not want it back on every reload. Any tile on any tab can opt in by
    // adding a [data-collapse] button to its header - no per-tile wiring
    // needed. Delegated on `document` (not one view panel) so it works the
    // same way on the Opportunity tab as on the Overview tab.
    document.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-collapse]');
      if (!btn) return;
      const tile = btn.closest('.ins-tile');
      if (!tile) return;
      const hidden = tile.classList.toggle('is-retracted');
      this.scheduleFill();
      btn.setAttribute('aria-expanded', String(!hidden));
      btn.title = hidden ? 'Show this section' : 'Hide this section';
      try {
        const st = JSON.parse(localStorage.getItem('insRetracted') || '{}');
        st[btn.dataset.collapse] = hidden;
        localStorage.setItem('insRetracted', JSON.stringify(st));
      } catch (e) { /* private mode - the toggle still works for this visit */ }
    });

    // Featured Opportunities: a row (or its evidence button) opens every
    // document behind that company's position. Links inside the row keep
    // their own behaviour.
    document.getElementById('viewOpportunity')?.addEventListener('click', (ev) => {
      const row = ev.target.closest('[data-ev-co]');
      if (!row || (ev.target.closest('a') && !ev.target.closest('.opp-ev-btn'))) return;
      this.openOppEvidence(row.dataset.evCo, row.dataset.evInit);
    });

    // Re-apply whatever the user retracted last time.
    try {
      const retracted = JSON.parse(localStorage.getItem('insRetracted') || '{}');
      document.querySelectorAll('[data-collapse]').forEach(btn => {
        if (!retracted[btn.dataset.collapse]) return;
        btn.closest('.ins-tile')?.classList.add('is-retracted');
        btn.setAttribute('aria-expanded', 'false');
        btn.title = 'Show this section';
      });
    } catch (e) { /* nothing remembered is a fine starting state */ }

    // "View Evidence": the sources a tile's summary was written from.
    document.getElementById('viewOverview')?.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-evidence]');
      if (!btn) return;
      const key = btn.dataset.evidence;
      const titles = { tech: 'Technology trends', business: 'Business trends', regulatory: 'Regulatory',
                       deals: 'Budgets and deals', leadership: 'Leadership moves', news: 'News',
                       results: 'Recent results' };
      this.openDrawer(`${titles[key] || 'Evidence'} — sources`);
      document.getElementById('insResultsBody').innerHTML =
        Components.drawerEvidence((this.state.ovTiles || {})[key]);
    });

    // Every "View All" / "Read More" on the Overview, delegated.
    document.getElementById('viewOverview')?.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-viewall]');
      if (btn) this.ovViewAll(btn.dataset.viewall);
    });
    document.getElementById('insResultsClose')?.addEventListener('click', () => this.closeResultsModal());
    document.getElementById('insResultsModal')?.addEventListener('click', (ev) => {
      if (ev.target.id === 'insResultsModal') this.closeResultsModal();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') this.closeResultsModal();
    });

    // Content-family tabs (All / News / Hiring / Filings & IR). Filtering is
    // client-side over the already-fetched window, so switching is instant and
    // costs no request.
    const famTabs = document.getElementById('familyTabs');
    if (famTabs) {
      famTabs.addEventListener('click', (ev) => {
        const btn = ev.target.closest('.family-tab');
        if (!btn || btn.disabled) return;
        this.state.feedFamily = btn.dataset.family;
        try { localStorage.setItem('msp.feedFamily', this.state.feedFamily); } catch (e) {}
        this.loadEvents();
      });
    }

    // Refresh Feed
    const btnRefresh = document.getElementById('btnRefreshFeed');
    if (btnRefresh) {
      btnRefresh.addEventListener('click', () => {
        this.loadStats();
        this.loadEvents();
      });
    }

    // Company Search in Master Grid
    const compSearch = document.getElementById('companySearchInput');
    if (compSearch) {
      compSearch.addEventListener('input', (e) => {
        this.renderCompaniesGrid(e.target.value);
      });
    }

    // Company Sector Select in Master Grid
    const compSectorSelect = document.getElementById('companySectorSelect');
    if (compSectorSelect) {
      compSectorSelect.addEventListener('change', (e) => {
        this.loadCompanies(e.target.value);
      });
    }

    // Modal Triggers
    document.getElementById('btnRefreshNow')?.addEventListener('click', () => this.refreshNow());
    document.getElementById('btnCloseIngestModal')?.addEventListener('click', () => this.closeIngestModal());
    document.getElementById('btnCancelIngest')?.addEventListener('click', () => this.closeIngestModal());
    document.getElementById('btnSubmitIngest')?.addEventListener('click', () => this.submitIngestion());

    document.getElementById('btnCloseTaxonomyModal')?.addEventListener('click', () => this.closeTaxonomyModal());

    document.getElementById('btnCloseCompanyModal')?.addEventListener('click', () => this.closeCompanyModal());

    // Batch Ingest trigger
    document.getElementById('btnTriggerBatchIngest')?.addEventListener('click', () => this.submitBatchIngest());

    // ---------------- Phase 3: Ranked Signals filters
    document.querySelectorAll('#signalSectorFilterGroup .pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#signalSectorFilterGroup .pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.state.signalFilters.sector = btn.dataset.sector;
        this.loadSignals();
      });
    });
    document.querySelectorAll('#timingFilterGroup .pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#timingFilterGroup .pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.state.signalFilters.timing = btn.dataset.timing;
        this.renderSignals();
      });
    });
    document.getElementById('signalCategorySelect')?.addEventListener('change', (e) => {
      this.state.signalFilters.category = e.target.value;
      this.loadSignals();
    });
    const scoreSlider = document.getElementById('scoreSlider');
    const scoreVal = document.getElementById('scoreVal');
    scoreSlider?.addEventListener('input', (e) => {
      scoreVal.textContent = e.target.value;
      this.state.signalFilters.minScore = parseInt(e.target.value, 10);
      this.debounce(() => this.loadSignals(), 250)();
    });
    document.getElementById('signalSearchInput')?.addEventListener('input', (e) => {
      this.state.signalFilters.search = e.target.value;
      this.debounce(() => this.loadSignals(), 300)();
    });
    document.getElementById('btnResetSignalFilters')?.addEventListener('click', () => this.resetSignalFilters());
    document.getElementById('btnRecomputeSignals')?.addEventListener('click', () => this.recomputeSignals());

    // ---------------- Phase 3: Watchlists, ICP leads, alerts
    document.getElementById('btnRunIcpLeads')?.addEventListener('click', () => this.runIcpLeads());
    document.getElementById('btnNewWatchlist')?.addEventListener('click', () => this.openWatchlistModal());
    document.getElementById('btnCloseWatchlistModal')?.addEventListener('click', () => this.closeWatchlistModal());
    document.getElementById('btnCancelWatchlist')?.addEventListener('click', () => this.closeWatchlistModal());
    document.getElementById('btnSaveWatchlist')?.addEventListener('click', () => this.saveWatchlist());
    document.getElementById('wlCompanySearch')?.addEventListener('input', (e) => {
      this.debounce(() => this.searchWatchlistCompanies(e.target.value), 250)();
    });
    document.getElementById('btnReadAllAlerts')?.addEventListener('click', () => this.markAllAlertsRead());

    // ---------------- Global date range
    document.querySelectorAll('#rangePresets .pill-btn').forEach(btn => {
      btn.addEventListener('click', () => this.setDateRangePreset(btn.dataset.preset));
    });
    document.getElementById('btnApplyCustomRange')?.addEventListener('click', () => this.applyCustomRange());

    // ---------------- Phase 5: region filters, vendor graph, tenders
    document.querySelectorAll('#regionFilterGroup .pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#regionFilterGroup .pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.state.filters.region = btn.dataset.region;
        this.loadEvents();
      });
    });
    document.querySelectorAll('#signalRegionFilterGroup .pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#signalRegionFilterGroup .pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.state.signalFilters.region = btn.dataset.region;
        this.loadSignals();
      });
    });
    document.getElementById('vendorRegionSelect')?.addEventListener('change', () => this.loadVendorGraph());
    document.getElementById('btnRefreshTenders')?.addEventListener('click', () => this.refreshTenders());
    document.getElementById('tenderCountrySelect')?.addEventListener('change', () => this.loadTenders());
    document.getElementById('tenderSearchInput')?.addEventListener('input', () => {
      this.debounce(() => this.loadTenders(), 300)();
    });
    document.getElementById('btnBackfillUncovered')?.addEventListener('click', () => this.backfillUncovered());

    // ---------------- Phase 6: global search + company page
    const gs = document.getElementById('globalSearchInput');
    if (gs) {
      gs.addEventListener('input', (e) => this.debounce(() => this.runGlobalSearch(e.target.value), 250)());
      gs.addEventListener('focus', () => { if (gs.value.trim().length >= 2) this.runGlobalSearch(gs.value); });
      gs.addEventListener('blur', () => setTimeout(() => this.closeGlobalSearch(), 150));
      gs.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { gs.blur(); this.closeGlobalSearch(); }
        if (e.key === 'Enter') {
          const first = document.querySelector('#globalSearchResults .gs-item');
          if (first) first.dispatchEvent(new MouseEvent('mousedown'));
        }
      });
    }
    document.getElementById('btnCompanyBack')?.addEventListener('click', () => {
      if (history.length > 1) history.back(); else this.switchView('signals');
    });

    // ---------------- Phase 4: analytics + exports
    document.getElementById('heatmapRows')?.addEventListener('change', () => this.loadHeatmap());
    document.getElementById('heatmapCols')?.addEventListener('change', () => this.loadHeatmap());
    document.getElementById('trendBucket')?.addEventListener('change', () => this.loadTrends());
    document.getElementById('btnExportSignals')?.addEventListener('click', () => this.exportSignals());
    document.getElementById('btnExportEvents')?.addEventListener('click', () => this.exportEvents());
    document.getElementById('btnExportLeads')?.addEventListener('click', () => this.exportLeads());
    document.getElementById('btnPushCrm')?.addEventListener('click', () => this.pushLeadsToCrm());
  },

  // ==========================================================
  // Phase 4 — Analytics
  // ==========================================================

  async loadAnalytics() {
    await Promise.all([this.loadHeatmap(), this.loadTrends(), this.loadVendorGraph()]);
  },

  async loadVendorGraph() {
    const box = document.getElementById('vendorGraphContainer');
    if (!box) return;
    try {
      const params = this.withRange(new URLSearchParams({ limit: '25' }));
      const region = document.getElementById('vendorRegionSelect')?.value;
      if (region && region !== 'all') params.append('region', region);
      const res = await fetch(`${API_BASE}/graph/vendors?${params.toString()}`);
      box.innerHTML = Components.renderVendorGraph(await res.json());
    } catch (e) {
      box.innerHTML = '<p class="text-dim">Failed to build vendor graph.</p>';
    }
  },

  // ==========================================================
  // Phase 5 — Tenders
  // ==========================================================

  async loadTenders() {
    const box = document.getElementById('tendersList');
    if (!box) return;
    try {
      const params = this.withRange(new URLSearchParams({ limit: '100' }));
      const country = document.getElementById('tenderCountrySelect')?.value;
      if (country && country !== 'all') params.append('country', country);
      const search = document.getElementById('tenderSearchInput')?.value;
      if (search) params.append('search', search);

      const [listRes, statsRes] = await Promise.all([
        fetch(`${API_BASE}/tenders?${params.toString()}`),
        fetch(`${API_BASE}/tenders/stats`)
      ]);
      const data = await listRes.json();
      const stats = await statsRes.json();

      document.getElementById('tendersCount').textContent =
        `(${data.total} in ${this.rangeDescription()})`;
      document.getElementById('tenderSourceNote').innerHTML =
        `${stats.total} tenders stored · sources: ${Object.entries(stats.by_source || {}).map(([s, n]) => `${s} (${n})`).join(', ') || 'none yet'}` +
        (stats.samgov_configured ? '' : ' · <em>SAM.gov (US federal) inactive — needs SAMGOV_API_KEY</em>');

      // Populate the country filter once, from what's actually stored
      const sel = document.getElementById('tenderCountrySelect');
      if (sel && sel.options.length <= 1 && stats.by_country) {
        Object.keys(stats.by_country).forEach(cc => {
          const o = document.createElement('option');
          o.value = cc; o.textContent = cc;
          sel.appendChild(o);
        });
      }
      box.innerHTML = Components.renderTenders(data);
    } catch (e) {
      box.innerHTML = '<p class="text-dim">Failed to load tenders.</p>';
    }
  },

  async refreshTenders() {
    const btn = document.getElementById('btnRefreshTenders');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Fetching...'; }
    try {
      const res = await fetch(`${API_BASE}/tenders/refresh?limit=100`, { method: 'POST' });
      const d = await res.json();
      await this.loadTenders();
      alert(`Fetched ${d.fetched_per_source?.ted || 0} notices — ${d.created} new, ${d.skipped_existing} already stored.`);
    } catch (e) {
      alert('Tender refresh failed: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-rotate"></i> Refresh'; }
    }
  },

  async backfillUncovered() {
    try {
      const cov = await (await fetch(`${API_BASE}/ingest/coverage?sector=BFSI`)).json();
      if (!cov.uncovered) { alert('Every BFSI company already has data — nothing to backfill.'); return; }
      if (!confirm(`${cov.uncovered} of ${cov.total_companies} BFSI companies have no data yet ` +
                   `(${cov.coverage_pct}% covered).\n\nRun ingestion on just those? ` +
                   `This runs in the background and can take 20–50 minutes.`)) return;
      const res = await fetch(`${API_BASE}/ingest/run`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sector: 'BFSI', limit_docs: 2, mode: 'uncovered' })
      });
      alert((await res.json()).message);
      this.loadJobs();
      this.pollBatchJob();
    } catch (e) {
      alert('Backfill failed to start: ' + e.message);
    }
  },

  async loadHeatmap() {
    const box = document.getElementById('heatmapContainer');
    if (!box) return;
    try {
      const params = this.withRange(new URLSearchParams({
        rows: document.getElementById('heatmapRows')?.value || 'sector',
        cols: document.getElementById('heatmapCols')?.value || 'category'
      }));
      const res = await fetch(`${API_BASE}/stats/heatmap?${params.toString()}`);
      box.innerHTML = Components.renderHeatmap(await res.json());
    } catch (e) {
      box.innerHTML = '<p class="text-dim">Failed to build heatmap.</p>';
    }
  },

  /** Heatmap cell -> jump to the matching ranked signals. */
  drillIntoHeatmapCell(rowLabel, colLabel) {
    const cat = (this.state.taxonomy?.categories || []).find(c => c.name === colLabel);
    this.state.signalFilters.sector = this.SECTORS.includes(rowLabel) ? rowLabel : 'all';
    this.state.signalFilters.category = cat ? cat.id : 'all';
    this.state.signalFilters.minScore = 0;
    this.switchView('signals');
    document.querySelectorAll('#signalSectorFilterGroup .pill-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.sector === this.state.signalFilters.sector));
    const sel = document.getElementById('signalCategorySelect');
    if (sel) sel.value = this.state.signalFilters.category;
    const slider = document.getElementById('scoreSlider'); if (slider) slider.value = 0;
    const val = document.getElementById('scoreVal'); if (val) val.textContent = '0';
    this.loadSignals();
  },

  async loadTrends() {
    const canvas = document.getElementById('trendChart');
    if (!canvas) return;
    try {
      const params = this.withRange(new URLSearchParams({
        bucket: document.getElementById('trendBucket')?.value || 'auto'
      }));
      const res = await fetch(`${API_BASE}/stats/trends?${params.toString()}`);
      const data = await res.json();

      const palette = ['#06b6d4', '#3b82f6', '#8b5cf6', '#10b981', '#f59e0b', '#ec4899', '#f43f5e'];
      if (this.state.trendChart) this.state.trendChart.destroy();
      this.state.trendChart = new Chart(canvas, {
        type: 'bar',
        data: {
          labels: data.labels,
          datasets: data.series.map((s, i) => ({
            label: s.source_type,
            data: s.counts,
            backgroundColor: palette[i % palette.length],
            borderWidth: 0
          }))
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: { stacked: true, ticks: { color: '#64748b', maxRotation: 45, minRotation: 0 }, grid: { display: false } },
            y: { stacked: true, ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' } }
          },
          plugins: {
            legend: { labels: { color: '#94a3b8', boxWidth: 12, font: { size: 11 } } },
            tooltip: { mode: 'index', intersect: false }
          }
        }
      });
    } catch (e) {
      console.error('Trend chart failed:', e);
    }
  },

  async loadSourceHealth() {
    const box = document.getElementById('sourceHealthPanel');
    if (!box) return;
    try {
      const res = await fetch(`${API_BASE}/admin/source-health?${this.rangeParams().toString()}`);
      box.innerHTML = Components.renderSourceHealth(await res.json());
    } catch (e) {
      box.innerHTML = '<p class="text-dim">Source health unavailable.</p>';
    }
  },

  // ==========================================================
  // Phase 4 — Exports
  // ==========================================================

  /** Navigating to the URL lets the browser handle the file download. */
  _download(path, params) {
    window.location.href = `${API_BASE}${path}?${params.toString()}`;
  },

  exportSignals() {
    const f = this.state.signalFilters;
    const p = this.withRange(new URLSearchParams({ min_score: f.minScore }));
    if (f.sector !== 'all') p.append('sector', f.sector);
    if (f.category !== 'all') p.append('category_id', f.category);
    this._download('/exports/signals.csv', p);
  },

  exportEvents() {
    const f = this.state.filters;
    const p = this.withRange(new URLSearchParams());
    if (f.sector !== 'all') p.append('sector', f.sector);
    if (f.category !== 'all') p.append('category_id', f.category);
    if (f.source !== 'all') p.append('source_type', f.source);
    this._download('/exports/events.csv', p);
  },

  _icpParams() {
    const p = this.withRange(new URLSearchParams({
      min_score: document.getElementById('icpMinScore')?.value || '60'
    }));
    const sectors = this._checkedValues('icpSectorOptions');
    const inits = this._checkedValues('icpInitiativeOptions');
    if (sectors.length) p.append('sectors', sectors.join(','));
    if (inits.length) p.append('initiative_ids', inits.join(','));
    return p;
  },

  exportLeads() {
    this._download('/exports/leads.csv', this._icpParams());
  },

  async pushLeadsToCrm() {
    const btn = document.getElementById('btnPushCrm');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Pushing...'; }
    try {
      const res = await fetch(`${API_BASE}/exports/crm-webhook?${this._icpParams().toString()}`, { method: 'POST' });
      const data = await res.json();
      alert(data.status === 'delivered'
        ? `Delivered ${data.lead_count} leads to your CRM webhook.`
        : `CRM push ${data.status}: ${data.message}`);
    } catch (e) {
      alert('CRM push failed: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Push to CRM'; }
    }
  },

  async refreshCrmStatus() {
    try {
      const { configured } = await (await fetch(`${API_BASE}/exports/crm-status`)).json();
      document.getElementById('btnPushCrm')?.classList.toggle('hidden', !configured);
    } catch (e) { /* leave hidden */ }
  },

  // ==========================================================
  // Global date range — every view reads from this one state
  // ==========================================================

  restoreDateRange() {
    try {
      const saved = JSON.parse(localStorage.getItem('msp.dateRange') || 'null');
      if (saved && saved.preset) this.state.dateRange = saved;
    } catch (e) { /* corrupt or unavailable storage — keep the default */ }
    this.syncDateRangeUI();
  },

  persistDateRange() {
    try {
      localStorage.setItem('msp.dateRange', JSON.stringify(this.state.dateRange));
    } catch (e) { /* private mode / storage disabled — filtering still works */ }
  },

  /** ISO date (YYYY-MM-DD) N days ago. */
  _isoDaysAgo(days) {
    const d = new Date();
    d.setDate(d.getDate() - days);
    return d.toISOString().slice(0, 10);
  },

  /** The `start`/`end` params every windowed endpoint takes. */
  rangeParams() {
    const r = this.state.dateRange;
    const p = new URLSearchParams();
    if (r.preset === 'all') {
      p.append('start', 'all');
    } else if (r.preset === 'custom') {
      if (r.start) p.append('start', r.start);
      if (r.end) p.append('end', r.end);
    } else {
      p.append('start', this._isoDaysAgo(this.RANGE_PRESETS[r.preset] ?? 30));
    }
    return p;
  },

  /** Merge the range params into a view's own params. */
  withRange(params) {
    const merged = params || new URLSearchParams();
    this.rangeParams().forEach((v, k) => merged.append(k, v));
    return merged;
  },

  rangeDescription() {
    const r = this.state.dateRange;
    if (r.preset === 'custom') {
      if (r.start && r.end) return `${r.start} to ${r.end}`;
      if (r.start) return `since ${r.start}`;
      if (r.end) return `up to ${r.end}`;
      return 'custom range';
    }
    return this.RANGE_LABELS[r.preset] || 'last 30 days';
  },

  syncDateRangeUI() {
    const r = this.state.dateRange;
    document.querySelectorAll('#rangePresets .pill-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.preset === r.preset));
    const custom = document.getElementById('customRangeInputs');
    if (custom) custom.classList.toggle('hidden', r.preset !== 'custom');
    if (r.preset === 'custom') {
      const s = document.getElementById('rangeStart'); if (s && r.start) s.value = r.start;
      const e = document.getElementById('rangeEnd'); if (e && r.end) e.value = r.end;
    }
    const summary = document.getElementById('rangeSummary');
    if (summary) summary.innerHTML = `<i class="fa-solid fa-filter"></i> Showing ${escapeHtml(this.rangeDescription())}`;
  },

  setDateRangePreset(preset) {
    if (preset === 'custom') {
      // Seed the inputs with the current window so the user edits from context.
      this.state.dateRange = {
        preset: 'custom',
        start: this.state.dateRange.start || this._isoDaysAgo(30),
        end: this.state.dateRange.end || new Date().toISOString().slice(0, 10)
      };
      this.syncDateRangeUI();
      return; // wait for Apply — don't refetch on every keystroke
    }
    this.state.dateRange = { preset, start: null, end: null };
    this.persistDateRange();
    this.syncDateRangeUI();
    this.refreshAll();
  },

  applyCustomRange() {
    const start = document.getElementById('rangeStart')?.value || null;
    const end = document.getElementById('rangeEnd')?.value || null;
    if (start && end && start > end) {
      alert('Start date must be on or before the end date.');
      return;
    }
    this.state.dateRange = { preset: 'custom', start, end };
    this.persistDateRange();
    this.syncDateRangeUI();
    this.refreshAll();
  },

  /**
   * Reload the headline stats plus whichever view is on screen, under the
   * radar: a new date range is a fresh set of queries for every tile.
   */
  async refreshAll() {
    const r = this.state.dateRange || {};
    const label = r.preset === 'custom'
      ? (r.start && r.end ? `Loading ${r.start} to ${r.end}…` : 'Loading your date range…')
      : `Loading the ${this.RANGE_LABELS[r.preset] || 'selected range'}…`;
    const radar = this.showRadar(label);
    try {
      this._jsonCache.clear();
      await this.loadStats();
      const view = this.state.activeView;
      let loading;
      if (view === 'overview') { this.state.ovResults = null; loading = this.loadOverview(); }
      else if (view === 'opportunity') loading = this.loadOpportunities();
      // The company page is a view too: without this it kept showing the window
      // it was first opened with, however the date range changed.
      else if (view === 'company' && this.state.currentCompanyId) loading = this.showCompanyPage(this.state.currentCompanyId);
      else if (view === 'feed') loading = this.loadEvents();
      else if (view === 'signals') loading = this.loadSignals();
      else if (view === 'companies') loading = this.loadGalaxy();
      else if (view === 'watchlists') loading = Promise.all([this.loadWatchlists(), this.loadAlerts()]);
      else if (view === 'jobs') loading = this.loadJobs();
      else if (view === 'analytics') loading = this.loadAnalytics();
      else if (view === 'tenders') loading = this.loadTenders();
      await Promise.race([Promise.resolve(loading).catch(() => {}), new Promise(res => setTimeout(res, 20000))]);
    } finally {
      this.hideRadar(radar);
    }
  },

  /**
   * initiative_id -> {name, it_offering}, fetched once and cached.
   * Needed because the LLM enrichment pass invents its own initiative_name;
   * tags must come from the taxonomy or they are unstable between events.
   */
  async taxonomyMap() {
    if (this._taxMap) return this._taxMap;
    const map = {};
    try {
      const t = await (await fetch(`${API_BASE}/taxonomy`)).json();
      for (const cat of (t.categories || [])) {
        for (const i of (cat.initiatives || [])) {
          map[i.id] = { name: i.name, it_offering: i.it_offering, category: cat.name };
        }
      }
    } catch (e) {
      console.error('taxonomy failed', e);   // tags fall back to stored names
    }
    this._taxMap = map;
    return map;
  },

  /**
   * Overview (BFSI Insights). Each tile loads and fails independently, and every
   * request carries the global date range plus the Overview's region toggle.
   */
  async loadOverview() {
    const region = this.state.ovRegion || 'all';
    // A newer load (another region click) supersedes this one: its responses
    // must not overwrite the newer region's tiles if they arrive late.
    const token = this._ovToken = (this._ovToken || 0) + 1;
    const done = this.markRefreshing('viewOverview');
    const paths = new Set();
    const set = (id, html) => {
      if (token !== this._ovToken) return;
      const el = document.querySelector(`#${id} .ins-body`);
      if (el) { el.innerHTML = html; Components.compactLists(el); Components.animateIn(el); }
      this.scheduleFill();
    };
    const fail = (id, what) => set(id, `<p class="ins-empty">Could not load ${what}.</p>`);
    const range = this.rangeParams ? this.rangeParams() : new URLSearchParams();
    const withRegion = (reg) => {
      const p = new URLSearchParams(range);
      if (reg && reg !== 'all') p.set('region', reg);
      return p;
    };
    const get = (path, reg = region) => {
      if (reg === region) paths.add(path);
      const qs = withRegion(reg).toString();
      return this.cachedJson(`${API_BASE}${path}${path.includes('?') ? '&' : '?'}${qs}`);
    };

    document.querySelectorAll('#insRegion .ins-seg-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.region === region));

    // Trend tiles + subtitle. Regional comparison only makes sense on the global view.
    const insightsReady = Promise.all([
      get('/insights/overview'),
      region === 'all' ? get('/insights/overview', 'Americas') : Promise.resolve(null),
      region === 'all' ? get('/insights/overview', 'Rest of World') : Promise.resolve(null),
    ]).then(([ins, am, row]) => {
      this.state.ovTiles = ins.tiles || {};
      set('insTech', Components.insTech(ins, am && row ? { am, row } : null));
      set('insBusiness', Components.insBusiness(ins));
      const sub = document.getElementById('insSub');
      if (sub) {
        const where = region === 'all' ? 'Americas & Rest of World' : region;
        sub.textContent = `${where} · ${this.rangeDescription ? this.rangeDescription() : ''} · `
          + `${ins.active_companies} of ${ins.tracked_companies} companies active · `
          + `${ins.hot_signals} ${ins.hot_signals === 1 ? 'account' : 'accounts'} scoring 80+`
          + (ins.latest_data_at ? ` · data up to ${Components.insDate(ins.latest_data_at)}` : '');
        // Empty because nothing has been fetched recently, not because nothing happened.
        sub.classList.toggle('ins-sub--stale', ins.active_companies === 0 && !!ins.latest_data_at);
      }
    }).catch(e => { console.error('insights failed', e); fail('insTech', 'tech trends'); fail('insBusiness', 'business trends'); });

    const resultsReady = get('/results/recent?limit=200')
      .then(async d => { if (token !== this._ovToken) return; this.state.ovResults = d;
        await insightsReady.catch(() => {});
        set('insResults', (Components.insTileInsight((this.state.ovTiles || {}).results, 'results') || '') + Components.insResults(d)); })
      .catch(e => { console.error('results failed', e); fail('insResults', 'results'); });

    // The four list tiles render the same server query their summary describes
    // (tiles[key].evidence: every matching company counted, newest first). They
    // used to filter the newest 800 events client-side, which saw only a slice
    // of the window - Leadership listed 3 of the 18 companies with a move.
    try {
      await insightsReady;
      if (token !== this._ovToken) return;
      const tiles = this.state.ovTiles || {};
      const asEvents = key => ((tiles[key] || {}).evidence || []).map(x => ({
        company_id: x.company_id, company_name: x.company,
        doc_headline: x.publisher ? `[${x.publisher}] ${x.headline}` : x.headline, title: x.headline,
        quote_text: x.quote || '', source_url: x.url, doc_published_at: x.date, occurred_at: x.date,
        initiative_id: x.initiative_id, spend_amount: x.spend_amount, source_type: x.source_type,
        raw_doc_id: x.doc_id, doc_image: x.image,
        _initiatives: [{ initiative_id: x.initiative_id }], _family: 'news',
      }));
      const withInsight = (key, html) => (Components.insTileInsight(tiles[key], key) || '') + html;
      set('insRegulatory', withInsight('regulatory', Components.insRegulatory(asEvents('regulatory'))));
      set('insDeals', withInsight('deals', Components.insDeals(asEvents('deals'))));
      set('insLeadership', withInsight('leadership', Components.insLeadership(asEvents('leadership'))));
      set('insNews', withInsight('news', Components.insNews(asEvents('news'))));
    } catch (e) {
      console.error('overview lists failed', e);
      ['insRegulatory', 'insDeals', 'insLeadership', 'insNews'].forEach(id => fail(id, 'this tile'));
    }
    await Promise.allSettled([insightsReady, resultsReady]);
    if (token === this._ovToken) {
      done();
      this.prefetchRegions([...paths], region);
    }
  },

  /**
   * "View All" opens a drawer rather than another tab. With only Overview and
   * Companies in the nav, every path has to end somewhere those two pages can
   * reach - so trend drawers list the companies behind a theme, which lead
   * straight to a company page.
   */
  async ovViewAll(kind) {
    const region = this.state.ovRegion || 'all';
    const params = new URLSearchParams(this.rangeParams ? this.rangeParams() : '');
    if (region !== 'all') params.set('region', region);

    const titles = {
      tech: 'Companies pursuing technology initiatives',
      business: 'Companies making strategic moves',
      results: 'Recent results',
      regulatory: 'Regulatory & compliance',
      deals: 'Budgets, partnerships & RFPs',
      leadership: 'Leadership moves',
      news: 'All news',
    };
    this.openDrawer(titles[kind] || 'Details');
    const body = document.getElementById('insResultsBody');
    body.innerHTML = '<p class="ins-empty">Loading…</p>';

    try {
      if (kind === 'results') {
        const data = this.state.ovResults
          || await (await fetch(`${API_BASE}/results/recent?limit=200&${params}`)).json();
        body.innerHTML = Components.insResultsList(data);
        return;
      }
      if (kind === 'tech' || kind === 'business') {
        const cat = kind === 'tech' ? 'tech_initiatives' : 'strategic_priorities';
        const p = new URLSearchParams(params);
        p.set('category_id', cat);
        p.set('min_score', '0');
        p.set('limit', '100');
        const d = await (await fetch(`${API_BASE}/signals?${p}`)).json();
        body.innerHTML = Components.drawerCompanies(d.signals || d || [], await this.taxonomyMap());
        return;
      }
      // Event-backed drawers: one entry per source document.
      const catByKind = {
        regulatory: 'regulatory_compliance',
        deals: 'spending_signals',
        leadership: 'organizational_change',
      };
      const evts = [];
      for (let page = 0; page < 4; page++) {
        const p = new URLSearchParams(params);
        if (catByKind[kind]) p.set('category_id', catByKind[kind]);
        p.set('limit', '200');
        p.set('offset', String(page * 200));
        const batch = await (await fetch(`${API_BASE}/events?${p}`)).json();
        if (!Array.isArray(batch) || !batch.length) break;
        evts.push(...batch);
        if (batch.length < 200) break;
      }
      let rows = evts;
      if (kind === 'leadership') rows = evts.filter(e => Components.insIsLeadershipMove(e));
      if (kind === 'news') rows = groupEventsByDocument(evts).filter(c => c._family === 'news');
      body.innerHTML = Components.drawerStories(rows, kind === 'news');
    } catch (e) {
      console.error('drawer failed', e);
      body.innerHTML = '<p class="ins-empty">Could not load this list.</p>';
    }
  },

  async openOppEvidence(companyId, initiativeId) {
    this.openDrawer('Evidence behind this opportunity');
    const body = document.getElementById('insResultsBody');
    body.innerHTML = '<p class="ins-empty">Loading the documents…</p>';
    try {
      const params = this.withRange(new URLSearchParams({ company_id: companyId, initiative_id: initiativeId }));
      const res = await fetch(`${API_BASE}/opportunities/evidence?${params}`);
      if (!res.ok) throw new Error(`evidence -> ${res.status}`);
      const d = await res.json();
      const heading = document.getElementById('insDrawerTitle');
      if (heading) heading.textContent = `${d.company_name} - ${d.theme}`;
      body.innerHTML = Components.drawerOppEvidence(d);
      Components.animateIn(body);
    } catch (e) {
      console.error('evidence failed', e);
      body.innerHTML = '<p class="ins-empty">Could not load the documents.</p>';
    }
  },

  // ------------------------------------------------------ BFSI solar system
  async loadGalaxy() {
    const tile = document.getElementById('coGalaxy');
    if (!tile) return;
    try {
      const res = await fetch(`${API_BASE}/galaxy?${this.rangeParams()}`);
      if (!res.ok) throw new Error(`galaxy -> ${res.status}`);
      this.state.gxData = await res.json();
      this.state.gx = this.state.gx || { v: null, p: null };
      this.renderGalaxy();
      this.bindGalaxy();
    } catch (e) {
      console.error('galaxy failed', e);
      tile.querySelector('.ins-body').innerHTML = '<p class="ins-empty">Could not load the companies.</p>';
    }
  },

  renderGalaxy() {
    const d = this.state.gxData, st = this.state.gx;
    if (!d) return;
    const gxBody = document.querySelector('#coGalaxy .ins-body');
    gxBody.innerHTML = Components.galaxyView(d, st);
    Components.animateIn(gxBody);
    document.getElementById('gxCrumbs').innerHTML = Components.galaxyCrumbs(d, st);
  },

  bindGalaxy() {
    if (this._gxBound) return;
    this._gxBound = true;
    const tile = document.getElementById('coGalaxy');
    tile.addEventListener('click', (ev) => {
      const crumb = ev.target.closest('[data-gx-crumb]');
      if (crumb) {
        ev.preventDefault();
        this.state.gx = crumb.dataset.gxCrumb === 'root' ? { v: null, p: null } : { v: this.state.gx.v, p: null };
        return this.renderGalaxy();
      }
      const el = ev.target.closest('[data-gx]');
      if (!el) return;
      ev.preventDefault();
      const kind = el.dataset.gx;
      if (kind === 'sat') return this.goToCompany(el.dataset.id);
      if (kind === 'star') this.state.gx = { v: +el.dataset.v, p: null };
      if (kind === 'planet') this.state.gx = { v: +el.dataset.v, p: +el.dataset.p };
      this.renderGalaxy();
    });
    // One tooltip, following the pointer over anything with data-tip.
    tile.addEventListener('mousemove', (ev) => {
      const tip = tile.querySelector('.gx-tip');
      const stage = tile.querySelector('.gx-stage');
      if (!tip || !stage) return;
      const el = ev.target.closest('.gx-svg [data-tip]');
      if (!el) { tip.hidden = true; return; }
      const box = stage.getBoundingClientRect();
      tip.textContent = el.getAttribute('data-tip');
      tip.hidden = false;
      tip.style.left = `${Math.min(ev.clientX - box.left + 14, box.width - 260)}px`;
      tip.style.top = `${ev.clientY - box.top + 14}px`;
    });
    tile.addEventListener('mouseleave', () => { const t = tile.querySelector('.gx-tip'); if (t) t.hidden = true; });
  },

  /**
   * Fetch JSON through a short-lived cache. Switching region re-requested the
   * same data every time; now a region already seen - or prefetched in the
   * background - renders at once. Concurrent requests for one URL share a
   * single fetch. A few minutes' TTL lets summaries written in the background
   * appear on a later visit; a refresh or date-range change clears it.
   */
  _jsonCache: new Map(),
  cachedJson(url, ttl = 180000) {
    const hit = this._jsonCache.get(url);
    if (hit && Date.now() - hit.t < ttl) return hit.p;
    const p = fetch(url).then(r => {
      if (!r.ok) throw new Error(`${url} -> ${r.status}`);
      return r.json();
    });
    this._jsonCache.set(url, { t: Date.now(), p });
    p.catch(() => this._jsonCache.delete(url));
    return p;
  },

  /** Dim the tiles (with a moving bar) only if new data takes longer than a blink. */
  markRefreshing(viewId) {
    const el = document.getElementById(viewId);
    const timer = setTimeout(() => el?.classList.add('is-refreshing'), 90);
    return () => { clearTimeout(timer); el?.classList.remove('is-refreshing'); };
  },

  /** After a view settles, warm the cache for the other regions so switching is instant. */
  async prefetchRegions(paths, current) {
    const range = this.rangeParams ? this.rangeParams() : new URLSearchParams();
    for (const reg of ['all', 'Americas', 'Rest of World']) {
      if (reg === current) continue;
      for (const path of paths) {
        const p = new URLSearchParams(range);
        if (reg !== 'all') p.set('region', reg);
        const url = `${API_BASE}${path}${path.includes('?') ? '&' : '?'}${p}`;
        try { await this.cachedJson(url); } catch (e) { /* prefetch is best effort */ }
      }
    }
  },

  /** Re-fit every fillable list once the tiles have settled (debounced). */
  scheduleFill() {
    clearTimeout(this._fillTimer);
    // A plain timer, not requestAnimationFrame: rAF is paused in background
    // tabs, and a list left unfitted there would stay at its minimum.
    this._fillTimer = setTimeout(() => Components.fillLists(document), 150);
    if (!this._fillBound) {
      this._fillBound = true;
      window.addEventListener('resize', () => this.scheduleFill());
      // A tile can also change height with no window resize: a neighbour in
      // its row loads later, an image or web font arrives. Re-fit then too, or
      // the list keeps the length it was measured at. (Fitting never changes a
      // tile's height, so this cannot loop.)
      if (window.ResizeObserver) {
        this._tileHeights = new WeakMap();
        this._tileObserver = new ResizeObserver(entries => {
          const moved = entries.some(e => {
            const h = Math.round(e.contentRect.height);
            const was = this._tileHeights.get(e.target);
            this._tileHeights.set(e.target, h);
            return was !== undefined && Math.abs(was - h) > 2;
          });
          if (moved) this.scheduleFill();
        });
      }
    }
    if (this._tileObserver) document.querySelectorAll('.ins-tile').forEach(t => this._tileObserver.observe(t));
  },

  openDrawer(title) {
    const modal = document.getElementById('insResultsModal');
    const heading = document.getElementById('insDrawerTitle');
    if (heading) heading.textContent = title;
    modal?.classList.add('active');
    document.getElementById('insResultsClose')?.focus();
  },

  async openResultsModal() { return this.ovViewAll('results'); },

  closeResultsModal() {
    document.getElementById('insResultsModal')?.classList.remove('active');
  },

  /** Opportunity Radar. One request; tiles render from the same payload. */
  async loadOpportunities(opts = {}) {
    const region = this.state.oppRegion || 'all';
    const token = this._oppToken = (this._oppToken || 0) + 1;
    const done = this.markRefreshing('viewOpportunity');
    const set = (id, html) => {
      const el = document.querySelector(`#${id} .ins-body`);
      if (el) { el.innerHTML = html; Components.compactLists(el); Components.animateIn(el); }
      this.scheduleFill();
    };
    document.querySelectorAll('#oppRegion .ins-seg-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.region === region));

    const params = new URLSearchParams(this.rangeParams ? this.rangeParams() : '');
    if (region !== 'all') params.set('region', region);

    try {
      const url = `${API_BASE}/opportunities?${params}`;
      if (opts.fresh) this._jsonCache.delete(url);
      const d = await this.cachedJson(url);
      if (token !== this._oppToken) return;       // a newer region click won
      this.state.oppData = d;
      done();
      setTimeout(() => this.prefetchRegions(['/opportunities'], region), 400);

      document.getElementById('oppKpis').innerHTML = Components.oppKpis(d);
      Components.animateIn(document.getElementById('oppKpis'));
      set('oppFeatured', Components.oppFeatured(d.featured, d.featured_summary));
      set('oppRising', Components.oppRising(d.rising));
      set('oppCompetitive', Components.oppCompetitive(d.competitive));
      set('oppPeople', Components.oppPeople(d.people, d.recommendations, d.people_pending));
      set('oppRecs', Components.oppTargets(d.recommendations));
      const fit = () => Components.fitRows(document.querySelector('#oppPeople .opp-people-list'), '.opp-person-row', 5);
      fit();
      if (!this._peopleFitBound) {
        this._peopleFitBound = true;
        let t;
        window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(fit, 150); });
      }
      // Leaders for companies never searched are being looked up in the
      // background right now; reload once or twice to pick them up.
      if (d.people_pending && (this.state.oppPeopleRetries || 0) < 2) {
        this.state.oppPeopleRetries = (this.state.oppPeopleRetries || 0) + 1;
        clearTimeout(this._peopleRetry);
        this._peopleRetry = setTimeout(() => this.loadOpportunities({ fresh: true }), 30000);
      } else if (!d.people_pending) {
        this.state.oppPeopleRetries = 0;
      }

      const sub = document.getElementById('oppSub');
      if (sub) {
        const where = region === 'all' ? 'Americas & Rest of World' : region;
        sub.textContent = `${where} · ${this.rangeDescription ? this.rangeDescription() : ''} · `
          + `${d.totals.opportunities} opportunities across ${d.totals.companies} companies`;
      }
    } catch (e) {
      console.error('opportunities failed', e);
      done();
      document.getElementById('oppKpis').innerHTML = '<p class="ins-empty">Could not load the radar.</p>';
      ['oppFeatured', 'oppRising', 'oppCompetitive', 'oppPeople', 'oppRecs']
        .forEach(id => set(id, '<p class="ins-empty">Could not load this tile.</p>'));
    }
  },

  /** A theme tile opens the companies pursuing it. */
  openThemeDrawer(initiativeId) {
    const d = this.state.oppData || {};
    const theme = (d.themes || []).find(t => t.initiative_id === initiativeId);
    this.openDrawer(theme ? `${theme.name} — companies pursuing it` : 'Companies');
    const body = document.getElementById('insResultsBody');
    body.innerHTML = '<p class="ins-empty">Loading…</p>';
    const params = new URLSearchParams(this.rangeParams ? this.rangeParams() : '');
    if ((this.state.oppRegion || 'all') !== 'all') params.set('region', this.state.oppRegion);
    params.set('initiative_id', initiativeId);
    params.set('min_score', '0');
    params.set('limit', '100');
    Promise.all([
      fetch(`${API_BASE}/signals?${params}`).then(r => r.json()),
      this.taxonomyMap(),
    ]).then(([res, tax]) => {
      body.innerHTML = Components.drawerCompanies(res.signals || res || [], tax);
    }).catch(e => {
      console.error('theme drawer failed', e);
      body.innerHTML = '<p class="ins-empty">Could not load this theme.</p>';
    });
  },

  switchView(viewName, opts = {}) {
    this.state.activeView = viewName;
    // Keep the URL in step so tabs are linkable too, without re-entering the
    // router (which would loop).
    if (!opts.fromRouter) {
      const target = `#/${viewName}`;
      if (location.hash !== target) { this._suppressRouter = true; location.hash = target; }
    }
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.tab-btn[data-view="${viewName}"]`)?.classList.add('active');

    document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
    if (viewName === 'opportunity') {
      document.getElementById('viewOpportunity')?.classList.add('active');
      this._viewLoading = this.loadOpportunities();
    }
    if (viewName === 'overview') {
      document.getElementById('viewOverview')?.classList.add('active');
      this._viewLoading = this.loadOverview();
    }
    if (viewName === 'feed') document.getElementById('viewSignalFeed')?.classList.add('active');
    if (viewName === 'companies') {
      document.getElementById('viewCompanies')?.classList.add('active');
      this._viewLoading = this.loadGalaxy();
    }
    if (viewName === 'jobs') {
      document.getElementById('viewJobs')?.classList.add('active');
      this.loadJobs();
    }
    if (viewName === 'signals') {
      document.getElementById('viewSignals')?.classList.add('active');
      this.loadSignals();
    }
    if (viewName === 'watchlists') {
      document.getElementById('viewWatchlists')?.classList.add('active');
      this.loadWatchlists();
      this.loadAlerts();
      this.refreshCrmStatus();
    }
    if (viewName === 'analytics') {
      document.getElementById('viewAnalytics')?.classList.add('active');
      this.loadAnalytics();
    }
    if (viewName === 'tenders') {
      document.getElementById('viewTenders')?.classList.add('active');
      this.loadTenders();
    }
  },

  async loadTaxonomy() {
    try {
      const res = await fetch(`${API_BASE}/taxonomy`);
      const data = await res.json();
      this.state.taxonomy = data;

      // Populate Category Filter Dropdowns (events feed + ranked signals)
      ['categorySelect', 'signalCategorySelect'].forEach(id => {
        const catSelect = document.getElementById(id);
        if (catSelect && data.categories) {
          catSelect.innerHTML = '<option value="all">All Categories</option>';
          data.categories.forEach(cat => {
            const opt = document.createElement('option');
            opt.value = cat.id;
            opt.textContent = `${cat.name} (${cat.initiatives.length})`;
            catSelect.appendChild(opt);
          });
        }
      });
      this.populateIcpOptions();
    } catch (e) {
      console.error('Error loading taxonomy:', e);
    }
  },

  /** The header stat cards were removed; each page carries its own figures now. */
  async loadStats() {},

  /**
   * The deployed copy runs in public mode: read-only, refreshed by the daily
   * GitHub Actions run. Hide the controls a visitor cannot use - the server
   * refuses those requests regardless.
   */
  async loadAppConfig() {
    try {
      const cfg = await (await fetch(`${API_BASE}/app-config`)).json();
      this.state.publicMode = !!cfg.public_mode;
      document.body.classList.toggle('public-mode', this.state.publicMode);
    } catch (e) { /* older server: behave as the local copy */ }
  },

  /** Header freshness: last successful refresh and the next scheduled one. */
  async loadFreshness() {
    const dot = document.getElementById('freshnessDot');
    const text = document.getElementById('freshnessText');
    const box = document.getElementById('freshness');
    const btn = document.getElementById('btnRefreshNow');
    if (!text) return null;
    try {
      const s = await (await fetch(`${API_BASE}/refresh/status`)).json();
      const fmt = iso => iso ? new Date(iso).toLocaleString('en-GB',
        { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : null;
      const last = s.last_run || {};
      let label, cls;
      if (s.running) {
        label = 'Refreshing data…'; cls = 'busy';
      } else if (last.ok) {
        label = `Updated ${fmt(last.finished_at)}`; cls = 'ok';
      } else if (last.state === 'failed') {
        label = 'Last refresh failed'; cls = 'bad';
      } else {
        label = 'Not refreshed yet'; cls = 'stale';
      }
      if (text) text.textContent = label;
      if (dot) dot.className = `freshness-dot ${cls}`;
      if (s.public_mode) document.body.classList.add('public-mode');
      if (box) box.title = [s.next_run ? `Next refresh ${fmt(s.next_run)} (${s.schedule})` : '',
                            s.public_mode ? 'Refreshed automatically every morning.' : '',
                            last.error ? `Error: ${last.error}` : ''].filter(Boolean).join('\n');
      if (btn) btn.disabled = !!s.running;
      return s;
    } catch (e) {
      if (text) text.textContent = 'Status unavailable';
      return null;
    }
  },

  async refreshNow() {
    const btn = document.getElementById('btnRefreshNow');
    if (btn) btn.disabled = true;
    try { await fetch(`${API_BASE}/refresh`, { method: 'POST' }); } catch (e) {}
    await this.loadFreshness();
    // Poll until the run finishes, then redraw whatever is on screen.
    clearInterval(this._freshPoll);
    this._freshPoll = setInterval(async () => {
      const s = await this.loadFreshness();
      if (s && !s.running) {
        clearInterval(this._freshPoll);
        this.refreshAll();
      }
    }, 15000);
  },

  /** Back from a company page: to wherever the user came from. */
  goBack() {
    // Return to the page the company was opened from. A cold deep link has no
    // such page, so it falls back to the Companies list rather than leaving the app.
    this.switchView(this.state.prevView || 'companies');
  },

  renderCategoryChart(catData) {
    const ctx = document.getElementById('categoryPieChart');
    if (!ctx) return;

    const labels = Object.keys(catData || {});
    const counts = Object.values(catData || {});

    if (labels.length === 0) {
      labels.push('Awaiting Ingestion');
      counts.push(1);
    }

    if (this.state.categoryChart) {
      this.state.categoryChart.destroy();
    }

    this.state.categoryChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: labels,
        datasets: [{
          data: counts,
          backgroundColor: [
            '#06b6d4',
            '#3b82f6',
            '#8b5cf6',
            '#10b981',
            '#f59e0b',
            '#ec4899'
          ],
          borderWidth: 0
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false }
        },
        cutout: '72%'
      }
    });
  },

  async loadCompanies(sector = 'BFSI') {
    try {
      const params = this.withRange(new URLSearchParams({ limit: '100' }));
      if (sector && sector !== 'all') params.append('sector', sector);

      const res = await fetch(`${API_BASE}/companies?${params.toString()}`);
      const companies = await res.json();
      this.state.companies = companies;

      this.renderCompaniesGrid();
      this.populateIngestModalSelect(companies);
    } catch (e) {
      console.error('Error loading companies:', e);
    }
  },

  renderCompaniesGrid(searchFilter = '') {
    const grid = document.getElementById('companiesGrid');
    if (!grid) return;

    let list = this.state.companies;
    if (searchFilter) {
      const q = searchFilter.toLowerCase();
      list = list.filter(c =>
        c.name.toLowerCase().includes(q) ||
        (c.ticker && c.ticker.toLowerCase().includes(q)) ||
        (c.industry && c.industry.toLowerCase().includes(q))
      );
    }

    if (list.length === 0) {
      grid.innerHTML = '<p class="text-dim" style="grid-column: 1/-1; text-align:center; padding: 40px;">No companies found matching criteria.</p>';
      return;
    }

    grid.innerHTML = list.map(c => Components.renderCompanyCard(c)).join('');
  },

  populateIngestModalSelect(companies) {
    const select = document.getElementById('ingestCompanySelect');
    if (!select) return;

    select.innerHTML = '';
    companies.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      const tickerPart = c.ticker ? ` (${c.ticker})` : ' (News Only)';
      opt.textContent = `${c.name}${tickerPart} — ${c.sector}`;
      select.appendChild(opt);
    });
  },

  async loadEvents() {
    const listContainer = document.getElementById('signalsList');
    if (!listContainer) return;

    listContainer.innerHTML = `
      <div class="loading-spinner-state">
        <div class="spinner"></div>
        <p>Scanning signals database...</p>
      </div>
    `;

    try {
      const f = this.state.filters;
      let params = new URLSearchParams();
      if (f.sector !== 'all') params.append('sector', f.sector);
      if (f.category !== 'all') params.append('category_id', f.category);
      if (f.source !== 'all') params.append('source_type', f.source);
      if (f.confidence > 0) params.append('min_confidence', f.confidence);
      if (f.search) params.append('search', f.search);
      if (f.region !== 'all') params.append('region', f.region);
      params.append('limit', '50');
      this.withRange(params);

      const res = await fetch(`${API_BASE}/events?${params.toString()}`);
      const events = await res.json();
      this.state.events = events;

      // One card per story, then filtered to the selected content family so a
      // job posting never appears inside the news stream.
      const allCards = groupEventsByDocument(events);
      const fam = this.state.feedFamily || 'all';
      const grouped = fam === 'all' ? allCards : allCards.filter(c => c._family === fam);

      const counts = allCards.reduce((a, c) => { a[c._family] = (a[c._family] || 0) + 1; return a; }, {});
      document.querySelectorAll('#familyTabs .family-tab').forEach(btn => {
        const f = btn.dataset.family;
        const n = f === 'all' ? allCards.length : (counts[f] || 0);
        btn.classList.toggle('active', f === fam);
        btn.dataset.count = n;          // rendered by CSS ::after
        btn.disabled = n === 0 && f !== 'all';
      });

      const label = fam === 'all' ? 'stories' : fam;
      document.getElementById('feedItemCount').textContent =
        `(${grouped.length} ${label} · ${events.length} mentions · ${this.rangeDescription()})`;

      if (events.length === 0) {
        listContainer.innerHTML = `
          <div class="glass-card" style="text-align: center; padding: 40px; color: var(--text-dim);">
            <i class="fa-solid fa-satellite-dish" style="font-size: 32px; color: var(--text-muted); margin-bottom: 12px; display:block;"></i>
            <h3 style="color: var(--text-primary); margin-bottom: 6px;">No market signals found</h3>
            <p style="font-size: 13px; max-width: 450px; margin: 0 auto 16px;">Try adjusting your confidence filters, or trigger live ingestion for more companies.</p>
            <button class="btn btn-primary" onclick="App.openIngestModal()">
              <i class="fa-solid fa-cloud-arrow-down"></i> Run Ingestion
            </button>
          </div>
        `;
        return;
      }

      listContainer.innerHTML = grouped.map(e => Components.renderSignalCard(e)).join('');
    } catch (e) {
      console.error('Error loading events:', e);
      listContainer.innerHTML = '<p class="text-dim" style="text-align:center;">Failed to load signals stream.</p>';
    }
  },

  async loadJobs() {
    try {
      const res = await fetch(`${API_BASE}/ingest/jobs`);
      const jobs = await res.json();
      this.state.jobs = jobs;

      const tbody = document.getElementById('jobsTableBody');
      if (tbody) {
        tbody.innerHTML = Components.renderJobsTable(jobs);
      }
    } catch (e) {
      console.error('Error loading jobs:', e);
    }
    this.loadExaUsage();
    this.loadSourceHealth();
  },

  async loadExaUsage() {
    const badge = document.getElementById('exaUsageBadge');
    if (!badge) return;
    try {
      const [newsRes, irRes, careerRes] = await Promise.all([
        fetch(`${API_BASE}/ingest/exa-usage`),
        fetch(`${API_BASE}/ingest/ir-usage`),
        fetch(`${API_BASE}/ingest/career-usage`)
      ]);
      const news = await newsRes.json();
      const ir = await irRes.json();
      const career = await careerRes.json();

      const fmt = (label, u, fallbackWarn) => {
        const pct = u.budget > 0 ? Math.round((u.used / u.budget) * 100) : 0;
        const warn = u.budget_exhausted
          ? ` — <strong style="color:#f87171;">budget hit${fallbackWarn ? ', ' + fallbackWarn : ''}</strong>`
          : '';
        return `<i class="fa-solid fa-shield-halved"></i> Exa ${label} usage this month: ${u.used} / ${u.budget} requests (${pct}%)${warn}`;
      };

      badge.innerHTML =
        fmt('News', news, 'falling back to free News RSS') +
        `<br>` + fmt('IR Press', ir) +
        `<br>` + fmt('Career Pages', career);
    } catch (e) {
      badge.textContent = 'Exa usage: unavailable';
    }
  },

  resetFilters() {
    this.state.filters = {
      sector: 'all',
      category: 'all',
      source: 'all',
      confidence: 0.70,
      search: ''
    };

    document.querySelectorAll('#sectorFilterGroup .pill-btn').forEach(b => b.classList.toggle('active', b.dataset.sector === 'all'));
    document.querySelectorAll('#sourceFilterGroup .pill-btn').forEach(b => b.classList.toggle('active', b.dataset.source === 'all'));
    
    const catSelect = document.getElementById('categorySelect');
    if (catSelect) catSelect.value = 'all';

    const searchInput = document.getElementById('searchInput');
    if (searchInput) searchInput.value = '';

    const confSlider = document.getElementById('confidenceSlider');
    const confVal = document.getElementById('confidenceVal');
    if (confSlider) confSlider.value = 70;
    if (confVal) confVal.textContent = '70%';

    this.loadEvents();
  },

  // Ingestion Modal Flow
  openIngestModal() {
    document.getElementById('ingestModal')?.classList.add('active');
    document.getElementById('ingestProgressBox')?.classList.add('hidden');
  },

  closeIngestModal() {
    document.getElementById('ingestModal')?.classList.remove('active');
  },

  async submitIngestion() {
    const companyId = document.getElementById('ingestCompanySelect').value;
    const sourceType = document.querySelector('input[name="ingestSourceType"]:checked')?.value || 'all';
    const limit = parseInt(document.getElementById('ingestDocLimit').value) || 3;

    const progressBox = document.getElementById('ingestProgressBox');
    const btnSubmit = document.getElementById('btnSubmitIngest');

    progressBox.classList.remove('hidden');
    btnSubmit.disabled = true;

    try {
      const res = await fetch(`${API_BASE}/ingest/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          company_id: companyId,
          source_type: sourceType,
          limit_docs: limit
        })
      });

      const data = await res.json();
      console.log('Ingestion success:', data);

      await this.loadStats();
      await this.loadCompanies();
      await this.loadEvents();
      await this.loadJobs();

      setTimeout(() => {
        btnSubmit.disabled = false;
        this.closeIngestModal();
      }, 600);
    } catch (e) {
      console.error('Ingestion error:', e);
      alert('Failed to run ingestion: ' + e.message);
      btnSubmit.disabled = false;
    }
  },

  async submitBatchIngest() {
    const sectorVal = document.getElementById('batchSectorSelect')?.value || 'BFSI';
    const scopeLabel = sectorVal === 'all' ? 'the FULL company universe (all sectors)' : `the ${sectorVal} sector`;
    if (!confirm(`Run batch ingestion across ${scopeLabel}? This runs in the background and can take a while.`)) return;

    try {
      const payload = { limit_docs: 2 };
      if (sectorVal !== 'all') payload.sector = sectorVal;
      const res = await fetch(`${API_BASE}/ingest/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      alert(data.message || 'Batch ingestion started in the background.');
      this.loadJobs();
      this.pollBatchJob();
    } catch (e) {
      alert('Error triggering batch ingest: ' + e.message);
    }
  },

  pollBatchJob(attemptsLeft = 60) {
    if (attemptsLeft <= 0) return;
    setTimeout(async () => {
      try {
        const res = await fetch(`${API_BASE}/ingest/jobs?limit=1`);
        const jobs = await res.json();
        const latest = jobs[0];
        this.loadJobs();
        this.loadStats();
        this.loadEvents();
        if (latest && latest.status === 'running') {
          this.pollBatchJob(attemptsLeft - 1);
        } else if (latest) {
          this.loadCompanies();
        }
      } catch (e) {
        console.error('Job poll failed:', e);
      }
    }, 5000);
  },

  // Taxonomy Modal Flow
  openTaxonomyModal() {
    const content = document.getElementById('taxonomyTreeContent');
    if (content && this.state.taxonomy) {
      content.innerHTML = Components.renderTaxonomyTree(this.state.taxonomy);
    }
    document.getElementById('taxonomyModal')?.classList.add('active');
  },

  closeTaxonomyModal() {
    document.getElementById('taxonomyModal')?.classList.remove('active');
  },

  // Company Detail Modal Flow
  /** Company clicks now navigate to the routed page instead of a modal. */
  async openCompanyDetail(companyId) {
    this.goToCompany(companyId);
  },

  async openCompanyDetailModal(companyId) {
    try {
      const range = this.rangeParams().toString();
      const [detailRes, timelineRes] = await Promise.all([
        fetch(`${API_BASE}/companies/${companyId}?${range}`),
        fetch(`${API_BASE}/companies/${companyId}/timeline?${range}`)
      ]);
      const data = await detailRes.json();
      const timeline = await timelineRes.json();

      document.getElementById('modalCompanyName').textContent = data.company.name;
      document.getElementById('modalCompanyTicker').textContent = data.company.ticker || 'News Only';
      document.getElementById('modalCompanySector').textContent = data.company.sector;

      const body = document.getElementById('companyModalBody');
      if (body) {
        body.innerHTML = Components.renderCompanyDetailModal(data, timeline);
      }

      document.getElementById('companyDetailModal')?.classList.add('active');
    } catch (e) {
      console.error('Error fetching company details:', e);
    }
  },

  closeCompanyModal() {
    document.getElementById('companyDetailModal')?.classList.remove('active');
  },

  async triggerIngestForCompany(companyId, companyName) {
    const btn = event.target.closest('button') || event.target;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Polling SEC EDGAR & News...';

    try {
      await fetch(`${API_BASE}/ingest/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_id: companyId, limit_docs: 3 })
      });
      await this.loadStats();
      await this.loadEvents();
      this.closeCompanyModal();
      alert(`Ingestion finished for ${companyName}! Signals updated.`);
    } catch (e) {
      alert('Ingestion error: ' + e.message);
      btn.disabled = false;
    }
  },

  // ==========================================================
  // Phase 3 — Ranked Signals
  // ==========================================================

  async loadSignals() {
    const list = document.getElementById('rankedSignalsList');
    if (!list) return;
    list.innerHTML = `<div class="loading-spinner-state"><div class="spinner"></div><p>Ranking signals...</p></div>`;
    try {
      const f = this.state.signalFilters;
      const params = new URLSearchParams();
      if (f.sector !== 'all') params.append('sector', f.sector);
      if (f.category !== 'all') params.append('category_id', f.category);
      if (f.minScore > 0) params.append('min_score', f.minScore);
      if (f.search) params.append('search', f.search);
      if (f.region !== 'all') params.append('region', f.region);
      params.append('limit', '100');
      this.withRange(params);
      const res = await fetch(`${API_BASE}/signals?${params.toString()}`);
      const data = await res.json();
      this.state.signals = data.signals || [];
      this.renderSignals();
    } catch (e) {
      console.error('Error loading signals:', e);
      list.innerHTML = '<p class="text-dim" style="text-align:center;">Failed to load ranked signals.</p>';
    }
  },

  renderSignals() {
    const list = document.getElementById('rankedSignalsList');
    if (!list) return;
    const timing = this.state.signalFilters.timing;
    const signals = timing === 'all' ? this.state.signals : this.state.signals.filter(s => s.timing_window === timing);
    document.getElementById('signalsCount').textContent = `(${signals.length} signals · ${this.rangeDescription()})`;
    if (!signals.length) {
      list.innerHTML = `
        <div class="glass-card" style="text-align:center; padding:40px; color:var(--text-dim);">
          <i class="fa-solid fa-ranking-star" style="font-size:32px; color:var(--text-muted); margin-bottom:12px; display:block;"></i>
          <h3 style="color:var(--text-primary); margin-bottom:6px;">No signals match these filters</h3>
          <p style="font-size:13px; max-width:450px; margin:0 auto 16px;">Nothing scored above ${this.state.signalFilters.minScore} in the ${escapeHtml(this.rangeDescription())}. Try a wider date range, a lower minimum score, or a different sector.</p>
          <button class="btn btn-outline" onclick="App.setDateRangePreset('all')"><i class="fa-regular fa-calendar-days"></i> Widen to all time</button>
        </div>`;
      return;
    }
    list.innerHTML = signals.map(s => Components.renderIntentSignalCard(s)).join('');
  },

  async toggleSignalWhy(signalId) {
    const panel = document.getElementById(`why-${signalId}`);
    if (!panel) return;
    if (!panel.classList.contains('hidden')) {
      panel.classList.add('hidden');
      return;
    }
    if (!panel.dataset.loaded) {
      panel.innerHTML = `<div class="loading-spinner-state" style="padding:12px;"><div class="spinner-small"></div><p style="font-size:12px;">Loading evidence...</p></div>`;
      panel.classList.remove('hidden');
      try {
        const res = await fetch(`${API_BASE}/signals/${signalId}?${this.rangeParams().toString()}`);
        const detail = await res.json();
        panel.innerHTML = Components.renderSignalBreakdown(detail);
        panel.dataset.loaded = '1';
      } catch (e) {
        panel.innerHTML = '<p class="text-dim">Could not load signal detail.</p>';
      }
    } else {
      panel.classList.remove('hidden');
    }
  },

  resetSignalFilters() {
    this.state.signalFilters = { sector: 'all', category: 'all', minScore: 40, search: '', timing: 'all' };
    document.querySelectorAll('#signalSectorFilterGroup .pill-btn').forEach(b => b.classList.toggle('active', b.dataset.sector === 'all'));
    document.querySelectorAll('#timingFilterGroup .pill-btn').forEach(b => b.classList.toggle('active', b.dataset.timing === 'all'));
    const sel = document.getElementById('signalCategorySelect'); if (sel) sel.value = 'all';
    const inp = document.getElementById('signalSearchInput'); if (inp) inp.value = '';
    const slider = document.getElementById('scoreSlider'); if (slider) slider.value = 40;
    const val = document.getElementById('scoreVal'); if (val) val.textContent = '40';
    this.loadSignals();
  },

  async recomputeSignals() {
    const btn = document.getElementById('btnRecomputeSignals');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Recomputing...'; }
    try {
      const res = await fetch(`${API_BASE}/signals/recompute`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({})
      });
      const summary = await res.json();
      console.log('Recompute summary:', summary);
      await this.loadSignals();
      await this.loadAlerts();
      if (summary.alerts_created > 0) alert(`Recompute complete — ${summary.alerts_created} new alert(s) raised.`);
    } catch (e) {
      alert('Recompute failed: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-calculator"></i> Recompute'; }
    }
  },

  // ==========================================================
  // Phase 3 — ICP leads, Watchlists, Alerts
  // ==========================================================

  populateIcpOptions() {
    const tax = this.state.taxonomy;
    const sectorHtml = this.SECTORS.map(s =>
      `<label class="check-chip"><input type="checkbox" value="${escapeHtml(s)}" ${s === 'BFSI' ? 'checked' : ''}> ${escapeHtml(s)}</label>`).join('');
    const initHtml = (tax?.categories || []).flatMap(cat => cat.initiatives.map(init =>
      `<label class="check-chip" title="${escapeHtml(init.it_offering || '')}"><input type="checkbox" value="${init.id}"> ${escapeHtml(init.name)}</label>`)).join('');
    ['icpSectorOptions', 'wlSectorOptions'].forEach(id => { const el = document.getElementById(id); if (el) el.innerHTML = sectorHtml; });
    ['icpInitiativeOptions', 'wlInitiativeOptions'].forEach(id => { const el = document.getElementById(id); if (el) el.innerHTML = initHtml; });
  },

  _checkedValues(containerId) {
    return Array.from(document.querySelectorAll(`#${containerId} input[type="checkbox"]:checked`)).map(i => i.value);
  },

  async runIcpLeads() {
    const out = document.getElementById('icpLeadResults');
    if (!out) return;
    const sectors = this._checkedValues('icpSectorOptions');
    const inits = this._checkedValues('icpInitiativeOptions');
    const minScore = parseInt(document.getElementById('icpMinScore')?.value || '60', 10);
    out.innerHTML = `<div class="loading-spinner-state"><div class="spinner"></div><p>Ranking leads for your ICP...</p></div>`;
    try {
      const params = new URLSearchParams();
      if (sectors.length) params.append('sectors', sectors.join(','));
      if (inits.length) params.append('initiative_ids', inits.join(','));
      params.append('min_score', minScore);
      params.append('limit', '50');
      this.withRange(params);
      const res = await fetch(`${API_BASE}/signals/leads?${params.toString()}`);
      const data = await res.json();
      const title = `ICP: ${sectors.length ? sectors.join(', ') : 'all sectors'} × ${inits.length ? inits.length + ' initiative type(s)' : 'all initiatives'} · min score ${minScore} · ${this.rangeDescription()}`;
      out.innerHTML = Components.renderLeadTable(data, title);
      // Export/CRM actions only make sense once a list exists
      document.getElementById('btnExportLeads')?.classList.toggle('hidden', !data.leads?.length);
      if (data.leads?.length) this.refreshCrmStatus();
    } catch (e) {
      out.innerHTML = '<p class="text-dim">Failed to generate lead list.</p>';
    }
  },

  async loadWatchlists() {
    const list = document.getElementById('watchlistsList');
    if (!list) return;
    try {
      const res = await fetch(`${API_BASE}/watchlists`);
      this.state.watchlists = await res.json();
      list.innerHTML = this.state.watchlists.length
        ? this.state.watchlists.map(w => Components.renderWatchlistCard(w)).join('')
        : `<div class="lead-empty text-dim"><i class="fa-solid fa-list-ul"></i> No watchlists yet — create one to track target accounts and receive threshold alerts.</div>`;
    } catch (e) {
      console.error('Error loading watchlists:', e);
    }
  },

  async viewWatchlistLeads(id) {
    const out = document.getElementById('watchlistLeadResults');
    if (!out) return;
    out.innerHTML = `<div class="loading-spinner-state"><div class="spinner"></div><p>Ranking watchlist leads...</p></div>`;
    try {
      const res = await fetch(`${API_BASE}/watchlists/${id}/leads?${this.rangeParams().toString()}`);
      const data = await res.json();
      out.innerHTML = Components.renderLeadTable(data, `Leads for "${data.watchlist?.name || 'watchlist'}" · ${this.rangeDescription()}`);
      out.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) {
      out.innerHTML = '<p class="text-dim">Failed to load watchlist leads.</p>';
    }
  },

  openWatchlistModal(id = null) {
    this.state.editingWatchlistId = id;
    const wl = id ? this.state.watchlists.find(w => w.id === id) : null;
    document.getElementById('watchlistModalTitle').textContent = wl ? 'Edit Watchlist' : 'New Watchlist';
    document.getElementById('wlName').value = wl?.name || '';
    document.getElementById('wlDescription').value = wl?.description || '';
    document.getElementById('wlThreshold').value = wl?.alert_threshold ?? 70;
    document.getElementById('wlCompanySearch').value = '';
    document.getElementById('wlCompanyResults').innerHTML = '';
    document.querySelectorAll('#wlSectorOptions input').forEach(i => { i.checked = (wl?.icp_sectors || []).includes(i.value); });
    document.querySelectorAll('#wlInitiativeOptions input').forEach(i => { i.checked = (wl?.icp_initiative_ids || []).includes(i.value); });

    this.state.wlSelectedCompanies = [];
    const known = new Map(this.state.companies.map(c => [c.id, c]));
    (wl?.company_ids || []).forEach(cid => {
      const c = known.get(cid);
      this.state.wlSelectedCompanies.push(c ? { id: c.id, name: c.name, ticker: c.ticker } : { id: cid, name: cid.slice(0, 8), ticker: '' });
    });
    if (wl && wl.company_ids.length && wl.company_ids.some(cid => !known.has(cid))) {
      fetch(`${API_BASE}/watchlists/${id}`).then(r => r.json()).then(d => {
        if (d.companies) {
          this.state.wlSelectedCompanies = d.companies.map(c => ({ id: c.id, name: c.name, ticker: c.ticker }));
          this.renderWlSelected();
        }
      });
    }
    this.renderWlSelected();
    document.getElementById('watchlistModal')?.classList.add('active');
  },

  closeWatchlistModal() {
    document.getElementById('watchlistModal')?.classList.remove('active');
  },

  async searchWatchlistCompanies(q) {
    const out = document.getElementById('wlCompanyResults');
    if (!out) return;
    if (!q || q.trim().length < 2) { out.innerHTML = ''; return; }
    try {
      const res = await fetch(`${API_BASE}/companies?search=${encodeURIComponent(q.trim())}&limit=12`);
      const companies = await res.json();
      const selected = new Set(this.state.wlSelectedCompanies.map(c => c.id));
      out.innerHTML = companies.filter(c => !selected.has(c.id)).map(c =>
        `<button type="button" class="pill-btn" onclick="App.addWlCompany('${c.id}', '${escapeHtml(c.name)}', '${escapeHtml(c.ticker || '')}')">
           <span>${escapeHtml(c.name)}</span><span class="ticker-pill">${escapeHtml(c.ticker || c.sector)}</span>
         </button>`).join('') || '<span class="text-dim" style="font-size:12px;">No matches</span>';
    } catch (e) {
      out.innerHTML = '';
    }
  },

  addWlCompany(id, name, ticker) {
    if (!this.state.wlSelectedCompanies.some(c => c.id === id)) {
      this.state.wlSelectedCompanies.push({ id, name, ticker });
      this.renderWlSelected();
    }
    document.getElementById('wlCompanySearch').value = '';
    document.getElementById('wlCompanyResults').innerHTML = '';
  },

  removeWlCompany(id) {
    this.state.wlSelectedCompanies = this.state.wlSelectedCompanies.filter(c => c.id !== id);
    this.renderWlSelected();
  },

  renderWlSelected() {
    const el = document.getElementById('wlSelectedCompanies');
    if (!el) return;
    el.innerHTML = this.state.wlSelectedCompanies.length
      ? this.state.wlSelectedCompanies.map(c =>
          `<span class="chip-selected">${escapeHtml(c.ticker || c.name)} <button type="button" onclick="App.removeWlCompany('${c.id}')" title="Remove">&times;</button></span>`).join('')
      : '<span class="text-dim" style="font-size:12px;">No specific accounts — the watchlist will match every company in the selected ICP sectors.</span>';
  },

  async saveWatchlist() {
    const name = document.getElementById('wlName').value.trim();
    if (!name) { alert('Give the watchlist a name.'); return; }
    const payload = {
      name,
      description: document.getElementById('wlDescription').value.trim() || null,
      company_ids: this.state.wlSelectedCompanies.map(c => c.id),
      icp_sectors: this._checkedValues('wlSectorOptions'),
      icp_initiative_ids: this._checkedValues('wlInitiativeOptions'),
      alert_threshold: parseInt(document.getElementById('wlThreshold').value || '70', 10)
    };
    if (!payload.company_ids.length && !payload.icp_sectors.length) {
      alert('Pick at least one target account or one ICP sector, otherwise the watchlist can never match anything.');
      return;
    }
    const id = this.state.editingWatchlistId;
    try {
      const res = await fetch(id ? `${API_BASE}/watchlists/${id}` : `${API_BASE}/watchlists`, {
        method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
      });
      const data = await res.json();
      this.closeWatchlistModal();
      await this.loadWatchlists();
      await this.loadAlerts();
      if (data.alerts_created > 0) alert(`${data.alerts_created} account(s) in this watchlist are already above the threshold — alerts raised.`);
    } catch (e) {
      alert('Failed to save watchlist: ' + e.message);
    }
  },

  async deleteWatchlist(id, name) {
    if (!confirm(`Delete watchlist "${name}"?`)) return;
    try {
      await fetch(`${API_BASE}/watchlists/${id}`, { method: 'DELETE' });
      await this.loadWatchlists();
      const out = document.getElementById('watchlistLeadResults'); if (out) out.innerHTML = '';
    } catch (e) {
      alert('Failed to delete watchlist: ' + e.message);
    }
  },

  async loadAlerts() {
    try {
      const [listRes, countRes] = await Promise.all([
        fetch(`${API_BASE}/alerts?limit=50`),
        fetch(`${API_BASE}/alerts/unread-count`)
      ]);
      this.state.alerts = await listRes.json();
      const unread = (await countRes.json()).unread || 0;
      const badge = document.getElementById('alertCountBadge');
      if (badge) { badge.textContent = unread; badge.classList.toggle('hidden', unread === 0); }
      const count = document.getElementById('alertsCount');
      if (count) count.textContent = unread ? `(${unread} unread)` : '';
      const list = document.getElementById('alertsList');
      if (list) {
        list.innerHTML = this.state.alerts.length
          ? this.state.alerts.map(a => Components.renderAlertItem(a)).join('')
          : `<div class="lead-empty text-dim"><i class="fa-regular fa-bell-slash"></i> No alerts yet. Alerts fire when a watchlisted account's signal crosses its threshold.</div>`;
      }
    } catch (e) {
      console.error('Error loading alerts:', e);
    }
  },

  async markAlertRead(id) {
    try {
      await fetch(`${API_BASE}/alerts/${id}/read`, { method: 'POST' });
      await this.loadAlerts();
    } catch (e) { console.error(e); }
  },

  async markAllAlertsRead() {
    try {
      await fetch(`${API_BASE}/alerts/read-all`, { method: 'POST' });
      await this.loadAlerts();
    } catch (e) { console.error(e); }
  },

  // Utility Debounce
  debounce(func, wait) {
    let timeout;
    return (...args) => {
      clearTimeout(timeout);
      timeout = setTimeout(() => func.apply(this, args), wait);
    };
  }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  App.init();
});
