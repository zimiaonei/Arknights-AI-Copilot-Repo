/**
 * PRTS · MAA Google Material 3 (Material You) Application Controller
 * Zero Emoji, Zero External Dependencies, High-DPI Friendly
 */
(function (window, document) {
  'use strict';

  var VALID_PALETTES = ['sky', 'sage', 'lavender', 'sand'];
  var VALID_SCHEMES = ['light', 'dark'];
  var LS_PALETTE_KEY = 'prts_m3_palette';
  var LS_SCHEME_KEY = 'prts_m3_scheme';
  var MUTEX_TASK_BUTTON_IDS = [
    'btn-start-daily',
    'btn-start-annihilation',
    'btn-start-push',
    'btn-start-auto-growth',
    'btn-exec-growth-from-preview',
    'btn-sync-box',
    'btn-start-roguelike',
    'btn-rl-settlement'
  ];
  var lastToastSig = '';
  var lastToastTs = 0;

  // State Store
  var appState = {
    activePage: 'page-overview',
    palette: 'sky',
    scheme: 'light',
    options: {},
    snapshot: {},
    taskState: {},
    toolboxCatalog: null,
    activeToolboxId: '',
    logs: [],
    logFilterTag: 'all',
    logSearchQuery: '',
    logRingBufferSize: 2000,
    drawerExpanded: false,
    wizardStep: 1,
    wizardForced: false,
    wizardMaaValid: false,
    pendingAdbProbe: false,
    updateReleaseInfo: null,
    updateLocalVer: '',
    updateRemoteVer: ''
  };

  // Helper DOM selectors
  function $(id) {
    return document.getElementById(id);
  }

  function $$(selector) {
    return document.querySelectorAll(selector);
  }

  function safeStorageGet(key, fallback) {
    try {
      var val = window.localStorage ? window.localStorage.getItem(key) : null;
      return val || fallback;
    } catch (e) {
      return fallback;
    }
  }

  function safeStorageSet(key, value) {
    try {
      if (window.localStorage) {
        window.localStorage.setItem(key, value);
      }
    } catch (e) {
      // Ignore storage restrictions
    }
  }

  function handleBridgeError(contextLabel) {
    return function (err) {
      var msg = (err && err.message) ? err.message : String(err || '请求异常');
      showToast('error', contextLabel || '操作失败', msg);
    };
  }

  // Material You Theme & Palette Controller
  function applyTheme(palette, scheme, persist) {
    var nextPalette = VALID_PALETTES.indexOf(palette) !== -1 ? palette : (appState.palette || 'sky');
    var nextScheme = VALID_SCHEMES.indexOf(scheme) !== -1 ? scheme : (appState.scheme || 'light');

    appState.palette = nextPalette;
    appState.scheme = nextScheme;

    var root = document.documentElement;
    if (root) {
      root.setAttribute('data-palette', nextPalette);
      root.setAttribute('data-theme', nextScheme);
    }

    if (persist !== false) {
      safeStorageSet(LS_PALETTE_KEY, nextPalette);
      safeStorageSet(LS_SCHEME_KEY, nextScheme);
    }

    var swatches = $$('.palette-swatch-btn');
    for (var i = 0; i < swatches.length; i++) {
      if (swatches[i].getAttribute('data-palette') === nextPalette) {
        swatches[i].classList.add('active');
      } else {
        swatches[i].classList.remove('active');
      }
    }

    var modeBtns = $$('.theme-mode-btn');
    for (var j = 0; j < modeBtns.length; j++) {
      if (modeBtns[j].getAttribute('data-theme-mode') === nextScheme) {
        modeBtns[j].classList.add('active');
      } else {
        modeBtns[j].classList.remove('active');
      }
    }

    var themeIcon = $('icon-theme-toggle');
    if (themeIcon) {
      themeIcon.innerHTML = nextScheme === 'dark' ? '<use href="#icon-sun"/>' : '<use href="#icon-moon"/>';
    }
  }

  // Apply persisted theme synchronously before initial paint
  applyTheme(safeStorageGet(LS_PALETTE_KEY, 'sky'), safeStorageGet(LS_SCHEME_KEY, 'light'), false);
  window.__prtsSetTheme = function (palette, scheme) {
    applyTheme(palette, scheme, true);
  };

  function setupThemeControls() {
    applyTheme(appState.palette, appState.scheme, false);

    var btnPalette = $('btn-palette-popover');
    var popover = $('palette-popover');
    var btnThemeToggle = $('btn-theme-toggle');

    if (btnPalette && popover) {
      btnPalette.addEventListener('click', function (e) {
        e.stopPropagation();
        var isHidden = popover.classList.contains('hidden');
        if (isHidden) {
          popover.classList.remove('hidden');
          btnPalette.classList.add('active');
        } else {
          popover.classList.add('hidden');
          btnPalette.classList.remove('active');
        }
      });

      popover.addEventListener('click', function (e) {
        e.stopPropagation();
      });

      document.addEventListener('click', function () {
        if (!popover.classList.contains('hidden')) {
          popover.classList.add('hidden');
          btnPalette.classList.remove('active');
        }
      });
    }

    if (btnThemeToggle) {
      btnThemeToggle.addEventListener('click', function () {
        var nextScheme = appState.scheme === 'dark' ? 'light' : 'dark';
        applyTheme(appState.palette, nextScheme, true);
      });
    }

    var swatches = $$('.palette-swatch-btn');
    for (var i = 0; i < swatches.length; i++) {
      swatches[i].addEventListener('click', function () {
        var pal = this.getAttribute('data-palette');
        applyTheme(pal, appState.scheme, true);
      });
    }

    var modeBtns = $$('.theme-mode-btn');
    for (var j = 0; j < modeBtns.length; j++) {
      modeBtns[j].addEventListener('click', function () {
        var mode = this.getAttribute('data-theme-mode');
        applyTheme(appState.palette, mode, true);
      });
    }
  }

  // M3 Snackbar Toast Notification System
  function showToast(level, title, message) {
    var container = $('toast-container');
    if (!container) return;

    var normalized = level || 'info';
    var msgText = String(message || '');
    var sig = normalized + '|' + msgText;
    var now = Date.now();
    if (msgText && sig === lastToastSig && (now - lastToastTs) < 1200) {
      return;
    }
    lastToastSig = sig;
    lastToastTs = now;

    var toast = document.createElement('div');
    toast.className = 'toast toast-' + normalized;

    var dot = document.createElement('span');
    var dotClass = normalized === 'success' ? 'success' : (normalized === 'warn' ? 'warn' : (normalized === 'error' ? 'danger' : 'neutral'));
    dot.className = 'status-dot ' + dotClass;
    toast.appendChild(dot);

    var text = document.createElement('span');
    text.textContent = (title ? '[' + title + '] ' : '') + msgText;
    toast.appendChild(text);

    container.appendChild(toast);
    setTimeout(function () {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    }, 4000);
  }

  function handleTaskStartResult(contextTitle) {
    return function (res) {
      if (res && res.task_state) {
        updateTaskStateUI(res.task_state);
      }
      if (res && res.ok === false) {
        var msg = res.error || res.message || '当前无法启动任务，请先等待现有任务完成或检查配置。';
        showToast('warn', contextTitle || '操作被拦截', msg);
      }
    };
  }

  function guardTaskNotRunning(contextTitle) {
    if (appState.taskState && appState.taskState.running) {
      var curStatus = appState.taskState.status_text || '其他任务';
      showToast('warn', contextTitle || '任务运行中', '当前已有任务（' + curStatus + '）在运行中，请先等待完成或点击「停止任务」。');
      return false;
    }
    return true;
  }

  // Navigation Drawer & Page View Switching
  function setupNavigation() {
    var navItems = $$('.nav-item');
    for (var i = 0; i < navItems.length; i++) {
      navItems[i].addEventListener('click', function () {
        var targetPageId = this.getAttribute('data-page');
        if (targetPageId) {
          switchPage(targetPageId);
        }
      });
    }

    var drawerJumpBtn = $('drawer-btn-jump-terminal');
    if (drawerJumpBtn) {
      drawerJumpBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        switchPage('page-terminal');
      });
    }

    var drawerHeader = $('drawer-header-toggle');
    if (drawerHeader) {
      drawerHeader.addEventListener('click', function () {
        toggleBottomDrawer();
      });
    }
  }

  function switchPage(pageId) {
    appState.activePage = pageId;

    var navItems = $$('.nav-item');
    for (var i = 0; i < navItems.length; i++) {
      if (navItems[i].getAttribute('data-page') === pageId) {
        navItems[i].classList.add('active');
      } else {
        navItems[i].classList.remove('active');
      }
    }

    var pages = $$('.page');
    for (var j = 0; j < pages.length; j++) {
      if (pages[j].id === pageId) {
        pages[j].classList.add('active');
      } else {
        pages[j].classList.remove('active');
      }
    }

    var drawer = $('bottom-log-drawer');
    if (drawer) {
      if (pageId === 'page-terminal') {
        drawer.classList.add('hidden');
      } else {
        drawer.classList.remove('hidden');
      }
    }

    if (pageId === 'page-terminal') {
      renderTerminalLogs();
    } else if (pageId === 'page-toolbox') {
      if (!appState.toolboxCatalog) {
        loadToolboxCatalog();
      } else {
        renderToolboxDetail();
      }
    }
  }

  function toggleBottomDrawer(forceState) {
    var drawer = $('bottom-log-drawer');
    var icon = $('drawer-toggle-icon');
    if (!drawer) return;

    if (typeof forceState === 'boolean') {
      appState.drawerExpanded = forceState;
    } else {
      appState.drawerExpanded = !appState.drawerExpanded;
    }

    if (appState.drawerExpanded) {
      drawer.classList.add('expanded');
      if (icon) icon.innerHTML = '<use href="#icon-chevron-down"/>';
      renderDrawerPreviewLogs();
    } else {
      drawer.classList.remove('expanded');
      if (icon) icon.innerHTML = '<use href="#icon-chevron-up"/>';
    }
  }

  // Modals Manager
  function openModal(modalId) {
    var modal = $(modalId);
    if (modal) {
      modal.classList.remove('hidden');
    }
  }

  function closeModal(modalId) {
    if (modalId === 'modal-wizard' && appState.wizardForced) {
      return;
    }
    var modal = $(modalId);
    if (modal) {
      modal.classList.add('hidden');
    }
  }

  function closeTopmostModal() {
    var popover = $('palette-popover');
    var btnPalette = $('btn-palette-popover');
    if (popover && !popover.classList.contains('hidden')) {
      popover.classList.add('hidden');
      if (btnPalette) btnPalette.classList.remove('active');
      return;
    }

    var modalOrder = ['modal-growth-preview', 'modal-maa-update', 'modal-agent', 'modal-settings', 'modal-wizard'];
    for (var i = 0; i < modalOrder.length; i++) {
      var m = $(modalOrder[i]);
      if (m && !m.classList.contains('hidden')) {
        if (modalOrder[i] === 'modal-wizard' && appState.wizardForced) {
          return;
        }
        closeModal(modalOrder[i]);
        return;
      }
    }
  }

  function setupModals() {
    var closeButtons = $$('[data-close]');
    for (var i = 0; i < closeButtons.length; i++) {
      closeButtons[i].addEventListener('click', function () {
        var targetId = this.getAttribute('data-close');
        if (targetId) closeModal(targetId);
      });
    }

    // Backdrop click closes non-forced modals
    var overlays = $$('.modal-overlay');
    for (var o = 0; o < overlays.length; o++) {
      overlays[o].addEventListener('click', function (e) {
        if (e.target === this && this.id) {
          closeModal(this.id);
        }
      });
    }

    // Escape key closes popover / topmost modal
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        closeTopmostModal();
      }
    });

    // Open settings button
    var btnOpenSettings = $('btn-open-settings-modal');
    if (btnOpenSettings) {
      btnOpenSettings.addEventListener('click', function () {
        var panes = $$('.settings-tab-pane');
        for (var p = 0; p < panes.length; p++) {
          if (panes[p].style.display) {
            panes[p].style.display = '';
          }
        }
        var modalBody = document.querySelector('#modal-settings .modal-body');
        if (modalBody) modalBody.scrollTop = 0;
        loadSettingsIntoModal();
        openModal('modal-settings');
      });
    }

    // Open agent connect dialog
    var btnOpenAgent = $('btn-open-agent-modal');
    if (btnOpenAgent) {
      btnOpenAgent.addEventListener('click', function () {
        loadAgentPromptsIntoModal();
        openModal('modal-agent');
      });
    }

    // Settings Modal Tabs
    var setTabBtns = $$('#settings-tabs-bar .modal-tab-btn');
    for (var j = 0; j < setTabBtns.length; j++) {
      setTabBtns[j].addEventListener('click', function () {
        for (var k = 0; k < setTabBtns.length; k++) {
          setTabBtns[k].classList.remove('active');
          setTabBtns[k].setAttribute('aria-selected', 'false');
        }
        this.classList.add('active');
        this.setAttribute('aria-selected', 'true');
        var targetTab = this.getAttribute('data-tab');
        var panes = $$('.settings-tab-pane');
        for (var p = 0; p < panes.length; p++) {
          if (panes[p].style.display) {
            panes[p].style.display = '';
          }
          if (panes[p].id === targetTab) {
            panes[p].classList.add('active');
          } else {
            panes[p].classList.remove('active');
          }
        }
        var modalBody = document.querySelector('#modal-settings .modal-body');
        if (modalBody) modalBody.scrollTop = 0;
      });
    }

    // Agent Modal Tabs
    var agentTabBtns = $$('#agent-tabs-bar .modal-tab-btn');
    for (var a = 0; a < agentTabBtns.length; a++) {
      agentTabBtns[a].addEventListener('click', function () {
        for (var b = 0; b < agentTabBtns.length; b++) agentTabBtns[b].classList.remove('active');
        this.classList.add('active');
        var targetTab = this.getAttribute('data-tab');
        var panes = $$('.agent-tab-pane');
        for (var p2 = 0; p2 < panes.length; p2++) {
          if (panes[p2].style.display) {
            panes[p2].style.display = '';
          }
          if (panes[p2].id === targetTab) {
            panes[p2].classList.add('active');
          } else {
            panes[p2].classList.remove('active');
          }
        }
      });
    }
  }

  // Banner Helpers
  function applyAgentBanner(payload) {
    var banner = $('banner-agent');
    if (!banner) return;
    if (payload && payload.visible) {
      banner.classList.remove('hidden');
      if (payload.title && $('banner-agent-title')) $('banner-agent-title').textContent = payload.title;
      if (payload.subtitle !== undefined && $('banner-agent-sub')) $('banner-agent-sub').textContent = payload.subtitle;
    } else {
      banner.classList.add('hidden');
    }
  }

  function applyTutorialBanner(payload) {
    var banner = $('banner-tutorial');
    if (!banner) return;
    if (payload && payload.visible) {
      banner.classList.remove('hidden');
      if (payload.title && $('banner-tutorial-title')) $('banner-tutorial-title').textContent = payload.title;
      if (payload.subtitle !== undefined && $('banner-tutorial-sub')) $('banner-tutorial-sub').textContent = payload.subtitle;
    } else {
      banner.classList.add('hidden');
    }
  }

  function syncDryRunUI(isDry) {
    var pill = $('top-pill-dry-run');
    var sw = $('set-sw-dry-run');
    if (pill) pill.classList.toggle('hidden', !isDry);
    if (sw) sw.checked = isDry;
  }

  function openSettingsAtAiTab() {
    var panes = $$('.settings-tab-pane');
    for (var p = 0; p < panes.length; p++) {
      if (panes[p].style.display) panes[p].style.display = '';
    }
    loadSettingsIntoModal();
    openModal('modal-settings');
    var btns = $$('#settings-tabs-bar .modal-tab-btn');
    for (var i = 0; i < btns.length; i++) {
      if (btns[i].getAttribute('data-tab') === 'tab-set-ai') {
        btns[i].click();
        break;
      }
    }
  }

  function bindDryRunControls() {
    var sw = $('set-sw-dry-run');
    if (sw) {
      sw.addEventListener('change', function () {
        var want = Boolean(sw.checked);
        sw.disabled = true;
        PrtsBridge.setDryRun(want).then(function (res) {
          sw.disabled = false;
          if (!res || res.ok === false) {
            sw.checked = !want;
            showToast('error', '演习模式', (res && (res.error || res.message)) || '切换失败');
            return;
          }
          var on = res.dry_run != null ? Boolean(res.dry_run) : want;
          syncDryRunUI(on);
          if (appState.snapshot) appState.snapshot.dry_run = on;
          showToast(on ? 'warn' : 'success', '演习模式', on ? '演习模式已开启（仅当前会话生效）' : '演习模式已关闭');
        }).catch(function (err) {
          sw.disabled = false;
          sw.checked = !want;
          showToast('error', '演习模式', '切换失败：' + (err && err.message ? err.message : err));
        });
      });
    }
    var pill = $('top-pill-dry-run');
    if (pill) {
      pill.addEventListener('click', openSettingsAtAiTab);
    }
  }

  // Top Bar & Snapshot Binding
  function updateSnapshotUI(snap) {
    if (!snap) return;
    appState.snapshot = snap;

    // 0. Dry-Run (runtime-only) badge + settings switch sync
    if (Object.prototype.hasOwnProperty.call(snap, 'dry_run')) {
      syncDryRunUI(Boolean(snap.dry_run));
    }

    // 1. MAA Status Pill
    var dotMaa = $('dot-maa');
    var textMaaVer = $('text-maa-ver');
    if (snap.maa) {
      if (snap.maa.valid) {
        if (dotMaa) dotMaa.className = 'status-dot success';
        if (textMaaVer) textMaaVer.textContent = 'MAA ' + (snap.maa.version || '已就绪');
      } else {
        if (dotMaa) dotMaa.className = 'status-dot danger';
        if (textMaaVer) textMaaVer.textContent = 'MAA 未定位';
      }
    }

    // 2. ADB Status Pill
    updateAdbUI(snap.adb);

    // 3. AI Mode Tonal Pill
    var dotAi = $('dot-ai');
    var textAiMode = $('text-ai-mode');
    if (snap.ai) {
      if (textAiMode) textAiMode.textContent = snap.ai.label || (snap.ai.mode === 'builtin_api' ? '内置 API' : '桌面 Agent 协同');
      if (dotAi) {
        dotAi.className = 'status-dot ' + (snap.ai.mode === 'builtin_api' ? 'success' : 'agent');
      }
    }

    // 4. Page 1 KPI Tiles
    var kpiBox = $('kpi-box-count');
    var kpiBoxSub = $('kpi-box-sub');
    if (snap.box && kpiBox) {
      kpiBox.textContent = (snap.box.total_owned != null ? snap.box.total_owned : '--') + ' 名干员';
      if (kpiBoxSub) kpiBoxSub.textContent = '精二干员: ' + (snap.box.e2_count != null ? snap.box.e2_count : '--') + ' 名';
    }

    var kpiAnni = $('kpi-anni-progress');
    var kpiAnniLabel = $('kpi-anni-label');
    if (snap.annihilation && kpiAnni) {
      var proc = snap.annihilation.process;
      var procText = '-- / 1800';
      if (Array.isArray(proc) && proc.length >= 2) {
        procText = proc[0] + ' / ' + proc[1];
      } else if (typeof proc === 'string' && proc.trim()) {
        var cleanProc = proc.replace(/[\uD83C-\uDBFF\uDC00-\uDFFF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]/g, '').trim();
        if (cleanProc.indexOf(',') !== -1) {
          var parts = cleanProc.split(',');
          procText = parts[0].trim() + ' / ' + parts[1].trim();
        } else if (cleanProc.indexOf('/') !== -1) {
          var parts2 = cleanProc.split('/');
          procText = parts2[0].trim() + ' / ' + parts2[1].trim();
        } else {
          procText = cleanProc;
        }
      }
      kpiAnni.textContent = procText;

      if (kpiAnniLabel) {
        var rawLabel = snap.annihilation.label || (snap.annihilation.done ? '本周已完成' : '周进度');
        var cleanLabel = rawLabel.replace(/[\uD83C-\uDBFF\uDC00-\uDFFF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]/g, '').trim();
        if (snap.annihilation.done) {
          kpiAnniLabel.innerHTML = '<svg class="icon icon-sm" style="color: var(--md-status-success); margin-right: 4px;"><use href="#icon-check"/></svg><span>' + escapeHtml(cleanLabel || '本周已完成') + '</span>';
        } else {
          kpiAnniLabel.textContent = cleanLabel;
        }
      }
    }

    var kpiGrowthStage = $('kpi-growth-stage');
    var kpiGrowthReason = $('kpi-growth-reason');
    if (snap.growth && kpiGrowthStage) {
      kpiGrowthStage.textContent = snap.growth.first_stage || snap.growth.recommended_stages || '--';
      if (kpiGrowthReason) kpiGrowthReason.textContent = snap.growth.reason || '今日推荐刷取';
    }

    // 5. Page 1 Quick Preference Bar
    if (snap.server) {
      var srv = snap.server;
      if (srv === '官服') srv = 'Official';
      else if (srv === 'B服') srv = 'Bilibili';
      updateSegmentedUI('seg-server', srv);
    }
    if (snap.post_action) {
      var act = snap.post_action;
      if (act === '无') act = 'none';
      else if (act === '关闭模拟器') act = 'close_emulator';
      updateSegmentedUI('seg-post-action', act);
    }
    var quickSummary = $('pref-quick-summary');
    if (quickSummary && snap.quick_summary) {
      quickSummary.textContent = snap.quick_summary.text || 'AUTO 模式 | 自动推通未解锁';
    }

    // 6. Page 2 Depot Sync status
    var badgeDepot = $('badge-depot-time');
    var depotDesc = $('depot-summary-desc');
    if (snap.depot) {
      if (badgeDepot) badgeDepot.textContent = snap.depot.syncTime ? '已同步: ' + snap.depot.syncTime.split(' ')[0] : '未同步';
      if (depotDesc && snap.depot.count != null) {
        depotDesc.textContent = '当前仓库已识别 ' + snap.depot.count + ' 种物资明细，点击刷新将启动 MAA 识别更新。';
      }
    }

    // 7. Page 3 Roguelike Card
    var rl = snap.roguelike;
    if (rl) {
      var badgeRlTheme = $('badge-rl-theme');
      if (badgeRlTheme) badgeRlTheme.textContent = rl.theme_display || rl.theme || '沉沦者的黑流树海';
      var selectCardMode = $('select-rl-card-mode');
      if (selectCardMode && rl.card_mode) {
        selectCardMode.value = rl.card_mode;
      }
      var rlDesc = $('rl-structured-desc');
      if (rlDesc) {
        rlDesc.textContent = '当前目标：' + (rl.target_display || rl.mode_label || '默认刷取') +
          ' | 局后领奖：' + (rl.auto_claim_rewards ? '开' : '关') +
          ' | 科技树加点：' + (rl.auto_upgrade_tech_tree ? '开' : '关') +
          (rl.sub_hint ? ' (' + rl.sub_hint + ')' : '');
      }
    }

    // 8. Page 3 Reclamation Card
    var ra = snap.reclamation;
    if (ra) {
      var badgeRaTheme = $('badge-ra-theme');
      if (badgeRaTheme) badgeRaTheme.textContent = ra.theme_display || ra.theme || '重启锚点';
      var badgeRaMode = $('badge-ra-mode');
      if (badgeRaMode) badgeRaMode.textContent = ra.badge_text || ra.mode_display || ra.mode_label || '精耕细作';
      var raDesc = $('ra-structured-desc');
      if (raDesc) {
        raDesc.textContent = '模式：' + (ra.mode_display || ra.mode_label || 'RA-1') +
          ' | 搬空商店：' + (ra.clear_store ? '开' : '关') +
          (ra.loaded_from_gui_config ? ' | 继承原版 MAA 配置' : '');
      }

      if (appState.activePage === 'page-toolbox' && appState.activeToolboxId === 'reclamation') {
        var tbRaTheme = $('toolbox-ra-theme');
        if (tbRaTheme && ra.theme != null) {
          tbRaTheme.value = ra.theme;
        }
        var tbRaMode = $('toolbox-ra-mode');
        if (tbRaMode && (ra.mode_label != null || ra.mode != null)) {
          tbRaMode.value = ra.mode_label != null ? ra.mode_label : ra.mode;
        }
        var tbRaClear = $('toolbox-ra-clear-store');
        if (tbRaClear && ra.clear_store != null) {
          tbRaClear.checked = Boolean(ra.clear_store);
        }
      }
    }
  }

  function updateAdbUI(adb) {
    var dotAdb = $('dot-adb');
    var textAdbStatus = $('text-adb-status');
    if (!adb) return;

    if (adb.connected) {
      if (dotAdb) dotAdb.className = 'status-dot success';
      var resText = adb.width && adb.height ? ' (' + adb.width + 'x' + adb.height + ')' : '';
      if (textAdbStatus) textAdbStatus.textContent = 'ADB 已连接 ' + (adb.adb_address || '') + resText;
    } else {
      if (dotAdb) dotAdb.className = 'status-dot danger';
      if (textAdbStatus) textAdbStatus.textContent = 'ADB 未连接 (' + (adb.adb_address || '127.0.0.1:16384') + ')';
    }
  }

  function updateTaskStateUI(state) {
    if (!state) return;
    appState.taskState = state;

    var statusBox = $('task-status-box');
    var dotTask = $('dot-task');
    var textTask = $('text-task-status');
    var progressBar = $('task-progress-bar');
    var progressFill = $('task-progress-fill');
    var btnStop = $('btn-global-stop');
    var btnToggleRaBtn = $('btn-toggle-reclamation');
    var btnToggleRa = $('btn-toggle-ra-text');
    var btnCheckUpdate = $('btn-check-update');
    var isRunning = Boolean(state.running);
    var mutexDisabled = typeof state.task_buttons_disabled === 'boolean' ? state.task_buttons_disabled : isRunning;

    if (isRunning) {
      if (statusBox) statusBox.classList.remove('idle');
      if (dotTask) dotTask.className = 'status-dot success';
      if (textTask) textTask.textContent = state.status_text || '任务执行中...';
      if (btnStop) btnStop.disabled = false;

      if (progressBar) {
        progressBar.classList.remove('hidden');
        if (state.progress_mode === 'determinate' && state.progress_total > 0) {
          progressBar.classList.remove('indeterminate');
          var pct = Math.min(100, Math.round((state.progress_cur / state.progress_total) * 100));
          if (progressFill) progressFill.style.width = pct + '%';
        } else {
          progressBar.classList.add('indeterminate');
        }
      }
    } else {
      if (statusBox) statusBox.classList.add('idle');
      if (textTask) textTask.textContent = state.status_text || '就绪';
      if (btnStop) btnStop.disabled = true;
      if (progressBar) progressBar.classList.add('hidden');
    }

    // Unified running-state lock for all mutually exclusive launch buttons across all pages
    var lockTitle = mutexDisabled ? '当前已有任务运行中，请先等待完成或点击「停止任务」' : '';
    for (var i = 0; i < MUTEX_TASK_BUTTON_IDS.length; i++) {
      var mBtn = $(MUTEX_TASK_BUTTON_IDS[i]);
      if (mBtn) {
        mBtn.disabled = mutexDisabled;
        mBtn.title = lockTitle;
      }
    }
    if (btnCheckUpdate) {
      if (mutexDisabled) {
        btnCheckUpdate.classList.add('is-disabled');
        btnCheckUpdate.title = lockTitle;
      } else {
        btnCheckUpdate.classList.remove('is-disabled');
        btnCheckUpdate.title = '';
      }
    }

    if (state.ra_button_text && btnToggleRa) {
      btnToggleRa.textContent = state.ra_button_text;
    }
    if (typeof state.ra_button_enabled === 'boolean' && btnToggleRaBtn) {
      btnToggleRaBtn.disabled = !state.ra_button_enabled;
      btnToggleRaBtn.title = !state.ra_button_enabled ? lockTitle : '';
    }

    if (typeof updateToolboxButtons === 'function') {
      updateToolboxButtons(findToolboxItem(appState.activeToolboxId));
    }
  }

  function updateSegmentedUI(containerId, activeValue) {
    var container = $(containerId);
    if (!container) return;
    if (containerId === 'seg-server') {
      if (activeValue === '官服') activeValue = 'Official';
      else if (activeValue === 'B服') activeValue = 'Bilibili';
    } else if (containerId === 'seg-post-action') {
      if (activeValue === '无') activeValue = 'none';
      else if (activeValue === '关闭模拟器') activeValue = 'close_emulator';
    }
    var btns = container.querySelectorAll('.segmented-btn');
    for (var i = 0; i < btns.length; i++) {
      if (btns[i].getAttribute('data-value') === activeValue) {
        btns[i].classList.add('active');
      } else {
        btns[i].classList.remove('active');
      }
    }
  }

  // Log Batch & Monospace Console Management
  function appendLogs(logBatch) {
    if (!Array.isArray(logBatch) || logBatch.length === 0) return;

    for (var i = 0; i < logBatch.length; i++) {
      appState.logs.push(logBatch[i]);
    }

    if (appState.logs.length > appState.logRingBufferSize) {
      appState.logs = appState.logs.slice(appState.logs.length - appState.logRingBufferSize);
    }

    var latest = appState.logs[appState.logs.length - 1];
    if (latest) {
      var drawerLatestText = $('drawer-latest-text');
      if (drawerLatestText) {
        var tagClass = getTagClass(latest.tag);
        drawerLatestText.innerHTML = '<span class="log-tag ' + tagClass + '">[' + escapeHtml(latest.tag || '系统') + ']</span> ' +
          '<span>' + escapeHtml(latest.text || '') + '</span>';
      }
    }

    if (appState.drawerExpanded) {
      renderDrawerPreviewLogs();
    }

    if (appState.activePage === 'page-terminal') {
      renderTerminalLogs();
    }
  }

  function getTagClass(tag) {
    switch (tag) {
      case 'growth': return 'tag-growth';
      case 'agent': return 'tag-agent';
      case 'tutorial': return 'tag-tutorial';
      case 'roguelike': return 'tag-roguelike';
      case 'reclamation': return 'tag-reclamation';
      case 'toolbox': return 'tag-toolbox';
      case 'story': return 'tag-story';
      case 'report': return 'tag-report';
      case 'alert': return 'tag-alert';
      case 'error': return 'tag-error';
      default: return 'tag-normal';
    }
  }

  function matchesLogFilter(item, filterTag) {
    if (filterTag === 'all') return true;
    if (filterTag === 'alert') {
      return item.tag === 'alert' || item.tag === 'error';
    }
    return item.tag === filterTag;
  }

  function renderTerminalLogs() {
    var body = $('terminal-body');
    var countLbl = $('terminal-line-count');
    if (!body) return;

    var filterTag = appState.logFilterTag;
    var query = (appState.logSearchQuery || '').toLowerCase();

    var filtered = appState.logs.filter(function (item) {
      if (!matchesLogFilter(item, filterTag)) return false;
      if (query && (item.text || '').toLowerCase().indexOf(query) === -1) return false;
      return true;
    });

    if (countLbl) {
      countLbl.textContent = filtered.length + ' 行 / 共 ' + appState.logs.length + ' 行';
    }

    var html = '';
    for (var i = 0; i < filtered.length; i++) {
      var item = filtered[i];
      var tagClass = getTagClass(item.tag);
      html += '<div class="log-row">' +
        '<span class="log-ts">' + escapeHtml(item.ts || '') + '</span> ' +
        '<span class="log-tag ' + tagClass + '">[' + escapeHtml(item.tag || '日志') + ']</span> ' +
        '<span class="log-text">' + escapeHtml(item.text || '') + '</span>' +
        '</div>';
    }
    body.innerHTML = html;
    body.scrollTop = body.scrollHeight;
  }

  function renderDrawerPreviewLogs() {
    var content = $('drawer-content-preview');
    if (!content) return;

    var slice = appState.logs.slice(-10);
    var html = '';
    for (var i = 0; i < slice.length; i++) {
      var item = slice[i];
      var tagClass = getTagClass(item.tag);
      html += '<div class="log-row">' +
        '<span class="log-ts">' + escapeHtml(item.ts || '') + '</span> ' +
        '<span class="log-tag ' + tagClass + '">[' + escapeHtml(item.tag || '') + ']</span> ' +
        '<span class="log-text">' + escapeHtml(item.text || '') + '</span>' +
        '</div>';
    }
    content.innerHTML = html;
    content.scrollTop = content.scrollHeight;
  }

  function escapeHtml(str) {
    if (typeof str !== 'string') return String(str || '');
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Settings Modal Binding (All 57 fields)
  function loadSettingsIntoModal() {
    PrtsBridge.getSettings().then(function (cfg) {
      if (!cfg) return;

      var d = cfg.daily || (cfg.config && cfg.config.daily) || {};
      var g = cfg.growth || (cfg.config && cfg.config.growth) || {};
      var stock = g.material_safety_stock || (cfg.config && cfg.config.growth && cfg.config.growth.material_safety_stock) || {};
      var rl = cfg.roguelike || (cfg.config && cfg.config.roguelike) || {};
      var ra = cfg.reclamation || (cfg.config && cfg.config.reclamation) || {};
      var ai = cfg.ai || (cfg.config && cfg.config.ai) || {};
      var emu = cfg.emulator || (cfg.config && cfg.config.emulator) || {};
      var notif = cfg.notifications || (cfg.config && cfg.config.notifications) || {};

      // Populate dropdowns from cfg.options || appState.options BEFORE assigning selected values
      var opts = cfg.options || appState.options || {};
      var rlThemeVal = rl.theme !== undefined ? rl.theme : (cfg.rl_theme !== undefined ? cfg.rl_theme : '');
      var rlModeVal = rl.mode !== undefined ? rl.mode : (cfg.rl_mode !== undefined ? cfg.rl_mode : '');
      var raThemeVal = ra.theme !== undefined ? ra.theme : (cfg.ra_theme !== undefined ? cfg.ra_theme : '');
      var raModeVal = ra.mode !== undefined ? ra.mode : (cfg.ra_mode !== undefined ? cfg.ra_mode : '');

      populateDropdown('set-rl-theme', opts.rl_themes, rlThemeVal);
      populateDropdown('set-rl-mode', opts.rl_targets, rlModeVal);
      populateDropdown('set-ra-theme', opts.ra_themes, raThemeVal);
      populateDropdown('set-ra-mode', opts.ra_modes, raModeVal);

      // Tab 1: 刷理智与养成
      var fightMode = d.fight_mode || cfg.fight_mode || 'AUTO';
      updateSegmentedUI('set-fight-mode', fightMode);
      if ($('set-override-stage')) $('set-override-stage').value = d.override_stage !== undefined ? d.override_stage : (cfg.override_stage || '');
      if ($('set-medicine')) $('set-medicine').value = d.medicine != null ? d.medicine : (cfg.medicine != null ? cfg.medicine : 0);
      if ($('set-stone')) $('set-stone').value = d.stone != null ? d.stone : (cfg.stone != null ? cfg.stone : 0);
      if ($('set-max-push-rounds')) $('set-max-push-rounds').value = d.max_push_rounds != null ? d.max_push_rounds : (cfg.max_push_rounds != null ? cfg.max_push_rounds : 5);

      var targetOps = g.target_operators !== undefined ? g.target_operators : cfg.target_operators;
      if ($('set-target-ops')) $('set-target-ops').value = Array.isArray(targetOps) ? targetOps.join(', ') : (targetOps || '');
      if ($('set-t3-stock')) $('set-t3-stock').value = stock.T3_DEFAULT != null ? stock.T3_DEFAULT : (cfg.t3_stock != null ? cfg.t3_stock : 30);

      if ($('set-sw-auto-push')) $('set-sw-auto-push').checked = d.auto_push != null ? Boolean(d.auto_push) : Boolean(cfg.auto_push);
      if ($('set-sw-prefer-event')) $('set-sw-prefer-event').checked = g.prefer_event != null ? (g.prefer_event !== false) : (cfg.prefer_event !== false);
      if ($('set-sw-allow-chip')) $('set-sw-allow-chip').checked = g.allow_chip_stages != null ? (g.allow_chip_stages !== false) : (cfg.allow_chip_stages !== false);
      if ($('set-sw-allow-res')) $('set-sw-allow-res').checked = g.allow_resource_stages != null ? (g.allow_resource_stages !== false) : (cfg.allow_resource_stages !== false);

      // Tab 2: 公招高资与日常
      var preserve = d.recruit_preserve_tags || cfg.recruit_preserve_tags || [];
      if ($('set-cb-high-senior')) $('set-cb-high-senior').checked = preserve.indexOf('高级资深干员') !== -1;
      if ($('set-cb-senior')) $('set-cb-senior').checked = preserve.indexOf('资深干员') !== -1;
      if ($('set-cb-robot')) $('set-cb-robot').checked = preserve.indexOf('支援机械') !== -1;

      if ($('set-rec-times')) $('set-rec-times').value = d.recruit_times != null ? d.recruit_times : (cfg.recruit_times != null ? cfg.recruit_times : 4);
      if ($('set-penguin-id')) $('set-penguin-id').value = d.penguin_id !== undefined ? d.penguin_id : (cfg.penguin_id || '');
      if ($('set-sw-penguin')) $('set-sw-penguin').checked = d.report_to_penguin != null ? Boolean(d.report_to_penguin) : Boolean(cfg.report_to_penguin);
      if ($('set-sw-yituliu')) $('set-sw-yituliu').checked = d.report_to_yituliu != null ? Boolean(d.report_to_yituliu) : Boolean(cfg.report_to_yituliu);

      var skipTasks = d.skip_tasks || cfg.skip_tasks || [];
      var skipCheckboxes = $$('#set-skip-tasks-group input[type="checkbox"]');
      for (var s = 0; s < skipCheckboxes.length; s++) {
        skipCheckboxes[s].checked = skipTasks.indexOf(skipCheckboxes[s].value) !== -1;
      }

      // Tab 3: 肉鸽与生息演算
      var rlUseGui = rl.use_gui_config != null ? (rl.use_gui_config !== false) : (cfg.use_gui_config !== false);
      if ($('set-sw-use-gui-cfg')) $('set-sw-use-gui-cfg').checked = rlUseGui;
      if ($('set-rl-squad')) $('set-rl-squad').value = rl.squad !== undefined ? rl.squad : (cfg.rl_squad !== undefined ? cfg.rl_squad : '');
      if ($('set-rl-roles')) $('set-rl-roles').value = rl.roles !== undefined ? rl.roles : (cfg.rl_roles !== undefined ? cfg.rl_roles : '');
      if ($('set-rl-core-char')) $('set-rl-core-char').value = rl.core_char !== undefined ? rl.core_char : (cfg.rl_core_char !== undefined ? cfg.rl_core_char : '');
      if ($('set-rl-starts')) $('set-rl-starts').value = rl.starts_count != null ? rl.starts_count : (cfg.rl_starts_count != null ? cfg.rl_starts_count : '');

      if ($('set-sw-rl-ai')) $('set-sw-rl-ai').checked = rl.ai_copilot_enabled != null ? Boolean(rl.ai_copilot_enabled) : Boolean(cfg.rl_ai_copilot);
      if ($('set-sw-rl-timeout-fallback')) $('set-sw-rl-timeout-fallback').checked = rl.ai_auto_fallback_on_timeout != null ? (rl.ai_auto_fallback_on_timeout !== false) : (cfg.rl_timeout_fallback !== false);
      if ($('set-rl-timeout')) $('set-rl-timeout').value = rl.ai_decision_timeout_sec != null ? rl.ai_decision_timeout_sec : (cfg.rl_timeout_sec != null ? cfg.rl_timeout_sec : 45);
      if ($('set-sw-rl-boss-prts')) $('set-sw-rl-boss-prts').checked = rl.boss_prts_assist != null ? (rl.boss_prts_assist !== false) : (cfg.rl_boss_prts !== false);
      if ($('set-sw-rl-claim')) $('set-sw-rl-claim').checked = rl.auto_claim_rewards != null ? (rl.auto_claim_rewards !== false) : (cfg.rl_claim_rewards !== false);
      if ($('set-sw-rl-tech')) $('set-sw-rl-tech').checked = rl.auto_upgrade_tech_tree != null ? (rl.auto_upgrade_tech_tree !== false) : (cfg.rl_upgrade_tech !== false);

      var raTools = ra.tools_to_craft !== undefined ? ra.tools_to_craft : cfg.ra_tools;
      if ($('set-ra-tools')) $('set-ra-tools').value = Array.isArray(raTools) ? raTools.join(', ') : (raTools || '');
      if ($('set-sw-ra-clear-store')) $('set-sw-ra-clear-store').checked = ra.clear_store != null ? Boolean(ra.clear_store) : Boolean(cfg.ra_clear_store);

      // Tab 4: AI 协同与内核
      var curMode = ai.mode || cfg.ai_mode || 'agent_collab';
      var radios = $$('input[name="set-ai-mode"]');
      for (var r = 0; r < radios.length; r++) {
        radios[r].checked = radios[r].value === curMode;
      }
      if ($('set-base-url')) $('set-base-url').value = ai.base_url || cfg.base_url || 'https://api.openai.com/v1';
      if ($('set-api-key')) $('set-api-key').value = ai.api_key !== undefined ? ai.api_key : (cfg.api_key || '');
      if ($('set-model-name')) $('set-model-name').value = ai.model_name || cfg.model_name || 'gpt-4o-mini';
      if ($('set-timeout-sec')) $('set-timeout-sec').value = ai.timeout_sec != null ? ai.timeout_sec : (cfg.timeout_sec != null ? cfg.timeout_sec : 30);
      if ($('set-maa-path')) $('set-maa-path').value = cfg.maa_path || '';
      if ($('set-adb-address')) $('set-adb-address').value = emu.adb_address || cfg.adb_address || '127.0.0.1:16384';

      // Tab 5: Webhook 通知
      if ($('set-sw-webhook')) $('set-sw-webhook').checked = notif.webhook_enabled != null ? Boolean(notif.webhook_enabled) : Boolean(cfg.webhook_enabled);
      if ($('set-webhook-url')) $('set-webhook-url').value = notif.webhook_url !== undefined ? notif.webhook_url : (cfg.webhook_url || '');
      if ($('set-cb-wh-daily')) $('set-cb-wh-daily').checked = notif.notify_on_daily_finish != null ? (notif.notify_on_daily_finish !== false) : (cfg.notify_on_daily_finish !== false);
      if ($('set-cb-wh-recruit')) $('set-cb-wh-recruit').checked = notif.notify_on_recruit_alert != null ? (notif.notify_on_recruit_alert !== false) : (cfg.notify_on_recruit_alert !== false);
      if ($('set-cb-wh-agent')) $('set-cb-wh-agent').checked = notif.notify_on_agent_task != null ? (notif.notify_on_agent_task !== false) : (cfg.notify_on_agent_task !== false);
    }).catch(handleBridgeError('读取设置失败'));
  }

  function populateDropdown(selectId, items, selectedValue) {
    var select = $(selectId);
    if (!select || !Array.isArray(items)) return;

    select.innerHTML = '';
    var matched = false;
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var opt = document.createElement('option');
      opt.value = item.value;
      opt.textContent = item.label;
      if (selectedValue != null && String(item.value) === String(selectedValue)) {
        opt.selected = true;
        matched = true;
      }
      select.appendChild(opt);
    }
    if (selectedValue != null && !matched && selectedValue !== '') {
      var customOpt = document.createElement('option');
      customOpt.value = selectedValue;
      customOpt.textContent = selectedValue;
      customOpt.selected = true;
      select.appendChild(customOpt);
    }
    if (selectedValue != null) {
      select.value = selectedValue;
    }
  }

  function collectAndSaveSettings() {
    var activeFightMode = 'AUTO';
    var fightModeBtn = document.querySelector('#set-fight-mode .segmented-btn.active');
    if (fightModeBtn) {
      activeFightMode = fightModeBtn.getAttribute('data-value') || 'AUTO';
    }

    var targets = ($('set-target-ops') ? $('set-target-ops').value : '')
      .replace(/，/g, ',')
      .split(',')
      .map(function (s) { return s.trim(); })
      .filter(Boolean);

    var preserveTags = [];
    if ($('set-cb-high-senior') && $('set-cb-high-senior').checked) preserveTags.push('高级资深干员');
    if ($('set-cb-senior') && $('set-cb-senior').checked) preserveTags.push('资深干员');
    if ($('set-cb-robot') && $('set-cb-robot').checked) preserveTags.push('支援机械');

    var skipTasks = [];
    var skipCheckboxes = $$('#set-skip-tasks-group input[type="checkbox"]:checked');
    for (var s = 0; s < skipCheckboxes.length; s++) {
      skipTasks.push(skipCheckboxes[s].value);
    }

    var aiMode = 'agent_collab';
    var checkedRadio = document.querySelector('input[name="set-ai-mode"]:checked');
    if (checkedRadio) aiMode = checkedRadio.value;

    var raTools = ($('set-ra-tools') ? $('set-ra-tools').value : '')
      .replace(/，/g, ',')
      .split(',')
      .map(function (t) { return t.trim(); })
      .filter(Boolean);

    var payload = {
      emulator: {
        adb_address: ($('set-adb-address') ? $('set-adb-address').value : '').trim() || '127.0.0.1:16384'
      },
      ai: {
        mode: aiMode,
        enabled: aiMode === 'builtin_api',
        base_url: ($('set-base-url') ? $('set-base-url').value : '').trim() || 'https://api.openai.com/v1',
        api_key: ($('set-api-key') ? $('set-api-key').value : '').trim(),
        model_name: ($('set-model-name') ? $('set-model-name').value : '').trim() || 'gpt-4o-mini',
        timeout_sec: parseInt($('set-timeout-sec') ? $('set-timeout-sec').value : '30', 10) || 30
      },
      daily: {
        fight_mode: activeFightMode,
        override_stage: ($('set-override-stage') ? $('set-override-stage').value : '').trim(),
        medicine: parseInt($('set-medicine') ? $('set-medicine').value : '0', 10) || 0,
        stone: parseInt($('set-stone') ? $('set-stone').value : '0', 10) || 0,
        max_push_rounds: parseInt($('set-max-push-rounds') ? $('set-max-push-rounds').value : '5', 10) || 5,
        auto_push: $('set-sw-auto-push') ? $('set-sw-auto-push').checked : false,
        recruit_preserve_tags: preserveTags,
        recruit_times: parseInt($('set-rec-times') ? $('set-rec-times').value : '4', 10) || 4,
        penguin_id: ($('set-penguin-id') ? $('set-penguin-id').value : '').trim(),
        report_to_penguin: $('set-sw-penguin') ? $('set-sw-penguin').checked : false,
        report_to_yituliu: $('set-sw-yituliu') ? $('set-sw-yituliu').checked : false,
        skip_tasks: skipTasks
      },
      growth: {
        prefer_event: $('set-sw-prefer-event') ? $('set-sw-prefer-event').checked : true,
        allow_chip_stages: $('set-sw-allow-chip') ? $('set-sw-allow-chip').checked : true,
        allow_resource_stages: $('set-sw-allow-res') ? $('set-sw-allow-res').checked : true,
        target_operators: targets,
        material_safety_stock: {
          T3_DEFAULT: parseInt($('set-t3-stock') ? $('set-t3-stock').value : '30', 10) || 30
        }
      },
      roguelike: {
        use_gui_config: $('set-sw-use-gui-cfg') ? $('set-sw-use-gui-cfg').checked : true,
        theme: $('set-rl-theme') ? $('set-rl-theme').value : '',
        mode: $('set-rl-mode') ? $('set-rl-mode').value : '',
        squad: ($('set-rl-squad') ? $('set-rl-squad').value : '').trim(),
        roles: ($('set-rl-roles') ? $('set-rl-roles').value : '').trim(),
        core_char: ($('set-rl-core-char') ? $('set-rl-core-char').value : '').trim(),
        starts_count: $('set-rl-starts') && $('set-rl-starts').value.trim() ? parseInt($('set-rl-starts').value.trim(), 10) : null,
        ai_copilot_enabled: $('set-sw-rl-ai') ? $('set-sw-rl-ai').checked : false,
        ai_auto_fallback_on_timeout: $('set-sw-rl-timeout-fallback') ? $('set-sw-rl-timeout-fallback').checked : true,
        ai_decision_timeout_sec: parseFloat($('set-rl-timeout') ? $('set-rl-timeout').value : '45') || 45.0,
        boss_prts_assist: $('set-sw-rl-boss-prts') ? $('set-sw-rl-boss-prts').checked : true,
        auto_claim_rewards: $('set-sw-rl-claim') ? $('set-sw-rl-claim').checked : true,
        auto_upgrade_tech_tree: $('set-sw-rl-tech') ? $('set-sw-rl-tech').checked : true
      },
      reclamation: {
        use_gui_config: $('set-sw-use-gui-cfg') ? $('set-sw-use-gui-cfg').checked : true,
        theme: $('set-ra-theme') ? $('set-ra-theme').value : '',
        mode: $('set-ra-mode') ? $('set-ra-mode').value : '',
        tools_to_craft: raTools,
        clear_store: $('set-sw-ra-clear-store') ? $('set-sw-ra-clear-store').checked : false
      },
      notifications: {
        webhook_enabled: $('set-sw-webhook') ? $('set-sw-webhook').checked : false,
        webhook_url: ($('set-webhook-url') ? $('set-webhook-url').value : '').trim(),
        notify_on_daily_finish: $('set-cb-wh-daily') ? $('set-cb-wh-daily').checked : true,
        notify_on_recruit_alert: $('set-cb-wh-recruit') ? $('set-cb-wh-recruit').checked : true,
        notify_on_agent_task: $('set-cb-wh-agent') ? $('set-cb-wh-agent').checked : true
      },
      maa_path: ($('set-maa-path') ? $('set-maa-path').value : '').trim()
    };

    PrtsBridge.saveSettings(payload).then(function (res) {
      if (res && res.ok) {
        showToast('success', '保存成功', '设置已保存至 launcher_config.json');
        if (res.snapshot) {
          updateSnapshotUI(res.snapshot);
        }
        closeModal('modal-settings');
      } else {
        showToast('error', '保存失败', (res && res.error) || '未知错误');
      }
    }).catch(handleBridgeError('保存设置失败'));
  }

  // Agent Prompts Dialog
  function loadAgentPromptsIntoModal() {
    PrtsBridge.getAgentPrompts().then(function (data) {
      if (!data) return;
      if ($('text-prompt-init')) $('text-prompt-init').value = data.init_prompt || data.prompt_init || '';
      if ($('text-prompt-repair')) $('text-prompt-repair').value = data.repair_prompt || data.prompt_repair || '';
      if ($('text-readme-content')) $('text-readme-content').textContent = data.readme_content || '';
    }).catch(handleBridgeError('获取协同提示词失败'));
  }

  // Wizard Stepper Manager
  function setWizardStep(step) {
    appState.wizardStep = step;
    var panes = $$('.wizard-step-pane');
    for (var i = 0; i < panes.length; i++) {
      if (panes[i].style.display) {
        panes[i].style.display = '';
      }
      if (i + 1 === step) {
        panes[i].classList.add('active');
      } else {
        panes[i].classList.remove('active');
      }
    }

    var b1 = $('wizard-badge-1');
    var b2 = $('wizard-badge-2');
    var b3 = $('wizard-badge-3');

    if (b1) b1.className = 'step-badge ' + (step === 1 ? 'active' : (step > 1 ? 'done' : ''));
    if (b2) b2.className = 'step-badge ' + (step === 2 ? 'active' : (step > 2 ? 'done' : ''));
    if (b3) b3.className = 'step-badge ' + (step === 3 ? 'active' : '');

    var btnPrev = $('wizard-btn-prev');
    var btnNext = $('wizard-btn-next');
    if (btnPrev) btnPrev.disabled = (step === 1);
    if (btnNext) {
      btnNext.textContent = (step === 3) ? '完成并进入主面板' : '下一步';
    }
  }

  function syncWizardAiCards() {
    var checked = document.querySelector('input[name="wizard-ai-mode"]:checked');
    var mode = checked ? checked.value : 'agent_collab';
    var cardAgent = $('wizard-card-agent');
    var cardApi = $('wizard-card-api');

    if (cardAgent) {
      if (mode === 'agent_collab') {
        cardAgent.classList.add('card-highlight');
        cardAgent.style.background = '';
      } else {
        cardAgent.classList.remove('card-highlight');
        cardAgent.style.background = 'var(--md-sys-color-surface-container-lowest)';
      }
    }
    if (cardApi) {
      if (mode === 'builtin_api') {
        cardApi.classList.add('card-highlight');
        cardApi.style.background = '';
      } else {
        cardApi.classList.remove('card-highlight');
        cardApi.style.background = 'var(--md-sys-color-surface-container-lowest)';
      }
    }
  }

  function openWizardModal(forced) {
    appState.wizardForced = Boolean(forced);
    var closeBtn = $('wizard-btn-close');
    if (closeBtn) {
      if (appState.wizardForced) {
        closeBtn.classList.add('hidden');
      } else {
        closeBtn.classList.remove('hidden');
      }
    }
    setWizardStep(1);
    syncWizardAiCards();
    openModal('modal-wizard');
    PrtsBridge.wizardScanMaa().catch(function () {});
  }

  function setupWizard() {
    var btnPrev = $('wizard-btn-prev');
    if (btnPrev) {
      btnPrev.addEventListener('click', function () {
        if (appState.wizardStep > 1) {
          setWizardStep(appState.wizardStep - 1);
        }
      });
    }

    var btnNext = $('wizard-btn-next');
    if (btnNext) {
      btnNext.addEventListener('click', function () {
        if (appState.wizardStep < 3) {
          setWizardStep(appState.wizardStep + 1);
        } else {
          var aiMode = 'agent_collab';
          var radio = document.querySelector('input[name="wizard-ai-mode"]:checked');
          if (radio) aiMode = radio.value;

          var payload = {
            ai: {
              mode: aiMode,
              enabled: aiMode === 'builtin_api',
              base_url: ($('wizard-base-url') ? $('wizard-base-url').value : '').trim() || 'https://api.openai.com/v1',
              api_key: ($('wizard-api-key') ? $('wizard-api-key').value : '').trim(),
              model_name: ($('wizard-model-name') ? $('wizard-model-name').value : '').trim() || 'gpt-4o-mini'
            },
            emulator: {
              type: $('wizard-s2-emu-type') ? $('wizard-s2-emu-type').value : 'auto',
              adb_address: ($('wizard-s2-adb-address') ? $('wizard-s2-adb-address').value : '').trim() || '127.0.0.1:16384'
            }
          };

          PrtsBridge.wizardFinish(payload).then(function (res) {
            if (res && res.ok) {
              appState.wizardForced = false;
              showToast('success', '向导完成', '配置已保存');
              closeModal('modal-wizard');
            }
          }).catch(handleBridgeError('保存向导配置失败'));
        }
      });
    }

    // Step 1 buttons
    var btnBrowseMaa = $('wizard-btn-browse-maa');
    if (btnBrowseMaa) {
      btnBrowseMaa.addEventListener('click', function () {
        PrtsBridge.browseMaaFolder().then(function (res) {
          var chosen = res ? (res.path || res.folder || '') : '';
          if (chosen) {
            PrtsBridge.wizardBindMaa(chosen).catch(handleBridgeError('绑定 MAA 目录失败'));
          }
        }).catch(handleBridgeError('打开目录选择失败'));
      });
    }

    var btnDlMaa = $('wizard-btn-download-maa');
    if (btnDlMaa) {
      btnDlMaa.addEventListener('click', function () {
        var dlBox = $('wizard-dl-box');
        if (dlBox) dlBox.classList.remove('hidden');
        PrtsBridge.wizardStartDownloadMaa().catch(handleBridgeError('启动下载失败'));
      });
    }

    var btnCancelDl = $('wizard-btn-cancel-dl');
    if (btnCancelDl) {
      btnCancelDl.addEventListener('click', function () {
        PrtsBridge.wizardCancelDownloadMaa().catch(handleBridgeError('取消下载失败'));
      });
    }

    var btnRescanMaa = $('wizard-btn-rescan-maa');
    if (btnRescanMaa) {
      btnRescanMaa.addEventListener('click', function () {
        PrtsBridge.wizardScanMaa().catch(handleBridgeError('扫描 MAA 失败'));
      });
    }

    // Step 2 buttons
    var btnProbeS2 = $('wizard-btn-probe-s2');
    if (btnProbeS2) {
      btnProbeS2.addEventListener('click', function () {
        var emuType = $('wizard-s2-emu-type') ? $('wizard-s2-emu-type').value : 'auto';
        var addr = $('wizard-s2-adb-address') ? $('wizard-s2-adb-address').value : '127.0.0.1:16384';
        PrtsBridge.wizardRunDiagnostics(emuType, addr).catch(handleBridgeError('环境体检失败'));
      });
    }

    var btnLaunchS2 = $('wizard-btn-launch-s2');
    if (btnLaunchS2) {
      btnLaunchS2.addEventListener('click', function () {
        var emuType = $('wizard-s2-emu-type') ? $('wizard-s2-emu-type').value : 'auto';
        var addr = $('wizard-s2-adb-address') ? $('wizard-s2-adb-address').value : '127.0.0.1:16384';
        PrtsBridge.wizardLaunchEmulator(emuType, addr).catch(handleBridgeError('拉起模拟器失败'));
      });
    }

    // Step 3 Card Selection & Presets
    var cardAgent = $('wizard-card-agent');
    var cardApi = $('wizard-card-api');
    var wizardRadios = $$('input[name="wizard-ai-mode"]');

    if (cardAgent) {
      cardAgent.addEventListener('click', function () {
        var r = document.querySelector('input[name="wizard-ai-mode"][value="agent_collab"]');
        if (r) r.checked = true;
        syncWizardAiCards();
      });
    }
    if (cardApi) {
      cardApi.addEventListener('click', function () {
        var r = document.querySelector('input[name="wizard-ai-mode"][value="builtin_api"]');
        if (r) r.checked = true;
        syncWizardAiCards();
      });
    }
    for (var wr = 0; wr < wizardRadios.length; wr++) {
      wizardRadios[wr].addEventListener('change', syncWizardAiCards);
    }

    function applyPreset(url, model, key) {
      var r = document.querySelector('input[name="wizard-ai-mode"][value="builtin_api"]');
      if (r) r.checked = true;
      syncWizardAiCards();
      if ($('wizard-base-url')) $('wizard-base-url').value = url;
      if ($('wizard-model-name')) $('wizard-model-name').value = model;
      if (key && $('wizard-api-key') && !$('wizard-api-key').value) {
        $('wizard-api-key').value = key;
      }
    }

    if ($('preset-deepseek')) $('preset-deepseek').addEventListener('click', function () { applyPreset('https://api.deepseek.com/v1', 'deepseek-chat'); });
    if ($('preset-gemini')) $('preset-gemini').addEventListener('click', function () { applyPreset('https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-2.5-flash'); });
    if ($('preset-ollama')) $('preset-ollama').addEventListener('click', function () { applyPreset('http://127.0.0.1:11434/v1', 'qwen2.5:14b', 'ollama'); });
    if ($('preset-openai')) $('preset-openai').addEventListener('click', function () { applyPreset('https://api.openai.com/v1', 'gpt-4o-mini'); });

    var btnTestApi = $('wizard-btn-test-api');
    if (btnTestApi) {
      btnTestApi.addEventListener('click', function () {
        var u = $('wizard-base-url') ? $('wizard-base-url').value : '';
        var k = $('wizard-api-key') ? $('wizard-api-key').value : '';
        var m = $('wizard-model-name') ? $('wizard-model-name').value : '';
        btnTestApi.disabled = true;
        btnTestApi.textContent = '测试中...';
        PrtsBridge.testAiApi(u, k, m).then(function (res) {
          btnTestApi.disabled = false;
          btnTestApi.textContent = '测试连接';
          if (res && res.ok) {
            showToast('success', '连接成功', '模型接口响应正常');
          } else {
            var errMsg = (res && (res.message || res.error)) || '请求失败';
            showToast('error', '连接失败', errMsg);
          }
        }).catch(function (err) {
          btnTestApi.disabled = false;
          btnTestApi.textContent = '测试连接';
          handleBridgeError('测试连接失败')(err);
        });
      });
    }
  }

  // Push Event Subscriptions Setup
  function setupPushListeners() {
    PrtsBridge.on('snapshot', function (payload) {
      updateSnapshotUI(payload);
    });

    PrtsBridge.on('adb_status', function (payload) {
      updateAdbUI(payload);
      if (appState.pendingAdbProbe && payload) {
        appState.pendingAdbProbe = false;
        if (payload.connected) {
          showToast('success', 'ADB 探测成功', '已连接: ' + (payload.adb_address || ''));
        } else {
          showToast('warn', 'ADB 探测', payload.warning || '未检测到活跃模拟器端口，请确认模拟器已开启 ADB');
        }
      }
    });

    PrtsBridge.on('task_state', function (payload) {
      updateTaskStateUI(payload);
    });

    PrtsBridge.on('log_batch', function (payload) {
      appendLogs(payload);
    });

    PrtsBridge.on('toast', function (payload) {
      if (payload) {
        showToast(payload.level || 'info', payload.title || '', payload.message || '');
      }
    });

    PrtsBridge.on('agent_banner', applyAgentBanner);
    PrtsBridge.on('tutorial_banner', applyTutorialBanner);

    // MAA Update Events
    PrtsBridge.on('maa_update_check_done', function (payload) {
      if (!payload) return;
      if (payload.ok === false) {
        showToast('error', '检查更新失败', payload.error || '检查更新失败，请检查网络连接');
        return;
      }
      if (payload.has_update) {
        appState.updateReleaseInfo = payload.release_info;
        appState.updateLocalVer = payload.local_version || '未知';
        appState.updateRemoteVer = payload.remote_version || '未知';

        if ($('update-local-ver')) $('update-local-ver').textContent = appState.updateLocalVer;
        if ($('update-remote-ver')) $('update-remote-ver').textContent = appState.updateRemoteVer;
        if ($('update-size')) $('update-size').textContent = (payload.total_size_mb || '--') + ' MB';
        if ($('update-release-notes') && payload.release_info) {
          $('update-release-notes').textContent = payload.release_info.body || payload.release_info.tag_name || '常规版本增量升级';
        }
        openModal('modal-maa-update');
      } else {
        showToast('info', '检查更新', '当前 MAA 内核已是最新版本 (' + (payload.local_version || '') + ')');
      }
    });

    PrtsBridge.on('maa_update_progress', function (payload) {
      var pBar = $('update-progress-bar');
      var pFill = $('update-progress-fill');
      var pMsg = $('update-status-msg');
      if (pBar) pBar.classList.remove('hidden');
      if (payload && typeof payload.ratio === 'number') {
        var pct = Math.round(payload.ratio * 100);
        if (pFill) pFill.style.width = pct + '%';
        if (pMsg) pMsg.textContent = (payload.msg || '更新中...') + ' (' + pct + '%)';
      }
    });

    PrtsBridge.on('maa_update_apply_done', function (payload) {
      closeModal('modal-maa-update');
      if (payload && payload.ok) {
        showToast('success', '更新完成', 'MAA 已升级至 ' + (payload.new_version || ''));
        PrtsBridge.refreshSnapshot(false).catch(function () {});
      } else {
        showToast('error', '更新失败', (payload && payload.error) || '未知错误');
      }
    });

    // Wizard Push Events
    PrtsBridge.on('open_wizard', function () {
      openWizardModal(true);
    });

    PrtsBridge.on('wizard_maa_scan', function (payload) {
      if (!payload) return;
      var dot = $('wizard-s1-dot');
      var statusLbl = $('wizard-s1-status');
      var detailLbl = $('wizard-s1-detail');
      appState.wizardMaaValid = Boolean(payload.valid);

      if (payload.valid) {
        if (dot) dot.className = 'status-dot success';
        if (statusLbl) statusLbl.textContent = 'MAA 内核校验通过 (' + (payload.maa_version || '') + ')';
        if (detailLbl) detailLbl.textContent = '路径：' + (payload.maa_dir || '');
      } else {
        if (dot) dot.className = 'status-dot danger';
        if (statusLbl) statusLbl.textContent = '未定位到有效 MAA 内核';
        if (detailLbl) detailLbl.textContent = payload.reason || '缺少 MaaCore.dll 或 resource 目录';
      }
    });

    PrtsBridge.on('wizard_dl_progress', function (payload) {
      if (!payload) return;
      var fill = $('wizard-dl-progress-fill');
      var text = $('wizard-dl-status-text');
      var bytes = $('wizard-dl-bytes');
      if (fill && typeof payload.ratio === 'number') {
        fill.style.width = Math.round(payload.ratio * 100) + '%';
      }
      if (text) text.textContent = payload.msg || '下载中...';
      if (bytes && payload.done != null && payload.total != null) {
        bytes.textContent = (payload.done / (1024 * 1024)).toFixed(1) + ' MB / ' + (payload.total / (1024 * 1024)).toFixed(1) + ' MB';
      }
    });

    PrtsBridge.on('wizard_dl_finished', function (payload) {
      var box = $('wizard-dl-box');
      if (payload && payload.ok) {
        showToast('success', '下载完成', 'MAA 自动安装校验通过');
        if (box) box.classList.add('hidden');
        PrtsBridge.wizardScanMaa().catch(function () {});
      } else {
        showToast('error', '下载失败', (payload && payload.error) || '下载未完成');
      }
    });

    PrtsBridge.on('wizard_diag_done', function (payload) {
      if (!payload) return;
      var p = payload.write_permission;
      var m = payload.maa_integrity;
      var a = payload.adb_resolution;

      if (p) {
        if ($('wizard-dot-perm')) $('wizard-dot-perm').className = 'status-dot ' + (p.ok ? 'success' : 'danger');
        if ($('wizard-desc-perm')) $('wizard-desc-perm').textContent = p.reason || (p.ok ? '目录有完整读写权限' : '权限受限');
      }
      if (m) {
        if ($('wizard-dot-maa')) $('wizard-dot-maa').className = 'status-dot ' + (m.ok ? 'success' : 'danger');
        if ($('wizard-desc-maa')) $('wizard-desc-maa').textContent = m.reason || (m.ok ? '核心文件完整' : '文件缺失');
      }
      if (a) {
        if ($('wizard-dot-adb')) $('wizard-dot-adb').className = 'status-dot ' + (a.connected && a.is_16_9 ? 'success' : 'warn');
        if ($('wizard-desc-adb')) $('wizard-desc-adb').textContent = a.reason || (a.connected ? (a.is_16_9 ? '连接正常 (16:9)' : '已连接但非 16:9') : '未检测到 ADB 连接');
      }
    });

    PrtsBridge.on('wizard_launch_done', function (payload) {
      if (payload && payload.ok) {
        showToast('success', '拉起完成', '模拟器启动并探测成功');
      } else {
        showToast('warn', '拉起提示', (payload && payload.error) || '请确认模拟器已手动启动');
      }
    });
  }

  // Setup Button Action Handlers
  function setupActions() {
    // Top Bar Actions
    var btnCheckUpdate = $('btn-check-update');
    if (btnCheckUpdate) {
      btnCheckUpdate.addEventListener('click', function () {
        PrtsBridge.checkMaaUpdate()
          .then(handleTaskStartResult('检查更新'))
          .catch(handleBridgeError('检查更新失败'));
      });
    }

    var btnProbeAdb = $('btn-probe-adb');
    if (btnProbeAdb) {
      btnProbeAdb.addEventListener('click', function () {
        appState.pendingAdbProbe = true;
        showToast('info', 'ADB 探测', '正在探测端口或拉起模拟器...');
        PrtsBridge.probeOrLaunchEmulator().catch(function (err) {
          appState.pendingAdbProbe = false;
          handleBridgeError('ADB 探测失败')(err);
        });
      });
    }

    var btnGlobalStop = $('btn-global-stop');
    if (btnGlobalStop) {
      btnGlobalStop.addEventListener('click', function () {
        PrtsBridge.stopCurrentTask().catch(handleBridgeError('停止任务失败'));
      });
    }

    // Banner Actions
    var btnCopyAgent = $('banner-btn-copy-agent');
    if (btnCopyAgent) {
      btnCopyAgent.addEventListener('click', function () {
        PrtsBridge.getAgentPrompts().then(function (res) {
          var text = res ? (res.repair_prompt || res.prompt_repair || res.init_prompt || res.prompt_init || '') : '';
          if (text) {
            PrtsBridge.copyToClipboard(text).then(function () {
              showToast('success', '复制成功', '已复制协同指令至剪贴板');
            }).catch(handleBridgeError('复制失败'));
          } else {
            showToast('warn', '复制提示', '未获取到协同提示词内容');
          }
        }).catch(handleBridgeError('获取协同指令失败'));
      });
    }

    var btnOpenTask = $('banner-btn-open-task');
    if (btnOpenTask) {
      btnOpenTask.addEventListener('click', function () {
        PrtsBridge.openPath('agent_task.json').catch(handleBridgeError('打开文件失败'));
      });
    }

    var btnResumeRules = $('banner-btn-resume-rules');
    if (btnResumeRules) {
      btnResumeRules.addEventListener('click', function () {
        PrtsBridge.resumeWithRules().catch(handleBridgeError('恢复执行失败'));
      });
    }

    var btnStopAgent = $('banner-btn-stop-agent');
    if (btnStopAgent) {
      btnStopAgent.addEventListener('click', function () {
        PrtsBridge.stopCurrentTask().catch(handleBridgeError('停止任务失败'));
      });
    }

    var btnResumeTut = $('banner-btn-resume-tutorial');
    if (btnResumeTut) {
      btnResumeTut.addEventListener('click', function () {
        PrtsBridge.resumeFromTutorial().catch(handleBridgeError('继续推图失败'));
      });
    }

    var btnStopTut = $('banner-btn-stop-tutorial');
    if (btnStopTut) {
      btnStopTut.addEventListener('click', function () {
        PrtsBridge.stopCurrentTask().catch(handleBridgeError('停止任务失败'));
      });
    }

    // Page 1 Actions
    var btnStartDaily = $('btn-start-daily');
    if (btnStartDaily) {
      btnStartDaily.addEventListener('click', function () {
        if (!guardTaskNotRunning('一键日常收菜')) return;
        PrtsBridge.startDaily()
          .then(handleTaskStartResult('一键日常收菜'))
          .catch(handleBridgeError('启动日常失败'));
      });
    }

    var btnStartAnni = $('btn-start-annihilation');
    if (btnStartAnni) {
      btnStartAnni.addEventListener('click', function () {
        if (!guardTaskNotRunning('当期剿灭托管')) return;
        PrtsBridge.startAnnihilation()
          .then(handleTaskStartResult('当期剿灭托管'))
          .catch(handleBridgeError('启动剿灭失败'));
      });
    }

    // Segmented server control
    var segServerBtns = $$('#seg-server .segmented-btn');
    for (var i = 0; i < segServerBtns.length; i++) {
      segServerBtns[i].addEventListener('click', function () {
        var val = this.getAttribute('data-value');
        updateSegmentedUI('seg-server', val);
        PrtsBridge.setServer(val).catch(handleBridgeError('切换区服失败'));
      });
    }

    // Segmented post action control
    var segPostBtns = $$('#seg-post-action .segmented-btn');
    for (var j = 0; j < segPostBtns.length; j++) {
      segPostBtns[j].addEventListener('click', function () {
        var val = this.getAttribute('data-value');
        updateSegmentedUI('seg-post-action', val);
        PrtsBridge.setPostAction(val).catch(handleBridgeError('切换完成后动作失败'));
      });
    }

    // Segmented fight mode control
    var segFightBtns = $$('#set-fight-mode .segmented-btn');
    for (var k = 0; k < segFightBtns.length; k++) {
      segFightBtns[k].addEventListener('click', function () {
        var val = this.getAttribute('data-value');
        updateSegmentedUI('set-fight-mode', val);
      });
    }

    // Page 2 Actions
    var btnStartPush = $('btn-start-push');
    if (btnStartPush) {
      btnStartPush.addEventListener('click', function () {
        if (!guardTaskNotRunning('新活动/主线推图')) return;
        var stageInput = $('input-push-stage');
        var val = stageInput ? stageInput.value.trim() : '';
        PrtsBridge.startPush(val)
          .then(handleTaskStartResult('新活动/主线推图'))
          .catch(handleBridgeError('启动推图失败'));
      });
    }

    var btnStartAutoGrowth = $('btn-start-auto-growth');
    if (btnStartAutoGrowth) {
      btnStartAutoGrowth.addEventListener('click', function () {
        if (!guardTaskNotRunning('智能自主养成')) return;
        PrtsBridge.startAutoGrowth()
          .then(handleTaskStartResult('智能自主养成'))
          .catch(handleBridgeError('启动自主养成失败'));
      });
    }

    var btnPreviewGrowth = $('btn-preview-growth');
    if (btnPreviewGrowth) {
      btnPreviewGrowth.addEventListener('click', function () {
        PrtsBridge.previewGrowthPlan().then(function (res) {
          if (res && res.ok !== false && res.plan) {
            var content = $('text-growth-preview-content');
            if (content) {
              content.textContent = typeof res.plan === 'string' ? res.plan : JSON.stringify(res.plan, null, 2);
            }
            openModal('modal-growth-preview');
          } else {
            var msg = (res && (res.reason || res.message || res.error)) || '暂无需要规划刷取的干员材料';
            showToast('info', '养成决策', msg);
          }
        }).catch(handleBridgeError('预览决策失败'));
      });
    }

    var btnExecGrowthFromPreview = $('btn-exec-growth-from-preview');
    if (btnExecGrowthFromPreview) {
      btnExecGrowthFromPreview.addEventListener('click', function () {
        if (!guardTaskNotRunning('智能自主养成')) return;
        closeModal('modal-growth-preview');
        PrtsBridge.startAutoGrowth()
          .then(handleTaskStartResult('智能自主养成'))
          .catch(handleBridgeError('启动自主养成失败'));
      });
    }

    var btnSyncBox = $('btn-sync-box');
    if (btnSyncBox) {
      btnSyncBox.addEventListener('click', function () {
        if (!guardTaskNotRunning('刷新干员与仓库')) return;
        PrtsBridge.startSyncBox()
          .then(handleTaskStartResult('刷新干员与仓库'))
          .catch(handleBridgeError('刷新档案失败'));
      });
    }

    // Page 3 Actions
    var selectCardMode = $('select-rl-card-mode');
    if (selectCardMode) {
      selectCardMode.addEventListener('change', function () {
        PrtsBridge.setRoguelikeMode(this.value).catch(handleBridgeError('切换肉鸽模式失败'));
      });
    }

    var btnStartRl = $('btn-start-roguelike');
    if (btnStartRl) {
      btnStartRl.addEventListener('click', function () {
        if (!guardTaskNotRunning('集成战略探索')) return;
        var mode = selectCardMode ? selectCardMode.value : '';
        PrtsBridge.startRoguelike(mode)
          .then(handleTaskStartResult('集成战略探索'))
          .catch(handleBridgeError('启动集成战略失败'));
      });
    }

    var btnRlSettlement = $('btn-rl-settlement');
    if (btnRlSettlement) {
      btnRlSettlement.addEventListener('click', function () {
        if (!guardTaskNotRunning('一键领奖与加点')) return;
        PrtsBridge.startRoguelikePostSettlement()
          .then(handleTaskStartResult('一键领奖与加点'))
          .catch(handleBridgeError('领奖与加点失败'));
      });
    }

    var btnToggleRa = $('btn-toggle-reclamation');
    if (btnToggleRa) {
      btnToggleRa.addEventListener('click', function () {
        PrtsBridge.toggleReclamation()
          .then(handleTaskStartResult('生息演算'))
          .catch(handleBridgeError('生息演算操作失败'));
      });
    }

    // Page 4 Actions
    var btnOpenReports = $('btn-open-reports');
    if (btnOpenReports) {
      btnOpenReports.addEventListener('click', function () {
        PrtsBridge.openPath('reports').catch(handleBridgeError('打开战报目录失败'));
      });
    }

    var btnClearTerminal = $('btn-clear-terminal');
    if (btnClearTerminal) {
      btnClearTerminal.addEventListener('click', function () {
        PrtsBridge.clearLogs().then(function () {
          appState.logs = [];
          renderTerminalLogs();
          var drawerPreview = $('drawer-content-preview');
          if (drawerPreview) drawerPreview.innerHTML = '';
          var drawerLatest = $('drawer-latest-text');
          if (drawerLatest) {
            drawerLatest.innerHTML = '<span class="log-tag tag-normal">[系统]</span> <span>日志已清空。</span>';
          }
        }).catch(handleBridgeError('清空日志失败'));
      });
    }

    var terminalSearch = $('terminal-search');
    if (terminalSearch) {
      terminalSearch.addEventListener('input', function () {
        appState.logSearchQuery = this.value;
        renderTerminalLogs();
      });
    }

    bindDryRunControls();

    // Settings Modal actions
    var btnSaveSettings = $('btn-save-settings');
    if (btnSaveSettings) {
      btnSaveSettings.addEventListener('click', function () {
        collectAndSaveSettings();
      });
    }

    var btnBrowseMaaSettings = $('btn-browse-maa-settings');
    if (btnBrowseMaaSettings) {
      btnBrowseMaaSettings.addEventListener('click', function () {
        PrtsBridge.browseMaaFolder().then(function (res) {
          var chosen = res ? (res.path || res.folder || '') : '';
          if (chosen && $('set-maa-path')) {
            $('set-maa-path').value = chosen;
          }
        }).catch(handleBridgeError('选择目录失败'));
      });
    }

    var btnSyncGuiConfig = $('btn-sync-gui-config');
    if (btnSyncGuiConfig) {
      btnSyncGuiConfig.addEventListener('click', function () {
        var maaPath = $('set-maa-path') ? $('set-maa-path').value : '';
        PrtsBridge.syncExplorationFromMaaGui(maaPath).then(function (res) {
          if (res && res.ok) {
            showToast('success', '同步成功', '已继承原版 MAA 肉鸽与生息配置');
            loadSettingsIntoModal();
          } else {
            showToast('warn', '同步失败', (res && res.error) || '未找到原版配置文件');
          }
        }).catch(handleBridgeError('同步配置失败'));
      });
    }

    var btnTestWebhook = $('btn-test-webhook-send');
    if (btnTestWebhook) {
      btnTestWebhook.addEventListener('click', function () {
        var url = $('set-webhook-url') ? $('set-webhook-url').value.trim() : '';
        if (!url) {
          showToast('warn', '提示', '请先填写 Webhook URL');
          return;
        }
        var lbl = $('btn-test-webhook-label') || btnTestWebhook.querySelector('span');
        btnTestWebhook.disabled = true;
        if (lbl) lbl.textContent = '发送中...';
        PrtsBridge.testWebhook(url).then(function (res) {
          btnTestWebhook.disabled = false;
          if (lbl) lbl.textContent = '发送测试 Webhook 通知';
          if (res && res.sent) {
            showToast('success', '推送成功', 'HTTP 状态码: ' + res.status);
          } else {
            showToast('error', '推送失败', (res && res.error) || '未能送达目标地址');
          }
        }).catch(function (err) {
          btnTestWebhook.disabled = false;
          if (lbl) lbl.textContent = '发送测试 Webhook 通知';
          handleBridgeError('测试推送失败')(err);
        });
      });
    }

    var btnReopenWizard = $('btn-reopen-wizard-from-settings');
    if (btnReopenWizard) {
      btnReopenWizard.addEventListener('click', function () {
        closeModal('modal-settings');
        openWizardModal(false);
      });
    }

    // Agent Modal Prompt Copies
    var btnCopyP1 = $('btn-copy-prompt-init');
    if (btnCopyP1) {
      btnCopyP1.addEventListener('click', function () {
        var text = $('text-prompt-init') ? $('text-prompt-init').value : '';
        if (text) {
          PrtsBridge.copyToClipboard(text).then(function () {
            showToast('success', '复制成功', '提示词一已复制至剪贴板');
          }).catch(handleBridgeError('复制失败'));
        }
      });
    }

    var btnCopyP2 = $('btn-copy-prompt-repair');
    if (btnCopyP2) {
      btnCopyP2.addEventListener('click', function () {
        var text = $('text-prompt-repair') ? $('text-prompt-repair').value : '';
        if (text) {
          PrtsBridge.copyToClipboard(text).then(function () {
            showToast('success', '复制成功', '提示词二已复制至剪贴板');
          }).catch(handleBridgeError('复制失败'));
        }
      });
    }

    var btnOpenFolder = $('btn-open-workspace-folder');
    if (btnOpenFolder) {
      btnOpenFolder.addEventListener('click', function () {
        PrtsBridge.openPath('').catch(handleBridgeError('打开目录失败'));
      });
    }

    // MAA Update Modal actions
    var btnConfirmUpdate = $('btn-confirm-maa-update');
    if (btnConfirmUpdate) {
      btnConfirmUpdate.addEventListener('click', function () {
        PrtsBridge.confirmMaaUpdate(appState.updateReleaseInfo, appState.updateLocalVer, appState.updateRemoteVer)
          .catch(handleBridgeError('启动增量更新失败'));
      });
    }

    var btnCancelUpdate = $('btn-cancel-maa-update');
    if (btnCancelUpdate) {
      btnCancelUpdate.addEventListener('click', function () {
        closeModal('modal-maa-update');
        PrtsBridge.cancelMaaUpdate().catch(function () {});
      });
    }

    // Page 5 Toolbox Actions
    var btnRefreshToolbox = $('btn-refresh-toolbox');
    if (btnRefreshToolbox) {
      btnRefreshToolbox.addEventListener('click', function () {
        loadToolboxCatalog(true);
      });
    }

    var btnStartToolbox = $('btn-start-toolbox');
    if (btnStartToolbox) {
      btnStartToolbox.addEventListener('click', function () {
        var item = findToolboxItem(appState.activeToolboxId);
        if (!item) return;

        if (item.available === false) {
          showToast('warn', '组件不可用', '当前 MAA 版本缺少该组件资源，请更新 MAA。');
          return;
        }

        if (item.id === 'reclamation') {
          PrtsBridge.toggleReclamation()
            .then(handleTaskStartResult('生息演算'))
            .catch(handleBridgeError('生息演算操作失败'));
          return;
        }

        if (!guardTaskNotRunning(item.name)) return;

        var params = collectToolboxFormParams(item.id);
        PrtsBridge.startToolboxTask(item.id, params)
          .then(handleTaskStartResult(item.name))
          .catch(handleBridgeError('启动小工具任务失败'));
      });
    }

    var btnToolboxSec = $('btn-toolbox-secondary');
    if (btnToolboxSec) {
      btnToolboxSec.addEventListener('click', function () {
        var item = findToolboxItem(appState.activeToolboxId);
        if (item && item.id === 'reclamation') {
          switchPage('page-exploration');
        }
      });
    }
  }

  // Populate Filter Chips on Terminal Header
  function renderFilterChips(categories) {
    var container = $('terminal-filter-chips');
    if (!container || !Array.isArray(categories)) return;

    container.innerHTML = '';
    for (var i = 0; i < categories.length; i++) {
      var cat = categories[i];
      var chip = document.createElement('button');
      chip.className = 'filter-chip ' + (cat.key === appState.logFilterTag ? 'active' : '');
      chip.textContent = cat.label;
      chip.setAttribute('data-tag', cat.key);
      chip.addEventListener('click', function () {
        var tag = this.getAttribute('data-tag');
        appState.logFilterTag = tag;
        var allChips = container.querySelectorAll('.filter-chip');
        for (var c = 0; c < allChips.length; c++) allChips[c].classList.remove('active');
        this.classList.add('active');
        renderTerminalLogs();
      });
      container.appendChild(chip);
    }
  }

  // Toolbox Catalog & Dynamic Form Management
  function findToolboxItem(toolId) {
    if (!appState.toolboxCatalog || !appState.toolboxCatalog.categories) return null;
    var cats = appState.toolboxCatalog.categories;
    for (var c = 0; c < cats.length; c++) {
      var items = cats[c].items || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].id === toolId) return items[i];
      }
    }
    return null;
  }

  function loadToolboxCatalog(refreshSnapshot) {
    return PrtsBridge.getToolboxCatalog().then(function (catalog) {
      if (!catalog) return;
      appState.toolboxCatalog = catalog;

      if (!appState.activeToolboxId) {
        var firstItem = null;
        var cats = catalog.categories || [];
        for (var c = 0; c < cats.length; c++) {
          var items = cats[c].items || [];
          if (items.length > 0) {
            firstItem = items[0];
            break;
          }
        }
        if (firstItem) {
          appState.activeToolboxId = firstItem.id;
        }
      }

      renderToolboxSidebar();
      renderToolboxDetail();

      if (refreshSnapshot) {
        PrtsBridge.refreshSnapshot().then(function (res) {
          if (res && res.snapshot) updateSnapshotUI(res.snapshot);
        }).catch(function () {});
      }
    }).catch(handleBridgeError('加载小工具目录失败'));
  }

  function renderToolboxSidebar() {
    var container = $('toolbox-nav-list');
    if (!container || !appState.toolboxCatalog) return;

    var cats = appState.toolboxCatalog.categories || [];
    var html = '';
    for (var c = 0; c < cats.length; c++) {
      var cat = cats[c];
      var items = cat.items || [];
      if (items.length === 0) continue;
      var catTitle = cat.title || cat.name || cat.key || cat.id || '';
      var isCurrentEvent = (cat.key || cat.id || cat.category_id) === 'current_event';
      html += '<div class="toolbox-category-group">';
      html += '<div class="toolbox-category-label">' + escapeHtml(catTitle) + '</div>';
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        var isActive = it.id === appState.activeToolboxId;
        var unavailClass = it.available === false ? ' unavailable' : '';
        var activeClass = isActive ? ' active' : '';
        var tagLabel = it.available === false ? '不可用' : (isCurrentEvent ? '当期' : '');
        var pillHtml = tagLabel ? '<span class="toolbox-item-pill">' + escapeHtml(tagLabel) + '</span>' : '';
        html += '<button type="button" class="toolbox-nav-item' + activeClass + unavailClass + '" data-tool-id="' + escapeHtml(it.id) + '">' +
          '<span class="toolbox-item-name">' + escapeHtml(it.name) + '</span>' +
          pillHtml +
          '</button>';
      }
      html += '</div>';
    }
    container.innerHTML = html;

    var navBtns = container.querySelectorAll('.toolbox-nav-item');
    for (var b = 0; b < navBtns.length; b++) {
      navBtns[b].addEventListener('click', function () {
        var tid = this.getAttribute('data-tool-id');
        if (tid && tid !== appState.activeToolboxId) {
          appState.activeToolboxId = tid;
          var all = container.querySelectorAll('.toolbox-nav-item');
          for (var k = 0; k < all.length; k++) all[k].classList.remove('active');
          this.classList.add('active');
          renderToolboxDetail();
        }
      });
    }
  }

  function renderToolboxDetail() {
    var item = findToolboxItem(appState.activeToolboxId);
    var titleEl = $('toolbox-detail-title');
    var badgeEl = $('toolbox-detail-badge');
    var chipEl = $('toolbox-maa-chip');
    var summaryEl = $('toolbox-detail-summary');
    var noticesBox = $('toolbox-notices-box');
    var paramsContainer = $('toolbox-params-container');

    if (!item) {
      if (titleEl) titleEl.textContent = '未选择工具';
      if (summaryEl) summaryEl.textContent = '请从左侧选择一个小工具';
      if (noticesBox) noticesBox.classList.add('hidden');
      if (paramsContainer) paramsContainer.innerHTML = '';
      updateToolboxButtons(null);
      return;
    }

    if (titleEl) titleEl.textContent = item.name || '';
    if (badgeEl) {
      var catTitle = '常驻功能';
      if (item.category === 'current_event') catTitle = '当期活动';
      else if (item.category === 'permanent_event') catTitle = '常驻活动';
      badgeEl.textContent = catTitle;
    }
    if (chipEl) {
      chipEl.textContent = item.version_req || 'MAA v6.23.2+';
    }
    if (summaryEl) {
      summaryEl.textContent = item.summary || '全自动任务。';
    }

    // Render notices
    if (noticesBox) {
      var notices = item.notices || [];
      if (typeof notices === 'string' && notices.trim()) notices = [notices.trim()];
      else if (!Array.isArray(notices)) notices = [];
      var fullNotices = notices.slice();
      if (item.available === false) {
        fullNotices.unshift('当前 MAA 内核版本缺少此小游戏或组件所需的任务资源，请在顶部状态栏或设置中检查更新 MAA。');
      }
      if (fullNotices.length > 0) {
        noticesBox.classList.remove('hidden');
        var noticeHtml = '';
        for (var n = 0; n < fullNotices.length; n++) {
          noticeHtml += '<div class="toolbox-notice-item">' +
            '<span style="font-weight:700;">[注意]</span> ' +
            '<span>' + escapeHtml(fullNotices[n]) + '</span>' +
            '</div>';
        }
        noticesBox.innerHTML = noticeHtml;
      } else {
        noticesBox.classList.add('hidden');
        noticesBox.innerHTML = '';
      }
    }

    // Render dynamic form
    renderToolboxForm(item, paramsContainer);

    // Update bottom buttons
    updateToolboxButtons(item);
  }

  function renderToolboxForm(item, container) {
    if (!container) return;
    container.innerHTML = '';

    var catalogCfg = (appState.toolboxCatalog && appState.toolboxCatalog.toolbox_config) || {};
    var opts = appState.options || {};

    if (item.id === 'secret_front') {
      var sfCfg = catalogCfg.secret_front || item.params || {};
      var curEnding = sfCfg.ending || 'EndingE';
      var curEvent = sfCfg.priority_event || '';

      var endings = opts.secret_front_endings || [
        { label: '结局 A', value: 'EndingA' },
        { label: '结局 B', value: 'EndingB' },
        { label: '结局 C', value: 'EndingC' },
        { label: '结局 D', value: 'EndingD' },
        { label: '结局 E（默认）', value: 'EndingE' }
      ];

      var events = opts.secret_front_events || [
        { label: '不优先指定事件', value: '' },
        { label: '支援作战平台', value: '支援作战平台' },
        { label: '游侠', value: '游侠' },
        { label: '诡影迷踪', value: '诡影迷踪' }
      ];

      var endingsHtml = '';
      for (var e = 0; e < endings.length; e++) {
        var selE = endings[e].value === curEnding ? ' selected' : '';
        endingsHtml += '<option value="' + escapeHtml(endings[e].value) + '"' + selE + '>' + escapeHtml(endings[e].label) + '</option>';
      }

      var eventsHtml = '';
      for (var ev = 0; ev < events.length; ev++) {
        var selEv = events[ev].value === curEvent ? ' selected' : '';
        eventsHtml += '<option value="' + escapeHtml(events[ev].value) + '"' + selEv + '>' + escapeHtml(events[ev].label) + '</option>';
      }

      var sfHtml = '<div class="m3-settings-group">' +
        '<div class="m3-group-label">隐秘战线参数设置</div>' +
        '<div class="m3-group-list">' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">目标结局</div>' +
              '<div class="m3-setting-desc">指定本次小游戏自动攻略的目标结局路线</div>' +
            '</div>' +
            '<select class="input-select" id="toolbox-sf-ending" style="width: 170px;">' + endingsHtml + '</select>' +
          '</div>' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">优先系列事件</div>' +
              '<div class="m3-setting-desc">探索途中遇到指定系列事件节点时优先选择</div>' +
            '</div>' +
            '<select class="input-select" id="toolbox-sf-event" style="width: 170px;">' + eventsHtml + '</select>' +
          '</div>' +
        '</div>' +
      '</div>';
      container.innerHTML = sfHtml;

      var endingSelect = $('toolbox-sf-ending');
      var eventSelect = $('toolbox-sf-event');
      function onSfChange() {
        var params = collectToolboxFormParams('secret_front');
        if (catalogCfg.secret_front) {
          catalogCfg.secret_front.ending = params.ending;
          catalogCfg.secret_front.priority_event = params.priority_event;
        }
        PrtsBridge.saveToolboxConfig('secret_front', params).catch(handleBridgeError('保存配置失败'));
      }
      if (endingSelect) endingSelect.addEventListener('change', onSfChange);
      if (eventSelect) eventSelect.addEventListener('change', onSfChange);

    } else if (item.id === 'stronghold_protocol') {
      var spCfg = catalogCfg.stronghold_protocol || item.params || {};
      var curTheme = spCfg.theme || 'Regulation';
      var curMode = spCfg.mode || 'badge';
      var curTarget = spCfg.target != null ? spCfg.target : 3;
      var curRuns = spCfg.runs != null ? spCfg.runs : 60;

      var rawThemes = (item.meta && item.meta.themes) || [{ label: '规程 (Regulation)', value: 'Regulation' }];
      var themesHtml = '';
      for (var t = 0; t < rawThemes.length; t++) {
        var thVal = rawThemes[t].value || rawThemes[t];
        var thLbl = rawThemes[t].label || rawThemes[t];
        var selT = thVal === curTheme ? ' selected' : '';
        themesHtml += '<option value="' + escapeHtml(thVal) + '"' + selT + '>' + escapeHtml(thLbl) + '</option>';
      }

      var rawRecOps = (item.meta && item.meta.recommended_operators) || '';
      var recOps = Array.isArray(rawRecOps) ? (rawRecOps.length ? '推荐核心与借人干员：' + rawRecOps.join('、') : '') : String(rawRecOps || '');
      var recOpsHtml = recOps ? (
        '<div class="m3-setting-item m3-setting-accent">' +
          '<div class="m3-setting-text">' +
            '<div class="m3-setting-title">推荐核心干员</div>' +
            '<div class="m3-setting-desc">' + escapeHtml(recOps) + '</div>' +
          '</div>' +
        '</div>'
      ) : '';

      var isScore = curMode === 'score';
      var targetDisplay = isScore ? 'display: none;' : '';
      var runsDisplay = isScore ? 'display: none;' : '';

      var spHtml = '<div class="m3-settings-group">' +
        '<div class="m3-group-label">卫戍协议参数设置</div>' +
        '<div class="m3-group-list">' +
          recOpsHtml +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">协议主题</div>' +
              '<div class="m3-setting-desc">选择当前当期或往期常驻协议主题</div>' +
            '</div>' +
            '<select class="input-select" id="toolbox-sp-theme" style="width: 170px;">' + themesHtml + '</select>' +
          '</div>' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">策略模式</div>' +
              '<div class="m3-setting-desc">刷角标（完整通关）或刷分（放生速通）</div>' +
            '</div>' +
            '<div class="segmented-control" id="toolbox-sp-mode">' +
              '<button type="button" class="segmented-btn' + (!isScore ? ' active' : '') + '" data-value="badge"><span>刷角标</span></button>' +
              '<button type="button" class="segmented-btn' + (isScore ? ' active' : '') + '" data-value="score"><span>刷分速通</span></button>' +
            '</div>' +
          '</div>' +
          '<div class="m3-setting-item" id="toolbox-sp-target-row" style="' + targetDisplay + '">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">角标目标</div>' +
              '<div class="m3-setting-desc">通关次数达到目标后自动停止</div>' +
            '</div>' +
            '<div class="segmented-control" id="toolbox-sp-target">' +
              '<button type="button" class="segmented-btn' + (curTarget === 3 ? ' active' : '') + '" data-value="3"><span>金色角标 (3次)</span></button>' +
              '<button type="button" class="segmented-btn' + (curTarget === 6 ? ' active' : '') + '" data-value="6"><span>彩色角标 (6次)</span></button>' +
            '</div>' +
          '</div>' +
          '<div class="m3-setting-item" id="toolbox-sp-runs-row" style="' + runsDisplay + '">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">通关局数上限</div>' +
              '<div class="m3-setting-desc">单次任务最大尝试探索局数 (1~999)</div>' +
            '</div>' +
            '<input type="number" class="input-text" id="toolbox-sp-runs" style="width: 100px;" min="1" max="999" value="' + escapeHtml(String(curRuns)) + '">' +
          '</div>' +
        '</div>' +
      '</div>';
      container.innerHTML = spHtml;

      function onSpChange() {
        var params = collectToolboxFormParams('stronghold_protocol');
        if (catalogCfg.stronghold_protocol) {
          catalogCfg.stronghold_protocol.theme = params.theme;
          catalogCfg.stronghold_protocol.mode = params.mode;
          catalogCfg.stronghold_protocol.target = params.target;
          catalogCfg.stronghold_protocol.runs = params.runs;
        }
        PrtsBridge.saveToolboxConfig('stronghold_protocol', params).catch(handleBridgeError('保存配置失败'));
      }

      var modeBtns = $$('#toolbox-sp-mode .segmented-btn');
      for (var mb = 0; mb < modeBtns.length; mb++) {
        modeBtns[mb].addEventListener('click', function () {
          for (var k = 0; k < modeBtns.length; k++) modeBtns[k].classList.remove('active');
          this.classList.add('active');
          var isSc = this.getAttribute('data-value') === 'score';
          var tRow = $('toolbox-sp-target-row');
          var rRow = $('toolbox-sp-runs-row');
          if (tRow) tRow.style.display = isSc ? 'none' : '';
          if (rRow) rRow.style.display = isSc ? 'none' : '';
          onSpChange();
        });
      }

      var targetBtns = $$('#toolbox-sp-target .segmented-btn');
      for (var tb = 0; tb < targetBtns.length; tb++) {
        targetBtns[tb].addEventListener('click', function () {
          for (var k = 0; k < targetBtns.length; k++) targetBtns[k].classList.remove('active');
          this.classList.add('active');
          onSpChange();
        });
      }

      var themeSelect = $('toolbox-sp-theme');
      if (themeSelect) themeSelect.addEventListener('change', onSpChange);
      var runsInput = $('toolbox-sp-runs');
      if (runsInput) runsInput.addEventListener('change', onSpChange);

    } else if (item.id === 'reclamation') {
      var snapRa = (appState.snapshot && appState.snapshot.reclamation) || {};
      var catRa = (appState.toolboxCatalog && appState.toolboxCatalog.reclamation) || {};
      var curRaTheme = snapRa.theme != null ? snapRa.theme : (catRa.theme != null ? catRa.theme : 'RelaunchAnchor');
      var curRaMode = snapRa.mode_label != null ? snapRa.mode_label : (snapRa.mode != null ? snapRa.mode : (catRa.mode_label != null ? catRa.mode_label : (catRa.mode != null ? catRa.mode : 'RA1')));
      var curClear = snapRa.clear_store != null ? snapRa.clear_store : (catRa.clear_store != null ? catRa.clear_store : true);

      var raThemes = opts.ra_themes || [
        { label: '跟随原版 MAA (默认重启锚点)', value: '' },
        { label: '重启锚点 (RelaunchAnchor)', value: 'RelaunchAnchor' },
        { label: '沙洲遗闻 (Tales)', value: 'Tales' }
      ];

      var raModes = opts.ra_modes || [
        { label: '跟随原版 MAA', value: '' },
        { label: 'RA-1 关卡循环 · 精耕细作 (RA1)', value: 'RA1' },
        { label: 'RA-4 关卡循环 · 筹划经营/维什戴尔 (RA4)', value: 'RA4' },
        { label: 'RA-15 关卡循环 · 筹划经营/圣聆初雪 (RA15)', value: 'RA15' },
        { label: '沙洲遗闻 · 无存档刷繁荣度 (ProsperityNoSave)', value: 'ProsperityNoSave' },
        { label: '沙洲遗闻 · 存档造物刷繁荣度 (ProsperityInSave)', value: 'ProsperityInSave' }
      ];

      var raThemesHtml = '';
      for (var rth = 0; rth < raThemes.length; rth++) {
        var selRth = raThemes[rth].value === curRaTheme ? ' selected' : '';
        raThemesHtml += '<option value="' + escapeHtml(raThemes[rth].value) + '"' + selRth + '>' + escapeHtml(raThemes[rth].label) + '</option>';
      }

      var raModesHtml = '';
      for (var rm = 0; rm < raModes.length; rm++) {
        var selRm = raModes[rm].value === curRaMode ? ' selected' : '';
        raModesHtml += '<option value="' + escapeHtml(raModes[rm].value) + '"' + selRm + '>' + escapeHtml(raModes[rm].label) + '</option>';
      }

      var checkedClear = curClear ? ' checked' : '';

      var raHtml = '<div class="m3-settings-group">' +
        '<div class="m3-group-label">生息演算联动设置</div>' +
        '<div class="m3-group-list">' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">演算主题</div>' +
              '<div class="m3-setting-desc">选择生息演算主题剧本</div>' +
            '</div>' +
            '<select class="input-select" id="toolbox-ra-theme" style="width: 200px;">' + raThemesHtml + '</select>' +
          '</div>' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">刷取模式</div>' +
              '<div class="m3-setting-desc">指定挂机或繁荣度刷取策略</div>' +
            '</div>' +
            '<select class="input-select" id="toolbox-ra-mode" style="width: 200px;">' + raModesHtml + '</select>' +
          '</div>' +
          '<label class="m3-setting-item switch-label">' +
            '<span class="m3-setting-text">' +
              '<span class="m3-setting-title">自动搬空演算商店</span>' +
              '<span class="m3-setting-desc">每轮周期自动前往黑市兑换全部商品</span>' +
            '</span>' +
            '<input type="checkbox" class="switch-input" id="toolbox-ra-clear-store"' + checkedClear + '>' +
            '<span class="switch-track"><span class="switch-thumb"><svg class="switch-icon-on"><use href="#icon-check"/></svg><svg class="switch-icon-off"><use href="#icon-minus"/></svg></span></span>' +
          '</label>' +
        '</div>' +
      '</div>';
      container.innerHTML = raHtml;

      function onRaChange() {
        var params = collectToolboxFormParams('reclamation');
        PrtsBridge.saveToolboxConfig('reclamation', params).then(function () {
          return PrtsBridge.refreshSnapshot();
        }).then(function (res) {
          if (res && res.snapshot) updateSnapshotUI(res.snapshot);
        }).catch(handleBridgeError('保存生息演算配置失败'));
      }

      var raThemeSel = $('toolbox-ra-theme');
      if (raThemeSel) raThemeSel.addEventListener('change', onRaChange);
      var raModeSel = $('toolbox-ra-mode');
      if (raModeSel) raModeSel.addEventListener('change', onRaChange);
      var raClearInp = $('toolbox-ra-clear-store');
      if (raClearInp) raClearInp.addEventListener('change', onRaChange);

    } else if (item.id === 'event_shop') {
      var esCfg = catalogCfg.event_shop || item.params || {};
      var presets = esCfg.blacklist_presets || [];
      var customBl = esCfg.custom_blacklist || '';

      var allPresets = opts.event_shop_presets || [
        { label: '数据增补仪', value: '数据增补仪' },
        { label: '数据增补条', value: '数据增补条' },
        { label: '龙门币', value: '龙门币' },
        { label: '家具零件', value: '家具零件' },
        { label: '无限池/其他', value: '其他' }
      ];

      var chipsHtml = '<div class="toolbox-chip-list" id="toolbox-es-presets">';
      for (var p = 0; p < allPresets.length; p++) {
        var pVal = allPresets[p].value;
        var pLbl = allPresets[p].label;
        var isAct = presets.indexOf(pVal) !== -1;
        var actClass = isAct ? ' active' : '';
        chipsHtml += '<button type="button" class="filter-chip' + actClass + '" data-value="' + escapeHtml(pVal) + '">' +
          escapeHtml(pLbl) +
          '</button>';
      }
      chipsHtml += '</div>';

      var esHtml = '<div class="m3-settings-group">' +
        '<div class="m3-group-label">活动商店黑名单配置</div>' +
        '<div class="m3-group-list">' +
          '<div class="m3-setting-item m3-setting-stack">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">快速黑名单选项（点击切换）</div>' +
              '<div class="m3-setting-desc">勾选项将不会在活动商店中自动购买</div>' +
            '</div>' +
            chipsHtml +
          '</div>' +
          '<div class="m3-setting-item m3-setting-stack">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">自定义黑名单商品名称</div>' +
              '<div class="m3-setting-desc">以逗号、分号或空格分隔不希望购买的商品名称</div>' +
            '</div>' +
            '<input type="text" class="input-text" id="toolbox-es-custom" placeholder="例如：龙门币, 招聘许可, 家具零件" value="' + escapeHtml(customBl) + '">' +
          '</div>' +
        '</div>' +
      '</div>';
      container.innerHTML = esHtml;

      function onEsChange() {
        var params = collectToolboxFormParams('event_shop');
        if (catalogCfg.event_shop) {
          catalogCfg.event_shop.blacklist_presets = params.blacklist_presets;
          catalogCfg.event_shop.custom_blacklist = params.custom_blacklist;
        }
        PrtsBridge.saveToolboxConfig('event_shop', params).catch(handleBridgeError('保存配置失败'));
      }

      var presetChips = $$('#toolbox-es-presets .filter-chip');
      for (var pc = 0; pc < presetChips.length; pc++) {
        presetChips[pc].addEventListener('click', function () {
          this.classList.toggle('active');
          onEsChange();
        });
      }

      var customInp = $('toolbox-es-custom');
      if (customInp) customInp.addEventListener('change', onEsChange);

    } else if (item.id === 'auto_raise_potential') {
      var arpCfg = catalogCfg.auto_raise_potential || item.params || {};
      var useNormal = Boolean(arpCfg.use_normal_token);
      var checkedNorm = useNormal ? ' checked' : '';

      var arpHtml = '<div class="m3-settings-group">' +
        '<div class="m3-group-label">信物提升潜能设置</div>' +
        '<div class="m3-group-list">' +
          '<label class="m3-setting-item switch-label">' +
            '<span class="m3-setting-text">' +
              '<span class="m3-setting-title">中坚信物不足时消耗普通信物</span>' +
              '<span class="m3-setting-desc">开启后，若提升中坚干员时中坚信物数量不足，允许消耗通用信物或普通干员信物</span>' +
            '</span>' +
            '<input type="checkbox" class="switch-input" id="toolbox-arp-use-normal"' + checkedNorm + '>' +
            '<span class="switch-track"><span class="switch-thumb"><svg class="switch-icon-on"><use href="#icon-check"/></svg><svg class="switch-icon-off"><use href="#icon-minus"/></svg></span></span>' +
          '</label>' +
        '</div>' +
      '</div>';
      container.innerHTML = arpHtml;

      var arpInp = $('toolbox-arp-use-normal');
      if (arpInp) {
        arpInp.addEventListener('change', function () {
          var params = { use_normal_token: this.checked };
          if (catalogCfg.auto_raise_potential) {
            catalogCfg.auto_raise_potential.use_normal_token = params.use_normal_token;
          }
          PrtsBridge.saveToolboxConfig('auto_raise_potential', params).catch(handleBridgeError('保存配置失败'));
        });
      }

    } else if (item.id === 'blackflow_money') {
      var bfCfg = catalogCfg.blackflow_money || item.params || {};
      var stopFull = bfCfg.stop_when_investment_full != null ? Boolean(bfCfg.stop_when_investment_full) : true;
      var bfRuns = bfCfg.starts_count != null ? bfCfg.starts_count : 999;
      var checkedFull = stopFull ? ' checked' : '';

      var bfHtml = '<div class="m3-settings-group">' +
        '<div class="m3-group-label">黑流树海刷钱设置</div>' +
        '<div class="m3-group-list">' +
          '<label class="m3-setting-item switch-label">' +
            '<span class="m3-setting-text">' +
              '<span class="m3-setting-title">源石锭存满即停</span>' +
              '<span class="m3-setting-desc">当源石锭投资额度达到上限时自动结束刷取</span>' +
            '</span>' +
            '<input type="checkbox" class="switch-input" id="toolbox-bf-stop-full"' + checkedFull + '>' +
            '<span class="switch-track"><span class="switch-thumb"><svg class="switch-icon-on"><use href="#icon-check"/></svg><svg class="switch-icon-off"><use href="#icon-minus"/></svg></span></span>' +
          '</label>' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">最大探索局数</div>' +
              '<div class="m3-setting-desc">单次刷钱任务的最大探索局数 (1~999)</div>' +
            '</div>' +
            '<input type="number" class="input-text" id="toolbox-bf-runs" style="width: 100px;" min="1" max="999" value="' + escapeHtml(String(bfRuns)) + '">' +
          '</div>' +
        '</div>' +
      '</div>';
      container.innerHTML = bfHtml;

      function onBfChange() {
        var params = collectToolboxFormParams('blackflow_money');
        if (catalogCfg.blackflow_money) {
          catalogCfg.blackflow_money.stop_when_investment_full = params.stop_when_investment_full;
          catalogCfg.blackflow_money.starts_count = params.starts_count;
        }
        PrtsBridge.saveToolboxConfig('blackflow_money', params).catch(handleBridgeError('保存配置失败'));
      }

      var bfStopInp = $('toolbox-bf-stop-full');
      if (bfStopInp) bfStopInp.addEventListener('change', onBfChange);
      var bfRunsInp = $('toolbox-bf-runs');
      if (bfRunsInp) bfRunsInp.addEventListener('change', onBfChange);

    } else {
      var noParamHtml = '<div class="m3-settings-group">' +
        '<div class="m3-group-label">自动化任务说明</div>' +
        '<div class="m3-group-list">' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">执行方式</div>' +
              '<div class="m3-setting-desc">该工具无需额外参数，点击下方「启动任务」即可由 MAA 核心全自动执行</div>' +
            '</div>' +
          '</div>' +
          '<div class="m3-setting-item">' +
            '<div class="m3-setting-text">' +
              '<div class="m3-setting-title">功能概览</div>' +
              '<div class="m3-setting-desc">' + escapeHtml(item.summary || '全自动化小游戏/功能辅助') + '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';
      container.innerHTML = noParamHtml;
    }
  }

  function collectToolboxFormParams(toolId) {
    if (toolId === 'secret_front') {
      var endingEl = $('toolbox-sf-ending');
      var eventEl = $('toolbox-sf-event');
      return {
        ending: endingEl ? endingEl.value : 'EndingE',
        priority_event: eventEl ? eventEl.value : ''
      };
    } else if (toolId === 'stronghold_protocol') {
      var themeEl = $('toolbox-sp-theme');
      var modeActive = document.querySelector('#toolbox-sp-mode .segmented-btn.active');
      var targetActive = document.querySelector('#toolbox-sp-target .segmented-btn.active');
      var runsEl = $('toolbox-sp-runs');
      var mode = modeActive ? modeActive.getAttribute('data-value') : 'badge';
      var target = targetActive ? parseInt(targetActive.getAttribute('data-value'), 10) : 3;
      var runs = runsEl ? parseInt(runsEl.value, 10) : 60;
      if (isNaN(runs) || runs <= 0) runs = 60;
      return {
        theme: themeEl ? themeEl.value : 'Regulation',
        mode: mode,
        target: target,
        runs: runs,
        attempts: 0
      };
    } else if (toolId === 'reclamation') {
      var raThemeEl = $('toolbox-ra-theme');
      var raModeEl = $('toolbox-ra-mode');
      var raClearEl = $('toolbox-ra-clear-store');
      return {
        theme: raThemeEl ? raThemeEl.value : 'RelaunchAnchor',
        mode: raModeEl ? raModeEl.value : 'RA1',
        clear_store: raClearEl ? raClearEl.checked : true
      };
    } else if (toolId === 'event_shop') {
      var activePresetChips = $$('#toolbox-es-presets .filter-chip.active');
      var presets = [];
      for (var p = 0; p < activePresetChips.length; p++) {
        presets.push(activePresetChips[p].getAttribute('data-value'));
      }
      var customEl = $('toolbox-es-custom');
      return {
        blacklist_presets: presets,
        custom_blacklist: customEl ? customEl.value.trim() : ''
      };
    } else if (toolId === 'auto_raise_potential') {
      var arpEl = $('toolbox-arp-use-normal');
      return {
        use_normal_token: arpEl ? arpEl.checked : false
      };
    } else if (toolId === 'blackflow_money') {
      var stopEl = $('toolbox-bf-stop-full');
      var bfRunsEl = $('toolbox-bf-runs');
      var bfRuns = bfRunsEl ? parseInt(bfRunsEl.value, 10) : 999;
      if (isNaN(bfRuns) || bfRuns <= 0) bfRuns = 999;
      return {
        stop_when_investment_full: stopEl ? stopEl.checked : true,
        starts_count: bfRuns
      };
    }
    return {};
  }

  function updateToolboxButtons(item) {
    var btnStart = $('btn-start-toolbox');
    var btnStartText = $('btn-start-toolbox-text');
    var btnSecondary = $('btn-toolbox-secondary');
    var btnSecondaryText = $('btn-toolbox-secondary-text');
    if (!btnStart) return;

    var state = appState.taskState || {};
    var isRunning = Boolean(state.running);
    var mutexDisabled = typeof state.task_buttons_disabled === 'boolean' ? state.task_buttons_disabled : isRunning;
    var taskKind = state.task_kind || 'idle';
    var isReclamation = item && item.id === 'reclamation';

    if (isReclamation) {
      if (taskKind === 'reclamation' && isRunning) {
        btnStart.className = 'btn btn-danger';
        btnStart.disabled = Boolean(state.ra_button_enabled === false);
        if (btnStartText) btnStartText.textContent = state.ra_button_text || '停止生息演算';
      } else {
        btnStart.className = 'btn btn-primary';
        btnStart.disabled = mutexDisabled || (item.available === false);
        if (btnStartText) btnStartText.textContent = state.ra_button_text || '启动生息演算';
      }

      if (btnSecondary) {
        btnSecondary.classList.remove('hidden');
        if (btnSecondaryText) btnSecondaryText.textContent = '肉鸽与生息卡片';
      }
    } else {
      btnStart.className = 'btn btn-primary';
      btnStart.disabled = mutexDisabled || (!item || item.available === false);
      if (btnStartText) btnStartText.textContent = '启动任务';

      if (btnSecondary) {
        btnSecondary.classList.add('hidden');
      }
    }
  }

  // Expose helper hooks for bridge smoke runner fallback
  window.switchPage = switchPage;
  window.openModal = openModal;
  window.closeModal = closeModal;

  // Application Entrypoint
  function init() {
    setupThemeControls();
    setupNavigation();
    setupModals();
    setupWizard();
    setupActions();
    setupPushListeners();

    PrtsBridge.onReady(function () {
      PrtsBridge.getInitialState().then(function (state) {
        if (!state) return;
        appState.options = state.options || {};

        if (state.options && state.options.log_categories) {
          renderFilterChips(state.options.log_categories);
        }

        if (state.snapshot) {
          updateSnapshotUI(state.snapshot);
        }
        if (state.task_state) {
          updateTaskStateUI(state.task_state);
        }
        if (state.agent_banner) {
          applyAgentBanner(state.agent_banner);
        }
        if (state.tutorial_banner) {
          applyTutorialBanner(state.tutorial_banner);
        }
        if (state.logs) {
          appendLogs(state.logs);
        }
        if (state.open_wizard) {
          openWizardModal(true);
        }

        loadToolboxCatalog();

        if (typeof window.__prtsRunSmokeCheck === 'function') {
          var smoke = window.__prtsRunSmokeCheck();
          PrtsBridge.reportSmokeMetrics(smoke).catch(function () {});
        }
      }).catch(handleBridgeError('初始化面板状态失败'));
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})(window, document);
