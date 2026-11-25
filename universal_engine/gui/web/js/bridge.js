/**
 * PRTS Industrial Material 3 Python <-> Webview Bridge Client
 * Zero Emoji, Zero External Dependencies, Industrial Restraint
 */
(function (window) {
  'use strict';

  // Global Runtime Error & Console Error Collector for Automated Verification
  window.__prtsConsoleErrors = window.__prtsConsoleErrors || [];
  (function installErrorCollector() {
    var origConsoleError = console.error;
    console.error = function () {
      try {
        var parts = [];
        for (var i = 0; i < arguments.length; i++) {
          parts.push(typeof arguments[i] === 'string' ? arguments[i] : String(arguments[i]));
        }
        window.__prtsConsoleErrors.push('[console.error] ' + parts.join(' '));
      } catch (e) {}
      return origConsoleError.apply(console, arguments);
    };
    window.addEventListener('error', function (evt) {
      window.__prtsConsoleErrors.push('[window.error] ' + (evt && evt.message ? evt.message : String(evt)));
    });
    window.addEventListener('unhandledrejection', function (evt) {
      var reason = evt && evt.reason ? (evt.reason.message || String(evt.reason)) : 'unhandledrejection';
      window.__prtsConsoleErrors.push('[unhandledrejection] ' + reason);
    });
  })();

  // Fallback Mock API for local browser debugging or headless test harness
  var fallbackMockApi = {
    get_initial_state: function () {
      return Promise.resolve({
        snapshot: {
          config: {},
          maa_dir: '<示例路径>/MAA',
          maa: { valid: true, version: 'v6.17.5', reason: '' },
          adb: { connected: true, adb_address: '127.0.0.1:16384', width: 1280, height: 720, is_16_9: true, mumu_adbd_hung: false, warning: '' },
          box: { total_owned: 298, e2_count: 142 },
          depot: { syncTime: '2026-11-24 12:00:00', count: 185 },
          ai: { mode: 'agent_collab', model_name: 'gpt-4o-mini', is_builtin_api: false, label: '桌面 Agent 协同' },
          growth: { ok: true, recommended_stages: '14-20, 1-7', first_stage: '14-20', reason: '优先养成 维什戴尔' },
          annihilation: { week_key: '2026-W48', process: '1800 / 1800', done: true, label: '本周已完成' },
          server: 'Official',
          post_action: 'none',
          quick_summary: { fight_mode: 'AUTO', auto_push: true, text: 'AUTO (智能自主养成) | 自动推通未解锁' },
          roguelike: {
            theme: 'BlackFlow',
            theme_display: '沉沦者的黑流树海 (BlackFlow)',
            mode_label: '黑流生态养成',
            target_display: '黑流生态养成 (30001)',
            ai_copilot_enabled: true,
            card_mode: 'AI 辅助策略通关',
            auto_claim_rewards: true,
            auto_upgrade_tech_tree: true,
            sub_hint: 'AI 辅助决策已就绪'
          },
          reclamation: {
            theme: 'RelaunchAnchor',
            theme_display: '重启锚点',
            mode_label: 'RA1',
            mode_display: 'RA-1 关卡循环 (RA1 · 精耕细作)',
            badge_text: '精耕细作',
            clear_store: true,
            loaded_from_gui_config: true,
            sub_hint: '已就绪'
          }
        },
        task_state: {
          running: false,
          task_kind: 'idle',
          status_text: '就绪',
          progress_mode: 'indeterminate',
          progress_value: 0,
          progress_cur: 0,
          progress_total: 0,
          level: 'info',
          ra_button_text: '启动生息演算',
          ra_button_enabled: true,
          stop_enabled: false
        },
        logs: [
          { id: 1, ts: '12:00:00', text: '[系统] PRTS Industrial 控制台启动完成。', tag: 'normal' }
        ],
        options: {
          bridge_version: '1.0',
          rl_card_modes: ['纯 MAA 自动刷取', 'AI 辅助策略通关'],
          rl_themes: [
            { label: '跟随原版 MAA (默认黑流树海)', value: '' },
            { label: '沉沦者的黑流树海 (BlackFlow)', value: 'BlackFlow' },
            { label: '岁的界园志异 (JieGarden)', value: 'JieGarden' },
            { label: '萨卡兹的无终奇语 (Sarkaz)', value: 'Sarkaz' },
            { label: '探索者的银凇止境 (Sami)', value: 'Sami' },
            { label: '水月与深蓝之树 (Mizuki)', value: 'Mizuki' },
            { label: '傀影与猩红孤钻 (Phantom)', value: 'Phantom' }
          ],
          rl_targets: [
            { label: '跟随原版 MAA (默认刷等级)', value: '' },
            { label: '刷等级 / 经验 (Exp)', value: 'Exp' },
            { label: '刷源石锭投资 (Investment)', value: 'Investment' },
            { label: '刷藏品 / 烧水 (Collectible)', value: 'Collectible' },
            { label: '黑流生态养成 (BlackFlowCultivation)', value: 'BlackFlowCultivation' },
            { label: '月度小队 (MonthlySquad)', value: 'MonthlySquad' },
            { label: '深入调查 (DeepExploration)', value: 'DeepExploration' }
          ],
          ra_themes: [
            { label: '跟随原版 MAA (默认重启锚点)', value: '' },
            { label: '重启锚点 (RelaunchAnchor)', value: 'RelaunchAnchor' },
            { label: '沙洲遗闻 (Tales)', value: 'Tales' }
          ],
          ra_modes: [
            { label: '跟随原版 MAA', value: '' },
            { label: 'RA-1 关卡循环 · 精耕细作 (RA1)', value: 'RA1' },
            { label: 'RA-4 关卡循环 · 筹划经营/维什戴尔 (RA4)', value: 'RA4' },
            { label: 'RA-15 关卡循环 · 筹划经营/圣聆初雪 (RA15)', value: 'RA15' },
            { label: '沙洲遗闻 · 无存档刷繁荣度 (ProsperityNoSave)', value: 'ProsperityNoSave' },
            { label: '沙洲遗闻 · 存档造物刷繁荣度 (ProsperityInSave)', value: 'ProsperityInSave' }
          ],
          emulator_types: ['auto', 'mumu12', 'ldplayer', 'bluestacks', 'nox'],
          skip_tasks: [
            { key: 'startup', label: '唤醒' },
            { key: 'recruit', label: '公招' },
            { key: 'infrast', label: '基建' },
            { key: 'mall', label: '信用商店' },
            { key: 'annihilation', label: '剿灭' },
            { key: 'fight', label: '刷理智' },
            { key: 'award', label: '领奖' }
          ],
          api_presets: [
            { name: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', model_name: 'deepseek-chat' },
            { name: 'Gemini (OpenAI兼容)', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', model_name: 'gemini-2.5-flash' },
            { name: '本地 Ollama', base_url: 'http://127.0.0.1:11434/v1', model_name: 'qwen2.5:14b' },
            { name: 'OpenAI', base_url: 'https://api.openai.com/v1', model_name: 'gpt-4o-mini' }
          ],
          log_categories: [
            { key: 'all', label: '全部' },
            { key: 'growth', label: '养成决策' },
            { key: 'agent', label: 'Agent协同' },
            { key: 'tutorial', label: '教学交接' },
            { key: 'roguelike', label: '肉鸽AI' },
            { key: 'reclamation', label: '生息演算' },
            { key: 'report', label: '每日战报' },
            { key: 'alert', label: '告警/错误' }
          ]
        }
      });
    },
    refresh_snapshot: function () { return Promise.resolve({ ok: true }); },
    start_daily: function () { return Promise.resolve({ ok: true }); },
    start_auto_growth: function () { return Promise.resolve({ ok: true }); },
    preview_growth_plan: function () { return Promise.resolve({ ok: true, plan: [] }); },
    start_annihilation: function () { return Promise.resolve({ ok: true }); },
    start_push: function () { return Promise.resolve({ ok: true }); },
    start_sync_box: function () { return Promise.resolve({ ok: true }); },
    set_roguelike_mode: function () { return Promise.resolve({ ok: true }); },
    start_roguelike: function () { return Promise.resolve({ ok: true }); },
    start_roguelike_post_settlement: function () { return Promise.resolve({ ok: true }); },
    toggle_reclamation: function () { return Promise.resolve({ ok: true }); },
    stop_current_task: function () { return Promise.resolve({ ok: true }); },
    set_server: function () { return Promise.resolve({ ok: true }); },
    set_post_action: function () { return Promise.resolve({ ok: true }); },
    probe_or_launch_emulator: function () { return Promise.resolve({ ok: true, connected: true }); },
    check_maa_update: function () { return Promise.resolve({ ok: true, has_update: false }); },
    confirm_maa_update: function () { return Promise.resolve({ ok: true }); },
    cancel_maa_update: function () { return Promise.resolve({ ok: true }); },
    get_agent_prompts: function () {
      return Promise.resolve({
        init_prompt: 'Prompt Init Content',
        prompt_init: 'Prompt Init Content',
        repair_prompt: 'Prompt Repair Content',
        prompt_repair: 'Prompt Repair Content',
        readme_content: '# PRTS Engine\nREADME Content'
      });
    },
    copy_to_clipboard: function () { return Promise.resolve({ ok: true }); },
    open_path: function () { return Promise.resolve({ ok: true }); },
    resume_with_rules: function () { return Promise.resolve({ ok: true }); },
    resume_from_tutorial: function () { return Promise.resolve({ ok: true }); },
    get_settings: function () {
      return Promise.resolve({
        emulator: { adb_address: '127.0.0.1:16384' },
        ai: { mode: 'agent_collab', enabled: false, base_url: 'https://api.openai.com/v1', api_key: '', model_name: 'gpt-4o-mini', timeout_sec: 30 },
        daily: {
          fight_mode: 'AUTO',
          override_stage: '',
          medicine: 0,
          stone: 0,
          max_push_rounds: 5,
          auto_push: true,
          recruit_preserve_tags: ['高级资深干员', '资深干员', '支援机械'],
          recruit_times: 4,
          penguin_id: '',
          report_to_penguin: false,
          report_to_yituliu: false,
          skip_tasks: []
        },
        growth: {
          prefer_event: true,
          allow_chip_stages: true,
          allow_resource_stages: true,
          target_operators: ['维什戴尔'],
          material_safety_stock: { T3_DEFAULT: 30 }
        },
        roguelike: {
          use_gui_config: true,
          theme: 'BlackFlow',
          mode: 'BlackFlowCultivation',
          squad: '',
          roles: '',
          core_char: '维什戴尔',
          starts_count: null,
          ai_copilot_enabled: true,
          ai_auto_fallback_on_timeout: true,
          ai_decision_timeout_sec: 45,
          boss_prts_assist: true,
          auto_claim_rewards: true,
          auto_upgrade_tech_tree: true
        },
        reclamation: {
          use_gui_config: true,
          theme: 'RelaunchAnchor',
          mode: 'RA1',
          tools_to_craft: [],
          clear_store: true
        },
        notifications: {
          webhook_enabled: false,
          webhook_url: '',
          notify_on_daily_finish: true,
          notify_on_recruit_alert: true,
          notify_on_agent_task: true
        },
        maa_path: '<示例路径>/MAA'
      });
    },
    save_settings: function () { return Promise.resolve({ ok: true }); },
    sync_exploration_from_maa_gui: function () { return Promise.resolve({ ok: true }); },
    test_webhook: function () { return Promise.resolve({ sent: true, status: 200 }); },
    test_ai_api: function () { return Promise.resolve({ ok: true }); },
    browse_maa_folder: function () { return Promise.resolve({ path: '' }); },
    wizard_scan_maa: function () { return Promise.resolve({ valid: true, maa_version: 'v6.17.5' }); },
    wizard_bind_maa: function () { return Promise.resolve({ ok: true }); },
    wizard_start_download_maa: function () { return Promise.resolve({ ok: true }); },
    wizard_cancel_download_maa: function () { return Promise.resolve({ ok: true }); },
    wizard_run_diagnostics: function () {
      return Promise.resolve({
        write_permission: { ok: true, reason: '通过' },
        maa_integrity: { ok: true, reason: '通过' },
        adb_resolution: { ok: true, connected: true, is_16_9: true, reason: '1280x720' }
      });
    },
    wizard_launch_emulator: function () { return Promise.resolve({ ok: true }); },
    wizard_finish: function () { return Promise.resolve({ ok: true }); },
    get_logs: function () { return Promise.resolve({ logs: [] }); },
    clear_logs: function () { return Promise.resolve({ ok: true }); },
    report_smoke_metrics: function () { return Promise.resolve({ ok: true }); }
  };

  var listeners = {};
  var pendingEvents = [];
  var isBatchScheduled = false;
  var isBridgeReady = false;
  var readyCallbacks = [];

  function getApi() {
    if (window.pywebview && window.pywebview.api) {
      return window.pywebview.api;
    }
    return fallbackMockApi;
  }

  function callBridgeMethod(methodName, args) {
    var api = getApi();
    if (typeof api[methodName] === 'function') {
      try {
        return Promise.resolve(api[methodName].apply(api, args || []));
      } catch (err) {
        return Promise.reject(err);
      }
    }
    console.warn('[Bridge] Method not found on API:', methodName);
    return Promise.reject(new Error('Method ' + methodName + ' not supported'));
  }

  function processPendingBatch() {
    isBatchScheduled = false;
    var batch = pendingEvents.slice();
    pendingEvents = [];
    for (var i = 0; i < batch.length; i++) {
      var item = batch[i];
      if (!item || !item.type) continue;
      var cbs = listeners[item.type];
      if (cbs && cbs.length) {
        for (var j = 0; j < cbs.length; j++) {
          try {
            cbs[j](item.payload);
          } catch (e) {
            console.error('[Bridge] Error in listener for', item.type, e);
          }
        }
      }
      var wildcard = listeners['*'];
      if (wildcard && wildcard.length) {
        for (var k = 0; k < wildcard.length; k++) {
          try {
            wildcard[k](item.type, item.payload);
          } catch (e2) {
            console.error('[Bridge] Error in wildcard listener', e2);
          }
        }
      }
    }
  }

  // Push batch interface called from Python via evaluate_js
  window.__prtsPushBatch = function (events) {
    if (!Array.isArray(events)) {
      events = [events];
    }
    for (var i = 0; i < events.length; i++) {
      pendingEvents.push(events[i]);
    }
    if (!isBatchScheduled) {
      isBatchScheduled = true;
      if (typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(processPendingBatch);
      } else {
        setTimeout(processPendingBatch, 0);
      }
    }
  };

  function onReady(cb) {
    if (isBridgeReady) {
      cb();
    } else {
      readyCallbacks.push(cb);
    }
  }

  function markReady() {
    if (isBridgeReady) return;
    isBridgeReady = true;
    for (var i = 0; i < readyCallbacks.length; i++) {
      try {
        readyCallbacks[i]();
      } catch (e) {
        console.error('[Bridge] Error in ready callback', e);
      }
    }
    readyCallbacks = [];
  }

  // Initialize handshake
  if (window.pywebview && window.pywebview.api) {
    markReady();
  } else {
    window.addEventListener('pywebviewready', function () {
      markReady();
    });
    // Fallback timer only if NOT inside EdgeChromium WebView2
    var isEdgeWebView = Boolean(window.chrome && window.chrome.webview);
    if (!isEdgeWebView) {
      setTimeout(function () {
        markReady();
      }, 150);
    }
  }

  var PrtsBridge = {
    onReady: onReady,
    isReady: function () { return isBridgeReady; },

    on: function (eventType, callback) {
      if (!listeners[eventType]) {
        listeners[eventType] = [];
      }
      listeners[eventType].push(callback);
      return function () {
        var idx = listeners[eventType].indexOf(callback);
        if (idx !== -1) {
          listeners[eventType].splice(idx, 1);
        }
      };
    },

    off: function (eventType, callback) {
      if (!listeners[eventType]) return;
      var idx = listeners[eventType].indexOf(callback);
      if (idx !== -1) {
        listeners[eventType].splice(idx, 1);
      }
    },

    // Method wrappers matching contract.FROZEN_BRIDGE_METHODS exactly
    getInitialState: function () { return callBridgeMethod('get_initial_state'); },
    refreshSnapshot: function (checkAdb) { return callBridgeMethod('refresh_snapshot', [Boolean(checkAdb)]); },
    startDaily: function () { return callBridgeMethod('start_daily'); },
    startAutoGrowth: function () { return callBridgeMethod('start_auto_growth'); },
    previewGrowthPlan: function () { return callBridgeMethod('preview_growth_plan'); },
    startAnnihilation: function () { return callBridgeMethod('start_annihilation'); },
    startPush: function (stagesRaw) { return callBridgeMethod('start_push', [String(stagesRaw || '')]); },
    startSyncBox: function () { return callBridgeMethod('start_sync_box'); },
    setRoguelikeMode: function (modeLabel) { return callBridgeMethod('set_roguelike_mode', [String(modeLabel || '')]); },
    startRoguelike: function (modeLabel) { return callBridgeMethod('start_roguelike', [String(modeLabel || '')]); },
    startRoguelikePostSettlement: function () { return callBridgeMethod('start_roguelike_post_settlement'); },
    toggleReclamation: function () { return callBridgeMethod('toggle_reclamation'); },
    stopCurrentTask: function () { return callBridgeMethod('stop_current_task'); },
    setServer: function (server) { return callBridgeMethod('set_server', [String(server || '')]); },
    setPostAction: function (postAction) { return callBridgeMethod('set_post_action', [String(postAction || '')]); },
    probeOrLaunchEmulator: function () { return callBridgeMethod('probe_or_launch_emulator'); },
    checkMaaUpdate: function () { return callBridgeMethod('check_maa_update'); },
    confirmMaaUpdate: function (releaseInfo, localVer, remoteVer) {
      return callBridgeMethod('confirm_maa_update', [releaseInfo, String(localVer || ''), String(remoteVer || '')]);
    },
    cancelMaaUpdate: function () { return callBridgeMethod('cancel_maa_update'); },
    getAgentPrompts: function () { return callBridgeMethod('get_agent_prompts'); },
    copyToClipboard: function (text) { return callBridgeMethod('copy_to_clipboard', [String(text || '')]); },
    openPath: function (target) { return callBridgeMethod('open_path', [String(target || '')]); },
    resumeWithRules: function () { return callBridgeMethod('resume_with_rules'); },
    resumeFromTutorial: function () { return callBridgeMethod('resume_from_tutorial'); },
    getSettings: function () { return callBridgeMethod('get_settings'); },
    saveSettings: function (payload) { return callBridgeMethod('save_settings', [payload]); },
    syncExplorationFromMaaGui: function (maaPath) { return callBridgeMethod('sync_exploration_from_maa_gui', [String(maaPath || '')]); },
    testWebhook: function (webhookUrl) { return callBridgeMethod('test_webhook', [String(webhookUrl || '')]); },
    testAiApi: function (baseUrl, apiKey, modelName) {
      return callBridgeMethod('test_ai_api', [String(baseUrl || ''), String(apiKey || ''), String(modelName || '')]);
    },
    browseMaaFolder: function () { return callBridgeMethod('browse_maa_folder'); },
    wizardScanMaa: function () { return callBridgeMethod('wizard_scan_maa'); },
    wizardBindMaa: function (folder) { return callBridgeMethod('wizard_bind_maa', [String(folder || '')]); },
    wizardStartDownloadMaa: function () { return callBridgeMethod('wizard_start_download_maa'); },
    wizardCancelDownloadMaa: function () { return callBridgeMethod('wizard_cancel_download_maa'); },
    wizardRunDiagnostics: function (emuType, adbAddress) {
      return callBridgeMethod('wizard_run_diagnostics', [String(emuType || ''), String(adbAddress || '')]);
    },
    wizardLaunchEmulator: function (emuType, adbAddress) {
      return callBridgeMethod('wizard_launch_emulator', [String(emuType || ''), String(adbAddress || '')]);
    },
    wizardFinish: function (payload) { return callBridgeMethod('wizard_finish', [payload]); },
    getLogs: function () { return callBridgeMethod('get_logs'); },
    clearLogs: function () { return callBridgeMethod('clear_logs'); },
    reportSmokeMetrics: function (metrics) { return callBridgeMethod('report_smoke_metrics', [metrics]); }
  };

  window.PrtsBridge = PrtsBridge;

  function inspectButtonRadiiAndCurves() {
    var selector = '.btn, .btn-stop-task, .nav-secondary-btn, .segmented-btn, .filter-chip';
    var elems = document.querySelectorAll(selector);
    var minRadius = 9999;
    var maxRadius = 0;
    var overshootViolations = [];
    var radiusViolations = [];
    var bezierRe = /cubic-bezier\(([^)]+)\)/g;

    for (var i = 0; i < elems.length; i++) {
      var el = elems[i];
      var cs = window.getComputedStyle(el);
      var radii = [
        parseFloat(cs.borderTopLeftRadius) || 0,
        parseFloat(cs.borderTopRightRadius) || 0,
        parseFloat(cs.borderBottomLeftRadius) || 0,
        parseFloat(cs.borderBottomRightRadius) || 0
      ];
      for (var r = 0; r < radii.length; r++) {
        if (radii[r] < minRadius) minRadius = radii[r];
        if (radii[r] > maxRadius) maxRadius = radii[r];
        if (radii[r] < 8 || radii[r] > 64) {
          radiusViolations.push({ id: el.id || el.className, radius: radii[r] });
        }
      }

      var tf = cs.transitionTimingFunction || '';
      var match;
      bezierRe.lastIndex = 0;
      while ((match = bezierRe.exec(tf)) !== null) {
        var nums = match[1].split(',').map(function (s) { return parseFloat(s.trim()); });
        if (nums.length === 4 && (nums[1] > 1.0 || nums[1] < 0.0 || nums[3] > 1.0 || nums[3] < 0.0)) {
          overshootViolations.push({ id: el.id || el.className, curve: match[0] });
        }
      }
    }

    return {
      ok: elems.length > 0 && radiusViolations.length === 0 && overshootViolations.length === 0,
      checked_count: elems.length,
      min_radius_px: elems.length > 0 ? minRadius : 0,
      max_radius_px: elems.length > 0 ? maxRadius : 0,
      radius_violations: radiusViolations,
      overshoot_violations: overshootViolations
    };
  }

  // Smoke Check Helper for verifying zero overflow, zero emoji, and page rendering
  window.__prtsRunSmokeCheck = function () {
    var results = {
      ready: true,
      pages_rendered: 0,
      visible_nav_items: 0,
      overflow_errors: 0,
      emoji_count: 0,
      stop_btn_right_gap: 0,
      nav_bottom_visible: false,
      drawer_visible: false,
      terminal_buttons_ok: false,
      segmented_active_ok: false,
      annihilation_text: '',
      annihilation_has_comma: false,
      console_errors: (window.__prtsConsoleErrors || []).slice(),
      console_error_count: (window.__prtsConsoleErrors || []).length,
      button_radius_check: null
    };

    try {
      // 1. Check Nav items
      var navItems = document.querySelectorAll('.nav-item');
      results.visible_nav_items = navItems.length;

      // 2. Count pages
      var pages = document.querySelectorAll('.page');
      results.pages_rendered = pages.length;

      // 3. Test horizontal overflow across all pages
      var originalActive = document.querySelector('.page.active');
      for (var p = 0; p < pages.length; p++) {
        var page = pages[p];
        var prevDisplay = page.style.display;
        page.style.display = 'flex';
        var scrollWidth = page.scrollWidth;
        var clientWidth = page.clientWidth;
        if (scrollWidth > clientWidth + 2) {
          results.overflow_errors++;
          console.warn('[SmokeCheck] Horizontal overflow detected on page', page.id, scrollWidth, clientWidth);
        }
        page.style.display = prevDisplay;
      }
      if (originalActive) {
        originalActive.style.display = '';
      }

      // 4. Test Emoji Regex across entire body textContent
      var bodyText = document.body ? document.body.textContent : '';
      var emojiRegex = /[\uD83C-\uDBFF\uDC00-\uDFFF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]/g;
      var matches = bodyText.match(emojiRegex);
      if (matches) {
        results.emoji_count = matches.length;
        console.warn('[SmokeCheck] Emoji characters detected in DOM:', matches);
      }

      // 5. Check Stop Button right gap
      var stopBtn = document.getElementById('btn-global-stop');
      var topBar = document.querySelector('.top-bar');
      if (stopBtn && topBar) {
        var topBarRect = topBar.getBoundingClientRect();
        var stopBtnRect = stopBtn.getBoundingClientRect();
        results.stop_btn_right_gap = Math.max(0, Math.round(topBarRect.right - stopBtnRect.right));
      }

      // 6. Check Nav rail bottom visibility
      var navBottom = document.querySelector('.nav-rail-bottom');
      if (navBottom) {
        var nbRect = navBottom.getBoundingClientRect();
        results.nav_bottom_visible = nbRect.height > 0 && nbRect.top < window.innerHeight && nbRect.bottom <= window.innerHeight + 5;
      }

      // 7. Check Bottom Drawer visibility
      var drawer = document.getElementById('bottom-log-drawer');
      if (drawer) {
        var drRect = drawer.getBoundingClientRect();
        results.drawer_visible = drRect.height > 0 && drRect.top < window.innerHeight;
      }

      // 8. Check Terminal buttons layout
      var termPage = document.getElementById('page-terminal');
      var bReports = document.getElementById('btn-open-reports');
      var bClear = document.getElementById('btn-clear-terminal');
      if (termPage && bReports && bClear) {
        var prevTermDisplay = termPage.style.display;
        termPage.style.display = 'flex';
        results.terminal_buttons_ok = (
          bReports.offsetWidth >= 70 && bReports.offsetHeight <= 48 &&
          bClear.offsetWidth >= 60 && bClear.offsetHeight <= 48
        );
        termPage.style.display = prevTermDisplay;
      }

      // 9. Check Segmented active state
      var segServerActive = document.querySelector('#seg-server .segmented-btn.active');
      var segPostActive = document.querySelector('#seg-post-action .segmented-btn.active');
      results.segmented_active_ok = Boolean(segServerActive && segPostActive);

      // 10. Check Annihilation text formatting
      var anniElem = document.getElementById('kpi-anni-progress');
      if (anniElem) {
        results.annihilation_text = (anniElem.textContent || '').trim();
        results.annihilation_has_comma = results.annihilation_text.indexOf(',') !== -1;
      }

      // 11. Check button radii & curves
      results.button_radius_check = inspectButtonRadiiAndCurves();
    } catch (err) {
      console.error('[SmokeCheck] Error running check:', err);
      results.ready = false;
    }

    return results;
  };

  window.__prtsRunSettingsTabsCheck = function () {
    var report = { ok: true, tabs_checked: 0, tabs: [] };
    try {
      var modal = document.getElementById('modal-settings');
      var openBtn = document.getElementById('btn-open-settings-modal');
      if (!modal || !openBtn) {
        return { ok: false, error: 'Missing modal-settings or btn-open-settings-modal', tabs_checked: 0, tabs: [] };
      }

      openBtn.click();
      var tabBtns = document.querySelectorAll('#settings-tabs-bar .modal-tab-btn');
      var allPanes = document.querySelectorAll('.settings-tab-pane');
      var modalBody = modal.querySelector('.modal-body');

      for (var i = 0; i < tabBtns.length; i++) {
        var btn = tabBtns[i];
        btn.click();
        var targetId = btn.getAttribute('data-tab');
        var targetPane = document.getElementById(targetId);
        var targetDisplay = targetPane ? window.getComputedStyle(targetPane).display : 'missing';
        var targetRect = targetPane ? targetPane.getBoundingClientRect() : { height: 0, width: 0 };

        var otherVisibleCount = 0;
        for (var p = 0; p < allPanes.length; p++) {
          if (allPanes[p].id !== targetId && window.getComputedStyle(allPanes[p]).display !== 'none') {
            otherVisibleCount++;
          }
        }

        var visibleControls = 0;
        if (targetPane) {
          var ctrls = targetPane.querySelectorAll('input:not([type="hidden"]), select, button, textarea, .switch-track');
          for (var c = 0; c < ctrls.length; c++) {
            var cr = ctrls[c].getBoundingClientRect();
            if (cr.width > 0 && cr.height > 0) {
              visibleControls++;
            }
          }
        }

        var overflowPx = modalBody ? Math.max(0, modalBody.scrollWidth - modalBody.clientWidth) : 0;
        var tabOk = Boolean(
          targetPane &&
          targetDisplay !== 'none' &&
          targetRect.height > 100 &&
          otherVisibleCount === 0 &&
          visibleControls >= 2 &&
          overflowPx <= 2
        );
        if (!tabOk) report.ok = false;

        report.tabs.push({
          id: targetId,
          ok: tabOk,
          display: targetDisplay,
          height: Math.round(targetRect.height),
          visible_controls: visibleControls,
          other_visible_panes: otherVisibleCount,
          overflow_px: overflowPx
        });
      }

      report.tabs_checked = report.tabs.length;
      if (report.tabs_checked !== 5) report.ok = false;

      if (tabBtns.length > 0) tabBtns[0].click();
      var closeBtn = modal.querySelector('[data-close="modal-settings"]');
      if (closeBtn) closeBtn.click();
    } catch (err) {
      report.ok = false;
      report.error = String(err);
    }
    return report;
  };

  window.__prtsRunThemeAndMotionCheck = function () {
    var res = {
      ok: true,
      palettes: {},
      dark_surface: '',
      light_surface: '',
      wizard_card_toggle_ok: false,
      alert_chip_ok: false,
      radius_check: null
    };
    try {
      if (typeof window.__prtsSetTheme === 'function') {
        var pals = ['sky', 'sage', 'lavender', 'sand'];
        var uniquePrimaries = {};
        for (var i = 0; i < pals.length; i++) {
          window.__prtsSetTheme(pals[i], 'light');
          var cs = window.getComputedStyle(document.documentElement);
          var pri = cs.getPropertyValue('--md-sys-color-primary').trim();
          res.palettes[pals[i]] = pri;
          uniquePrimaries[pri] = true;
        }
        window.__prtsSetTheme('sky', 'light');
        res.light_surface = window.getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-surface').trim();
        window.__prtsSetTheme('sky', 'dark');
        res.dark_surface = window.getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-surface').trim();
        window.__prtsSetTheme('sky', 'light');

        if (Object.keys(uniquePrimaries).length !== 4 || !res.dark_surface || res.dark_surface === res.light_surface) {
          res.ok = false;
        }
      } else {
        res.ok = false;
      }

      // Verify Wizard Step 3 card selection toggles .card-highlight without side effects
      var cardAgent = document.getElementById('wizard-card-agent');
      var cardApi = document.getElementById('wizard-card-api');
      if (cardAgent && cardApi) {
        cardApi.click();
        var apiHighlighted = cardApi.classList.contains('card-highlight') && !cardAgent.classList.contains('card-highlight');
        cardAgent.click();
        var agentRestored = cardAgent.classList.contains('card-highlight') && !cardApi.classList.contains('card-highlight');
        res.wizard_card_toggle_ok = apiHighlighted && agentRestored;
        if (!res.wizard_card_toggle_ok) res.ok = false;
      }

      // Verify Terminal alert chip exists and toggles active
      var alertChip = document.querySelector('#terminal-filter-chips [data-tag="alert"]');
      var allChip = document.querySelector('#terminal-filter-chips [data-tag="all"]');
      if (alertChip && allChip) {
        alertChip.click();
        var alertActive = alertChip.classList.contains('active');
        allChip.click();
        res.alert_chip_ok = alertActive && allChip.classList.contains('active');
        if (!res.alert_chip_ok) res.ok = false;
      }

      res.radius_check = inspectButtonRadiiAndCurves();
      if (!res.radius_check.ok) res.ok = false;
    } catch (err) {
      res.ok = false;
      res.error = String(err);
    }
    return res;
  };

  window.__prtsStartRadiusTransitionSample = function () {
    window.__prtsRadiusSampleResult = null;
    try {
      var chip = document.querySelector('#terminal-filter-chips [data-tag="alert"]');
      var allChip = document.querySelector('#terminal-filter-chips [data-tag="all"]');
      if (!chip || !allChip) {
        window.__prtsRadiusSampleResult = { ok: false, error: 'Missing filter chips' };
        return true;
      }
      var samples = [];
      var start = performance.now();
      chip.click();

      function tick() {
        var cs = window.getComputedStyle(chip);
        var r = parseFloat(cs.borderTopLeftRadius) || 0;
        samples.push(Math.round(r * 100) / 100);
        if (performance.now() - start < 240) {
          window.requestAnimationFrame(tick);
        } else {
          allChip.click();
          var minR = Math.min.apply(null, samples);
          var maxR = Math.max.apply(null, samples);
          window.__prtsRadiusSampleResult = {
            ok: samples.length > 0 && minR >= 8 && maxR <= 64,
            frames: samples.length,
            min_radius_px: minR,
            max_radius_px: maxR
          };
        }
      }

      window.requestAnimationFrame(tick);
      return true;
    } catch (err) {
      window.__prtsRadiusSampleResult = { ok: false, error: String(err) };
      return false;
    }
  };

})(window);
