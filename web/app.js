const state = {
  occasion: { value: "birthday", label: "生日" },
  relation: { value: "friend", label: "朋友" },
  // 收礼人偏向：value 为空 = 不指定（默认）。不指定时不做任何性别推断。
  recipientGender: { value: "", label: "不指定" },
  giftMode: { value: "purchased", label: "购买礼物" },
  giftModes: ["purchased"],
  result: null,
  requestPayload: null,
  difyJobId: null,
  pageSize: 6,
  pages: { purchased: 1, diy: 1 },
  selected: { purchased: null, diy: null },
  // 每次新结果的自增戳：选择清空只在「同一次结果的首次渲染」发生一次
  resultStamp: 0,
  selectionClearedPages: {},
  activeView: "basicView",
  lastGiftView: "basicView",
  lastWizardActive: false,
  lastWizardStep: 1,
};

const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8787" : "";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

// 当前向导步骤。2026-11-19：提到顶部声明，因为 trailSync()（第 123 行）
// 要读它做高亮，而 trailSync 在第 216 行就会被调用 —— 声明留在 1270 行
// 会触发 let 的暂时性死区（ReferenceError），整个脚本停在首屏。
let wizardStep = 1;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[char]));
}

// P2 文本排版：模型输出经常带 `**加粗**`、内部枚举（avoid_fragrance）等
// 机器痕迹。流式渲染用 textContent（不解析 HTML），这里做纯文本清理：
// 去掉 markdown 记号、把内部枚举值转成用户能懂的中文。只处理展示文本，
// 不解析任何 HTML/链接，保持注入安全。
// 2026-11-17：原来的免责声明是一整句公文——「以下提醒结合了知识库条目和通用
// 送礼常识；地方习俗有家庭差异，最终以收礼人的习惯为准」。信息只有最后半句
// 有用，前半句是在说明这套提醒怎么来的，用户不关心。统一压成一句人话。
function shortDisclaimer(raw) {
  const text = String(raw || "").trim();
  if (!text) return "";
  return "各地讲究不一样，最后还是看收礼人的习惯。";
}

function cleanChatText(value) {
  let text = String(value ?? "");
  text = text.replace(/[#*_>`~]/g, "");
  text = text.replace(/avoid_fragrance|no_fragrance|fragrance_free/gi, "避免香味");
  text = text.replace(/non_alcoholic|avoid_alcohol/gi, "避免酒精");
  text = text.replace(/avoid_nuts|allergy_free/gi, "避免坚果");
  text = text.replace(/accepts_fragrance_diy/gi, "接受香味类手作");
  text = text.replace(/hard_preferences/gi, "硬性要求");
  text = text.replace(/gift_mode/gi, "礼物类型");
  // 2026-11-17：模型常在开头自述检索状态。同一个意思它每次换个说法——
  // 「当前未提供可引用的具体知识库条目，以下为一般建议」「当前没有提供收礼
  // 地区…以下为一般建议，不代表知识库中的具体条目」。整句匹配的正则抓不全，
  // 改成按句子过滤：首段里凡是「在解释这套内容怎么来的」句子一律丢掉，
  // 只留有实际内容的句子。后面几段是正文，不动。
  const META = [
    /^(当前|目前)?(未|没有)(提供|找到|检索到)/,
    /不代表知识库/,
    /知识库中的具体条目/,
    /以下为一般建议/,
    /^(以上|以下)(内容|建议)?(仅|只)供参考/,
  ];
  const isMeta = (sentence) => META.some((re) => re.test(sentence));
  const paras = text.split(/\n/);
  if (paras.length) {
    const kept = paras[0]
      .split(/(?<=[。；;])/)
      .filter((sentence) => sentence.trim() && !isMeta(sentence.trim()))
      .join("");
    if (kept.trim()) {
      paras[0] = kept;
    } else {
      paras.shift();
    }
    text = paras.join("\n");
  }
  text = text.replace(/\s*\n{3,}/g, "\n\n");
  return text.trim();
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function selectChoice(container, target) {
  $$(container + " .choice").forEach((button) => button.classList.remove("selected"));
  target.classList.add("selected");
  return { value: target.dataset.value, label: target.dataset.label };
}

// ── 场景先行（v7）────────────────────────────────────────
// 首页三个场景入口只映射「向导本来就能确定的场合与关系」，不猜预算、兴趣、
// 地区或禁忌（设计稿第 3 节明确要求）。第四个入口进入空白向导。
const SCENE_STEPS = {
  1: {
    question: "这份礼物，<br>什么时候送？",
    help: "生日和答谢，挑法不一样。",
  },
  2: {
    question: "送给谁？",
    help: "送朋友和送长辈，分寸差得远。",
  },
  3: {
    question: "买现成的，<br>还是自己做？",
    help: "买的看预算，做的看材料费。",
  },
  4: {
    question: "想送个什么意思？",
    help: "说一句就行。不想说也没关系，我按前面挑。",
  },
};

// 左侧「这次的线索」：实时把已确定的条件串成一行，作为导航信息。
// 左侧「记下了」逐项回显：四步各占一行，显示已填的值。
// 2026-11-19：原来是把所有信息挤成一行（「朋友 · 生日 · 30–300 元」），
// 用户翻到第 4 步就看不到前面填了什么。改成逐项列表，回看一步一眼。
function trailSync() {
  const list = $("#trailList");
  const node = $("#trailText");
  // 逐项回显的每一行文案。空值一律显示「还没填」，不用占位符假装有内容。
  const rows = {};

  // 第 1 步：场合（选了具体说法就带上）
  const occasionCustom = ($("#occasionCustom")?.value || "").trim();
  rows[1] = occasionCustom
    ? `${state.occasion?.label || "未选"} · ${occasionCustom}`
    : state.occasion?.label || "还没填";

  // 第 2 步：对象 + 性别倾向 + 避开项
  const relationCustom = ($("#relationCustom")?.value || "").trim();
  const relationText = relationCustom
    ? `${state.relation?.label || "未选"} · ${relationCustom}`
    : state.relation?.label || "未选";
  const genderText = state.recipientGender?.label || "不指定";
  const avoid = $$("#preferenceChoices input:checked").map((input) =>
    input.parentElement.textContent.trim()
  );
  const parts2 = [relationText, genderText];
  if (avoid.length) parts2.push(`避开${avoid.join("、")}`);
  rows[2] = parts2.join(" · ");

  // 第 3 步：方式 + 预算
  const min = $("#budgetMin")?.value;
  const max = $("#budgetMax")?.value;
  const modeText =
    state.giftModes.length === 2
      ? "成品 + 手作"
      : state.giftModes[0] === "diy"
        ? "亲手做"
        : "买现成的";
  const budgetText = min || max ? `${min || "0"}–${max || "不限"} 元` : "预算未填";
  rows[3] = `${modeText} · ${budgetText}`;

  // 第 4 步：心意 + 千万别送的
  const freeText = ($("#freeText")?.value || "").trim();
  const avoidCustom = ($("#avoidCustom")?.value || "").trim();
  const parts4 = [];
  if (freeText) parts4.push(freeText.length > 18 ? `${freeText.slice(0, 18)}…` : freeText);
  // 忌口不加「忌：」前缀。右栏标签已经写着「有什么千万别送的」，
  // 用户自己也多半会写「不要…」，再加前缀就变成「忌：不要香薰」这种重话。
  if (avoidCustom) parts4.push(avoidCustom.length > 18 ? `${avoidCustom.slice(0, 18)}…` : avoidCustom);
  rows[4] = parts4.length ? parts4.join(" · ") : "还没填";

  // 写入 DOM；空值的行弱化，当前步高亮
  if (list) {
    $$("[data-trail-value]").forEach((el) => {
      const step = Number(el.dataset.trailValue);
      const text = rows[step] || "还没填";
      el.textContent = text;
      // 「还没填」是空态，不是内容，单独标出来好让样式弱化它
      el.classList.toggle("is-empty", text === "还没填");
    });
    $$("[data-trail]").forEach((el) => {
      const step = Number(el.dataset.trail);
      el.classList.toggle("is-current", step === wizardStep);
      el.classList.toggle("is-done", step < wizardStep);
    });
  }

  // 老的单行版本留作兜底（万一 #trailList 不存在）
  if (node && !list) {
    const parts = [];
    if (state.relation?.label) parts.push(state.relation.label);
    if (state.occasion?.label) parts.push(state.occasion.label);
    if (min || max) parts.push(`${min || "0"}–${max || "不限"} 元`);
    if (state.giftModes.length === 2) parts.push("成品 + 手作");
    else if (state.giftModes[0] === "diy") parts.push("亲手做");
    if (avoid.length) parts.push(`避开${avoid.join("、")}`);
    node.textContent = parts.length ? parts.join(" · ") : "还没开始填";
  }
}

function applyScene(button) {
  const presetKey = button.dataset.preset;
  if (presetKey && typeof applyPreset === "function") {
    applyPreset(presetKey);
    $$("#sceneEntries .scene").forEach((item) => item.classList.toggle("active", item === button));
    return;
  }
  const { occasion, occasionLabel, relation, relationLabel } = button.dataset;
  if (occasion) {
    state.occasion = { value: occasion, label: occasionLabel };
    $$("#occasionChoices .choice").forEach((item) => {
      item.classList.toggle("selected", item.dataset.value === occasion);
    });
  }
  if (relation) {
    state.relation = { value: relation, label: relationLabel };
    $$("#relationChoices .choice").forEach((item) => {
      item.classList.toggle("selected", item.dataset.value === relation);
    });
  }
  // 即时回声：只回显用户刚选的这两个字段，不编造预算或兴趣
  const echo = $("#sceneEcho");
  if (echo) echo.textContent = [relationLabel, occasionLabel].filter(Boolean).join(" · ");
  $$("#sceneEntries .scene").forEach((item) => item.classList.toggle("active", item === button));
  trailSync();
  // 2026-11-17：示例条是首屏「结果会像这样」的说明，选了场景进了向导就没用了。
  // 但它在页面里占 160px，滚到底时会从页头下露出 59px，被切成一条深色横杠。
  // 进向导就收起来。
  document.body.classList.add("wizard-active");
  scrollToWizard();
}

$$("#sceneEntries .scene").forEach((button) => {
  button.addEventListener("click", () => applyScene(button));
});

function setView(viewId, options = {}) {
  // 如果离开挑礼物页面前往顾问页，记住当前的挑礼物视图与向导进度
  if (state.activeView !== "chatView" && viewId === "chatView") {
    state.lastGiftView = state.activeView;
    state.lastWizardActive = document.body.classList.contains("wizard-active");
    state.lastWizardStep = wizardStep;
  }

  state.activeView = viewId;
  $$(".workspace-view").forEach((view) => view.classList.toggle("active", view.id === viewId));
  // 顶栏导航高亮：结果页都算「挑礼物」
  $$(".top-link").forEach((item) => item.classList.toggle("active",
    item.dataset.view === viewId
    || (viewId === "purchasedView" && item.dataset.view === "basicView")
    || (viewId === "diyView" && item.dataset.view === "basicView")));

  if (viewId === "basicView") {
    if (options.resetWizard) {
      document.body.classList.remove("wizard-active");
      wizardStep = 1;
      wizardSync({ initial: true });
    } else if (state.lastWizardActive) {
      document.body.classList.add("wizard-active");
      wizardStep = state.lastWizardStep || wizardStep || 1;
      wizardSync({ initial: true });
    }
  }
  if (viewId === "purchasedView") renderCategoryPage("purchased");
  if (viewId === "diyView") renderCategoryPage("diy");
  if (viewId === "chatView") syncChatContext();

  // 若离开结果页（回到首页或顾问页），则隐藏底部操作条
  const activeType = viewId === "purchasedView" ? "purchased" : (viewId === "diyView" ? "diy" : null);
  if (activeType) {
    const selId = state.selected?.[activeType];
    const candidate = (state.result?.candidates || []).find((c) => c.product_id === selId);
    updateSelectionDock(candidate, activeType);
  } else {
    updateSelectionDock(null, null);
  }

  window.scrollTo({ top: 0, behavior: "smooth" });
}

// 智能返回挑礼物页面（不冲刷已有结果或正在填写的向导步骤）
function navigateToGiftView(forceEdit = false) {
  if (forceEdit) {
    state.lastGiftView = "basicView";
    state.lastWizardActive = true;
    setView("basicView");
    document.body.classList.add("wizard-active");
    wizardStep = 1;
    wizardSync();
    return;
  }

  // 1. 如果已有推荐结果，直接回到结果页（成品或自制）
  if (state.result && state.result.candidates && state.result.candidates.length > 0) {
    const targetView = (state.lastGiftView === "diyView") ? "diyView" : "purchasedView";
    setView(targetView);
    return;
  }

  // 2. 如果之前正在填写向导阶段，恢复之前的向导步数
  if (state.lastWizardActive || document.body.classList.contains("wizard-active")) {
    setView("basicView");
    document.body.classList.add("wizard-active");
    wizardStep = state.lastWizardStep || wizardStep || 1;
    wizardSync({ initial: true });
    return;
  }

  // 3. 初始未开始状态
  setView("basicView");
}

// 顾问视图：同步当前上下文信息到指示条
function syncChatContext() {
  const context = buildChatContext();
  const summaryEl = $("#chatContextSummary");
  if (!summaryEl) return;
  const parts = [];
  if (context.occasion) parts.push(`场合：${context.occasion}`);
  if (context.recipient_relation) parts.push(`对象：${context.recipient_relation}`);
  if (context.budget) parts.push(`预算：${context.budget}`);
  const selGift = context.selected_gift || context.selected_diy;
  if (selGift) {
    parts.push(`已选礼物：${selGift.title} (${selGift.price || "未标价"})`);
  } else {
    parts.push("已选礼物：未指定（可在结果页点选）");
  }
  summaryEl.textContent = parts.join(" ｜ ");
}

// 2026-11-17：滚到向导顶部。粘性页头会盖住向导第一行（实测 76px），
// 所以要留出页头高度；但页面可能不够高、滚到底也到不了，这时就别硬加偏移，
// 否则顶部会多出一段空白。
function scrollToWizard() {
  const anchor = $("#wizardAnchor");
  if (!anchor) return;
  requestAnimationFrame(() => {
    const header = document.querySelector(".topbar");
    const headerH = header ? header.getBoundingClientRect().height : 0;
    const maxScroll = Math.max(
      0, document.documentElement.scrollHeight - window.innerHeight);
    const wanted = window.scrollY + anchor.getBoundingClientRect().top - headerH;
    // 想要的落点超出了可滚范围：说明页面撑不到，退而求其次用最大值，
    // 但不额外留白（此时页头下面自然就是向导的第一行）。
    const target = wanted > maxScroll ? maxScroll : wanted;
    window.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
  });
}

// 首页 hero 的「开始挑礼物」：滚到向导区（不切视图，向导就在 basicView 里）
$$("[data-scroll]").forEach((item) => item.addEventListener("click", () => {
  const target = document.querySelector(item.dataset.scroll);
  if (target) {
    // 同 applyScene：进向导就收起首屏示例带，再按页头高度对齐
    if (item.dataset.scroll === "#wizardAnchor") {
      document.body.classList.add("wizard-active");
      scrollToWizard();
      return;
    }
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}));

// ── 开场拆礼：同会话只播一次；reduced-motion 直接不渲染（CSS display:none）──
// 不能阻挡表单状态恢复和页面交互：intro 层永远不拦截 pointer（off 后才隐藏）。
(function initIntro() {
  const intro = $("#introOverlay");
  if (!intro) return;
  // reduced-motion 下 CSS 已把 .intro display:none，这里只管会话内只播一次
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    intro.remove();
    return;
  }
  let played = false;
  try { played = window.sessionStorage.getItem("gr_intro_played") === "1"; } catch { played = false; }
  
  // 启动开场全屏像素粒子星空
  const introCanvas = $("#introCanvas");
  let introParticleStop = null;
  if (introCanvas && !played) {
    introParticleStop = startIntroParticles(introCanvas);
  }

  const close = (triggerBurst = false) => {
    intro.style.pointerEvents = "none";
    if (triggerBurst && window.triggerCustomPixelBurst) {
      const rect = $("#introEnterBtn")?.getBoundingClientRect();
      const bx = rect ? rect.left + rect.width / 2 : window.innerWidth / 2;
      const by = rect ? rect.top + rect.height / 2 : window.innerHeight / 2;
      window.triggerCustomPixelBurst(bx, by, 48);
    }
    intro.classList.add("off");
    try { window.sessionStorage.setItem("gr_intro_played", "1"); } catch { /* 私密模式忽略 */ }
    window.setTimeout(() => {
      if (introParticleStop) introParticleStop();
      intro.remove();
    }, 600);
    // Part 2 动效接力：开场结束 → 首屏四段入场（同会话只此一次）
    document.body.classList.add("hero-in");
  };
  if (played) { intro.remove(); document.body.classList.add("hero-in"); return; }
  intro.removeAttribute("aria-hidden");
  
  // 封面开场自动定时：4.5s 兜底自动收起，点击任意按钮或背景立即进入
  const timer = window.setTimeout(() => close(false), 4500);
  $("#introSkip")?.addEventListener("click", () => { window.clearTimeout(timer); close(false); });
  $("#introEnterBtn")?.addEventListener("click", (e) => { 
    e.stopPropagation();
    window.clearTimeout(timer); 
    close(true); 
  });
  intro.addEventListener("click", (event) => {
    if (event.target === intro || event.target.classList.contains("intro-backdrop")) {
      window.clearTimeout(timer);
      close(false);
    }
  });

  function startIntroParticles(canvas) {
    const ctx = canvas.getContext("2d");
    if (!ctx) return () => {};
    let w = canvas.width = window.innerWidth;
    let h = canvas.height = window.innerHeight;
    let raf = 0;
    let active = true;

    const colors = ["#F59E0B", "#FCD34D", "#DC2626", "#10B981", "#60A5FA", "#FFFFFF"];
    const particles = Array.from({ length: 42 }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      size: Math.random() > 0.65 ? 4 : 2,
      vy: -(0.25 + Math.random() * 0.55),
      vx: (Math.random() - 0.5) * 0.35,
      color: colors[Math.floor(Math.random() * colors.length)],
      alpha: 0.2 + Math.random() * 0.6,
      twinkleSpeed: 0.02 + Math.random() * 0.03
    }));

    function loop() {
      if (!active) return;
      ctx.clearRect(0, 0, w, h);
      for (const p of particles) {
        p.y += p.vy;
        p.x += p.vx;
        if (p.y < -10) p.y = h + 10;
        if (p.x < -10) p.x = w + 10;
        if (p.x > w + 10) p.x = -10;
        p.alpha += Math.sin(Date.now() * p.twinkleSpeed) * 0.015;
        const a = Math.max(0.12, Math.min(0.9, p.alpha));
        ctx.fillStyle = p.color;
        ctx.globalAlpha = a;
        ctx.fillRect(Math.round(p.x), Math.round(p.y), p.size, p.size);
      }
      ctx.globalAlpha = 1;
      raf = requestAnimationFrame(loop);
    }
    loop();

    const onResize = () => {
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
    };
    window.addEventListener("resize", onResize);

    return () => {
      active = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
    };
  }
})();

function collectPayload() {
  return {
    occasion: state.occasion.value,
    occasion_label: state.occasion.label,
    occasion_custom: $("#occasionCustom").value.trim(),
    recipient_relation: state.relation.value,
    relation_label: state.relation.label,
    relation_custom: $("#relationCustom").value.trim(),
    // 收礼人偏向（male / female / 空=不指定）。空字符串表示用户没选，
    // 后端与 Dify 提示词都按「不指定」处理，不做任何性别推断。
    recipient_gender: state.recipientGender.value || "",
    budget_min: Number($("#budgetMin").value) || null,
    budget_max: Number($("#budgetMax").value) || null,
    hard_preferences: $$("#preferenceChoices input:checked").map((input) => input.value),
    soft_preferences: [],
    avoid_custom: $("#avoidCustom").value.trim(),
    free_text: $("#freeText").value.trim(),
    recipient_region: $("#recipientRegion").value.trim(),
    user_region: $("#userRegion").value.trim(),
    gift_mode: state.giftModes.length === 2 ? "all" : state.giftModes[0],
    accepts_food_diy: $("#acceptFoodDiy").checked,
    accepts_fragrance_diy: $("#acceptFragranceDiy").checked,
  };
}

// 结果区状态条（loading / error 共用一处），替代原来的 window.alert。
// 同时写表单页的 #formStatus —— 提交时用户正停在表单页，结果区还是隐藏的，
// 只在结果区显示 loading 等于没显示。
// mode: null 隐藏 | "loading" | "error"
function setResultStatus(mode, html) {
  ["formStatus", "purchasedStatus", "diyStatus"].forEach((id) => {
    const node = $(`#${id}`);
    if (!node) return;
    node.className = "result-status" + (mode ? ` is-${mode}` : " hidden");
    node.innerHTML = mode ? html : "";
  });
}

// 「AI 精选」的唯一判定：后端明确标了 is_llm_copy，且确有模型文案。
// 以前用 `is_llm_copy !== false` 当默认真，后端一旦漏标或模板补空，
// 规则卡就会混进精选区并顶着「由模型根据你的场景挑出」的说明。
function isAiCard(card) {
  return card.is_llm_copy === true
    && Boolean(String(card.meaning || "").trim() || String(card.why_this || "").trim());
}

// 卡脚左侧只放真警告（琥珀色）；过程性兜底文案已删——soft_warnings 实测
// 恒为空，页底三条统一提醒覆盖了同样的口径。注意：renderCard 里是模板
// 字符串，注释必须写在函数体里，不能写 {/\*...\*/}（那是 JSX 语法，
// 纯 JS 会原样渲染给用户——11-13 晚用户截图抓到过）。
function renderCardFooterWarning(card) {
  return card.soft_warnings?.length
    ? `<span class="warning">${escapeHtml(card.soft_warnings.join("；"))}</span>`
    : "";
}

function renderCard(card) {
  const isDiy = card.gift_type === "diy";
  const type = isDiy ? "diy" : "purchased";
  // Market Ready：初始无选中（用户主动点才进入比较/选择）
  const selected = state.selected[type] === card.product_id;
  // 三态判定（Market Ready P0-1）：AI 有理由 → 置顶卡；只有事实筛选依据 →
  // 普通备选卡（不叫"最贴合/AI 精选"）；连可核实的依据都没有 → 资料不足。
  const isLlmCard = card.is_llm_copy === true;
  const meaningText = String(card.meaning || "").trim();
  const whyText = String(card.why_this || "").trim();
  const hasReason = Boolean(meaningText || whyText);
  const hasBasis = hasReason || Boolean(String(card.watch_out || "").trim())
    || (card.taboo_notes || []).length > 0;
  const cardState = isLlmCard && hasReason ? "ai" : (hasBasis ? "basis" : "insufficient");
  const aiFlag = cardState === "ai" ? " is-ai-card" : "";
  // 卡片信息层级：标签 → 商品名 → 推荐理由 → 参考价/留意。
  // 没有理由时只说可核实的筛选依据，不冒充"最贴合"。
  // 2026-11-15：AI 卡从 5 张涨到 8 张，8 张全挂「最贴合这次送礼」等于没有区分度。
  // 只给第 1 名留这句，其余按模型给的 strategy 说话。
  const isTopPick = cardState === "ai" && Number(card.rank) === 1;
  const strategyLabel = {
    safe: "送这个不会错",
    heart: "心意到位",
    special: "有点小心思",
  }[card.strategy] || "也可以";
  // 2026-11-17：原来这里写「资料不足，暂不推荐」，但同一张卡下面的按钮是
  // 「选择这件」（见 actionLabel 处的说明：卡就是可选）。一个标签说别选、
  // 一个按钮让你选，自相矛盾。只保留可核实的事实「资料不足」，
  // 不替用户下「别选」的判断——那是用户自己的决定。
  const label = cardState === "ai"
    ? (isTopPick ? "我最推荐这件" : strategyLabel)
    : (cardState === "basis" ? (card.strategy_label || "也可以") : "资料不全");
  const priceText = card.gift_type === "diy"
    ? (card.cost || card.price_band || card.price || "材料费未标注")
    : (card.price_band || card.price || "价格未标注");
  const priceLabel = card.gift_type === "diy" ? "材料费估算" : "参考价";
  // 修复双重转义：先收集原文，最后一次转义（此前 push 已转义文本、
  // join 后再 escapeHtml 一次，& 和引号会变成 &amp; 等实体字符）
  // 标点：知识库的 note 自带句号，直接拼会拼出「。；」。每段先去掉末尾
  // 标点，再用统一的分号连接，最后补一个句号收尾。
  const stripTail = (text) => String(text || "").trim().replace(/[。；;，,、\s]+$/, "");
  const watchParts = [];
  if (card.watch_out) watchParts.push(stripTail(card.watch_out));
  if ((card.taboo_notes || []).length) {
    watchParts.push(card.taboo_notes.map((item) => `${item.keyword}：${stripTail(item.note)}`).join("；"));
  }
  if (card.soft_warnings?.length) {
    watchParts.push(card.soft_warnings.map(stripTail).filter(Boolean).join("；"));
  }
  const watchJoined = watchParts.filter(Boolean).join("；");
  const watchText = watchJoined ? `${watchJoined}。` : "";
  const reasonText = meaningText || whyText || String(card.description || "").trim();
  const reasonHtml = reasonText ? `<p class="card-reason">${escapeHtml(reasonText)}</p>` : "";
  const secondaryHtml = (meaningText && whyText)
    ? `<p class="card-why">${escapeHtml(whyText)}</p>`
    : "";
  // 参考价与「留意」分成两块：原先塞在同一个 flex 行里，留意文字一长就被
  // 挤到第二行、把整块撑高，两张卡内容长短不同时高度差得很明显。现在参考价
  // 单独一行（分隔线紧跟内容），留意另起一块，有自己的左标线。
  const priceHtml = `<div class="card-price-row"><span class="card-price-label">${escapeHtml(priceLabel)}</span> <b>${escapeHtml(priceText)}</b></div>`;
  const watchHtml = watchText
    ? `<div class="detail-watch"><em>留意</em><span>${escapeHtml(watchText)}</span></div>`
    : "";
  // 按钮文案必须和点击后的实际行为一致：点任何一张卡的按钮都会选中它
  // （handler 里统一写 state.selected），所以不能因为「资料不足」就把文案
  // 写成「查看资料」——那会让用户以为点开是看详情，实际是选了它。
  // 资料不足的卡仍然可选（用户可能就是想要这一件），只是文案要说实话。
  const actionLabel = isDiy ? "看怎么做" : "就它了";
  const diyNote = isDiy ? '<span class="diy-link-note">非材料购买链接</span>' : "";
  return `
    <article class="gift-card${aiFlag} ${escapeHtml(card.strategy)} ${selected ? "is-selected" : ""}" data-card-id="${escapeHtml(card.product_id)}" data-card-type="${type}">
      <span class="card-label">${escapeHtml(label)}</span>
      <h3>${escapeHtml(card.display_title || card.title)}</h3>
      ${reasonHtml}
      ${secondaryHtml}
      ${diyDetailsHtml(card, isDiy)}
      <div class="card-details">
        ${priceHtml}
        ${watchHtml}
      </div>
      <div class="card-footer">
        <button type="button" class="select-card-button ${selected ? "selected" : ""}" data-select-card="${escapeHtml(card.product_id)}" data-select-type="${type}">${selected ? "已定" : actionLabel}</button>
        <button type="button" class="letter-card-button" data-letter-card="${escapeHtml(card.product_id)}" data-card-title="${escapeHtml(card.display_title || card.title || "")}" title="查看附赠手写贺卡">💌 手写贺卡</button>
        ${diyNote}
      </div>
    </article>
  `;
}

function diyDetailsHtml(card, isDiy) {
  if (!isDiy) return "";
  // P1 手作折叠：材料/步骤默认收起，卡片只留「适合理由 + 材料费 + 留意」，
  // 用户要比较时再展开。保存信息跟着步骤一起进折叠层。
  return `
    <details class="diy-details">
      <summary>材料与制作步骤</summary>
      <div class="detail-block"><strong>材料</strong><p>${escapeHtml((card.materials || []).join("、") || "没写")}</p></div>
      <div class="detail-block"><strong>步骤</strong><ol>${(card.steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol></div>
      <div class="detail-block"><strong>保存</strong><p>${escapeHtml(card.preservation || "按你实际用的材料")}</p></div>
    </details>
  `;
}

function renderPageNotices(results) {
  const notices = results?.page_notices || [];
  const html = notices.length
    ? `<strong>买之前，我提醒你几句</strong><ul>${notices.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : "";
  // 两个视图（购买/自制）各自有容器，同一个提醒两处都要出现。
  for (const id of ["#pageNotices", "#diyPageNotices"]) {
    const target = $(id);
    if (!target) continue;
    target.classList.toggle("hidden", !notices.length);
    target.innerHTML = html;
  }
}

// 没有候选时占满整行的空态面板：说清为什么空、下一步能做什么，
// 而不是留一片白让用户以为页面坏了。
function renderEmptyState(type) {
  const label = type === "purchased" ? "礼物" : "手作方案";
  return `
    <div class="empty-panel">
      <h3>这个条件下，我确实没挑到合适的${label}</h3>
      <p>不是你要求高——条件卡这么死，能同时满足的本来就少。要不这样：</p>
      <ul>
        <li>预算往上放一点，好东西往往就差这一档。</li>
        <li>「千万别送」里放开一两条，选择立刻多。</li>
        <li>买的和做的都看看，有些心意自己做才够。</li>
      </ul>
      <div class="empty-actions">
        <button type="button" class="button secondary" data-go="basicView">修改条件</button>
      </div>
    </div>
  `;
}

function renderCategoryPage(type) {
  const candidates = (state.result?.candidates || []).filter((card) => card.gift_type === type);
  const pageKey = type;
  const cardsId = type === "purchased" ? "purchasedCards" : "diyCards";
  const paginationId = type === "purchased" ? "purchasedPagination" : "diyPagination";
  const titleId = type === "purchased" ? "purchasedTitle" : "diyTitle";
  const rangeId = type === "purchased" ? "purchasedPageRange" : "diyPageRange";
  // 两段而不是三段：本地规则卡一律进「更多选项」，不再单独拆「备选礼物」。
  // 理由是「备选礼物」要成立必须每张都有可核实依据（留意/禁忌提示），而
  // 4180 件目录里带 risk_notes 的太少——实测 20 张里只凑得出 4 张。为了凑
  // 数量给没依据的卡编文案，就是之前修掉的「模板文案冒充模型文案」。
  // 所以按用户的意思：卡上有东西就显示，没有就只显示价格，统一沉到更多选项。
  //
  // 2026-11-17：分页只切「更多选项」，AI 精选整段固定在顶部。
  // 原来先合并再按 6 件切片，AI 精选有 8 张时第 1 页被精选占满、
  // 「更多选项」一个都不出现，第 2 页又接不上精选——用户看到的是
  // 「只有 AI 精选」或「精选被拆成两半」，跟「两个分区」的设计对不上。
  const allAiCards = candidates.filter(isAiCard);
  const allOtherCards = candidates.filter((card) => !isAiCard(card));
  const pageCount = Math.max(1, Math.ceil(allOtherCards.length / state.pageSize));
  state.pages[pageKey] = Math.min(Math.max(1, state.pages[pageKey]), pageCount);
  const start = (state.pages[pageKey] - 1) * state.pageSize;
  // Market Ready：不自动替用户选中第一件。只在「新结果首次渲染本页」时清空，
  // 用户点选触发的重渲染、翻页、AI 更新都不清——否则点击会被自己的重渲染吞掉。
  if (state.selectionClearedPages[pageKey] !== state.resultStamp) {
    state.selectionClearedPages[pageKey] = state.resultStamp;
    state.selected[pageKey] = null;
  }
  const aiCards = allAiCards;
  const otherCards = allOtherCards.slice(start, start + state.pageSize);
  const visibleCandidates = [...aiCards, ...otherCards];
  // AI 精选要等 Dify 跑完才有（实测 20–40 秒），本地规则 1 秒内就出卡。
  const aiStillRunning = (state.result?.dify_status || "") === "running";
  const aiPending = aiStillRunning && !allAiCards.length;
  const aiPendingHtml = aiPending
    ? `<div class="ai-pending"><span class="ai-pending-dot"></span><span>AI 正在结合心意精细推演，下面是为你先行排查的稳妥备选…</span></div>`
    : "";

  const otherTotal = allOtherCards.length;
  const otherFrom = otherTotal ? start + 1 : 0;
  const otherTo = Math.min(start + state.pageSize, otherTotal);

  // 双层结果分层架构：AI 顾问心意精选置顶 + 本地规则库备选分层
  const aiSectionHtml = aiCards.length ? `
    <section class="tier-section tier-ai-section">
      <div class="tier-section-header">
        <div class="tier-header-main">
          <span class="pixel-tier-tag pixel-tag-ai">✦ AI 顾问心意精选</span>
          <strong class="tier-title">深度推演 · 最贴合推荐</strong>
        </div>
        <span class="tier-count">精选 ${aiCards.length} 件</span>
      </div>
      <p class="tier-summary-text">按你指定的场合、对象性格与心意深度推演，附带专属挑选理由与关键避坑提示。</p>
      <div class="tier-cards-grid tier-ai-grid">
        ${aiCards.map(renderCard).join("")}
      </div>
    </section>
  ` : "";

  const localSectionHtml = otherCards.length ? `
    <section class="tier-section tier-local-section">
      <div class="tier-section-header">
        <div class="tier-header-main">
          <span class="pixel-tier-tag pixel-tag-local">▪ 确定性规则库备选</span>
          <strong class="tier-title">更多候选参考 · 确定性筛选</strong>
        </div>
        <span class="tier-count">候选第 ${otherFrom}–${otherTo} 件 / 共 ${otherTotal} 件</span>
      </div>
      <p class="tier-summary-text">经过严格预算锁与 100+ 条地域礼俗禁忌排查后的稳妥候选库，支持翻页对比。</p>
      <div class="tier-cards-grid tier-local-grid">
        ${otherCards.map(renderCard).join("")}
      </div>
    </section>
  ` : "";

  const splitHtml = visibleCandidates.length
    ? aiPendingHtml + aiSectionHtml + localSectionHtml
    : "";

  $(`#${cardsId}`).innerHTML = visibleCandidates.length
    ? splitHtml
    : renderEmptyState(type);

  // Market Ready：结果导览标题
  const aiCount = candidates.filter(isAiCard).length;
  const noun = type === "purchased" ? "件礼物" : "个手作方案";
  $(`#${titleId}`).textContent = candidates.length
    ? (aiCount
        ? `给你挑了 ${aiCount} ${noun}，共 ${candidates.length} ${type === "purchased" ? "件" : "个"}可选`
        : `按你的条件，找到 ${candidates.length} ${noun}`)
    : "这次没找到合适的";

  $(`#${rangeId}`).textContent = candidates.length
    ? (allAiCards.length
        ? `我先挑的 ${allAiCards.length} 件 · 更多选项 ${otherTotal ? `${otherFrom}–${otherTo} / ${otherTotal}` : "0"}`
        : `第 ${otherFrom}–${otherTo} 件 / 共 ${otherTotal} 件`)
    : "";

  const pagination = $(`#${paginationId}`);
  // 分页只作用于更多选项：全部候选都在一页内时藏起来。
  pagination.classList.toggle("hidden", otherTotal <= state.pageSize);
  const pageButtons = [];
  pageButtons.push(`<button type="button" class="page-button" data-page-action="previous" ${state.pages[pageKey] <= 1 ? "disabled" : ""}>上一页</button>`);
  for (let page = 1; page <= pageCount; page += 1) {
    pageButtons.push(`<button type="button" class="page-button ${page === state.pages[pageKey] ? "active" : ""}" data-page="${page}">${page}</button>`);
  }
  pageButtons.push(`<button type="button" class="page-button" data-page-action="next" ${state.pages[pageKey] >= pageCount ? "disabled" : ""}>下一页</button>`);
  pageButtons.push(`<span class="page-summary">${otherTotal ? `${otherFrom}-${otherTo} / ${otherTotal}` : "0 件候选"}</span>`);
  pagination.innerHTML = pageButtons.join("");

  // 卡片敲定选择逻辑（支持反选）
  $(`#${cardsId}`).querySelectorAll("[data-select-card]").forEach((button) => {
    button.addEventListener("click", () => {
      const cardId = button.dataset.selectCard;
      if (state.selected[pageKey] === cardId) {
        state.selected[pageKey] = null;
      } else {
        state.selected[pageKey] = cardId;
      }
      renderCategoryPage(type);
    });
  });

  pagination.querySelectorAll("[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      state.pages[pageKey] = Number(button.dataset.page);
      renderCategoryPage(type);
      $(`#${cardsId}`).scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  pagination.querySelector("[data-page-action='previous']")?.addEventListener("click", () => {
    state.pages[pageKey] -= 1;
    renderCategoryPage(type);
  });

  pagination.querySelector("[data-page-action='next']")?.addEventListener("click", () => {
    state.pages[pageKey] += 1;
    renderCategoryPage(type);
  });

  const selectedCard = candidates.find((card) => card.product_id === state.selected[pageKey]);
  const selection = $(`#${type === "purchased" ? "purchasedSelection" : "diySelection"}`);
  if (selection) {
    selection.classList.toggle("has-selection", Boolean(selectedCard));
    selection.innerHTML = selectedCard
      ? `<span class="selection-dot"></span><span>当前敲定：<strong>${escapeHtml(selectedCard.title)}</strong></span>`
      : `<span class="selection-dot"></span><span>挑好了告诉我</span>`;
  }

  // 联动底部悬浮操作条 (Action Dock)
  updateSelectionDock(selectedCard, type);
}

// 底部悬浮操作条 (Action Dock) 交互联动与备忘复制
function updateSelectionDock(card, type) {
  const dock = $("#selectionDock");
  if (!dock) return;
  if (!card) {
    dock.classList.add("hidden");
    dock.classList.remove("dock-active");
    return;
  }
  dock.classList.remove("hidden");
  dock.classList.add("dock-active");

  const title = card.display_title || card.title || "已选好物";
  const price = card.price_band || (card.price != null ? `¥${card.price}` : "价格面议");
  const titleEl = $("#dockTitle");
  const priceEl = $("#dockPrice");
  if (titleEl) titleEl.textContent = title;
  if (priceEl) priceEl.textContent = price;

  const copyBtn = $("#dockCopyMemoBtn");
  if (copyBtn) {
    copyBtn.onclick = () => {
      const occasionText = state.intent?.occasion_label || state.intent?.occasion || state.occasion?.label || "日常心意";
      const relationText = state.intent?.relation_label || state.intent?.recipient_relation || state.relation?.label || "朋友";
      const reasonText = card.reason || "符合预算与场合要求，日常实用且有纪念意义";
      const riskText = card.risk_notes || "送出前建议核对喜好与包装是否完好";

      const memo = [
        `🎁 GiftRight 选礼备忘录`,
        `──────────────────────────────`,
        `【敲定礼物】${title} (${price})`,
        `【方案类型】${type === "purchased" ? "成品礼物" : "手工自制"}`,
        `【送礼场合】${occasionText} · 送给「${relationText}」`,
        `【挑选理由】${reasonText}`,
        `【避坑提示】${riskText}`,
        `──────────────────────────────`,
        `—— 由 GiftRight 智能心意工坊挑选`
      ].join("\n");

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(memo).then(() => {
          showAppToast("📋 选购备忘已复制到剪贴板！可发微信或保存备忘");
          if (window.triggerCustomPixelBurst) {
            window.triggerCustomPixelBurst(window.innerWidth / 2, window.innerHeight - 60);
          }
        }).catch(() => {
          showAppToast("📋 选购备忘已生成并复制！");
        });
      } else {
        showAppToast("📋 选购备忘已复制！");
      }
    };
  }

  const letterBtn = $("#dockLetterBtn");
  if (letterBtn) {
    letterBtn.onclick = () => {
      openLetterDialogForGift(card || title);
    };
  }

  const advisorBtn = $("#dockAdvisorBtn");
  if (advisorBtn) {
    advisorBtn.onclick = () => {
      setView("chatView");
      const input = $("#chatInput");
      if (input) {
        input.value = `我想送「${title}」，关于包装礼盒、随礼搭配或避坑，你有什么具体建议？`;
        input.focus();
      }
    };
  }

  const clearBtn = $("#dockClearBtn");
  if (clearBtn) {
    clearBtn.onclick = () => {
      state.selected[type] = null;
      renderCategoryPage(type);
    };
  }
}

function renderSceneTaboo(results) {
  const summary = results?.scene_taboo_summary;
  // Market Ready：none 状态 = 一条窄提示（不再是白色大卡+"知识库未查到"徽章）；
  // 有具体条目才展开成完整提醒卡。文案避开"未查到"等禁词，讲"不代表没有风险"。
  for (const id of ["#sceneTaboo", "#diySceneTaboo"]) {
    const target = $(id);
    if (!target) continue;
    if (!summary) { target.classList.add("hidden"); target.innerHTML = ""; continue; }
    if (summary.status === "none") {
      // 2026-11-17：没查到提醒就整块不显示。原来这里挂一句「暂未查到…
      // 这不代表没有风险」+ 可展开的解释，等于占一整块版面说「我没话说」，
      // 而且「我们查过…所以没有编造提醒」这种自证口吻很重。
      // 卡片本身已经有各自的「留意」事项，这里空着更好。
      target.classList.add("hidden");
      target.classList.remove("is-empty", "has-content", "expanded");
      target.innerHTML = "";
      continue;
    }
    const items = (summary.items || []).map((item) =>
      `<li><b>${escapeHtml(item.keyword)}</b><span>${escapeHtml(item.note)}</span></li>`
    ).join("");
    // P1 长提醒：首屏只给最关键的 1-2 条，完整提醒默认收在
    // 「查看全部送礼提醒」里（避免手机上滚很久才见到第一张卡）。
    const visibleItems = (summary.items || []).slice(0, 2);
    const visibleList = visibleItems.map((item) =>
      `<li><b>${escapeHtml(item.keyword)}</b><span>${escapeHtml(item.note)}</span></li>`
    ).join("");
    const hiddenItems = (summary.items || []).slice(2);
    const moreBlock = hiddenItems.length
      ? `<details class="scene-more"><summary>查看全部送礼提醒（${hiddenItems.length + 2}）</summary><ul class="scene-taboo-list">${hiddenItems.map((item) => `<li><b>${escapeHtml(item.keyword)}</b><span>${escapeHtml(item.note)}</span></li>`).join("")}</ul></details>`
      : "";
    // AI 综合提醒常常 800–1300 字，全展开会把礼物卡挤到首屏之外。
    // 默认收起，只露标题一行；想读的点开。
    const aiBlock = summary.ai_note
      ? `<details class="scene-ai-note"><summary><strong>AI 综合提醒</strong><span class="scene-ai-hint">展开</span></summary><p>${escapeHtml(cleanChatText(summary.ai_note))}</p></details>`
      : "";
    const kbList = items
      ? `<ul class="scene-taboo-list">${visibleList}</ul>${moreBlock}`
      : "";
    const sourceLine = (summary.sources || []).length
      ? `<small>来源：${summary.sources.map((source) => source.url && source.url !== "#" ? `<a href="${escapeHtml(safeUrl(source.url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title)}</a>` : escapeHtml(source.title)).join("、")}</small>`
      : "";
    const statusBadge = summary.status === "ai_found"
      ? `<span class="scene-badge ai">知识库 + AI</span>`
      : `<span class="scene-badge">知识库</span>`;
    target.classList.remove("hidden", "is-empty");
    target.classList.add("has-content");
    target.innerHTML = `<h3>这次送礼，建议先留意</h3>${statusBadge}${kbList}${aiBlock}<p class="scene-disclaimer">${escapeHtml(shortDisclaimer(summary.disclaimer))}</p>${sourceLine}`;
  }
}

function renderResults(payload, preserveState) {
  const results = payload.result || {};
  // dify_status 在 payload 层（后端 job 状态），不在 result 里；
  // 分区话术（AI 正在挑选中 / AI 这次没选出）靠它判断，必须并进来。
  results.dify_status = payload.dify_status || results.dify_status || "";
  const oldResult = state.result;
  const oldSelected = state.selected;
  // P0-2：AI 更新重渲染后恢复用户展开的手作详情（details 是 DOM 状态，
  // innerHTML 重渲染会收起，先按 data-card-id 记住再恢复）。
  const openDetailsIds = preserveState
    ? $$(".diy-details[open]").map((d) => {
        const card = d.closest(".gift-card");
        return card && card.dataset.cardId ? String(card.dataset.cardId) : "";
      }).filter(Boolean)
    : [];
  state.result = results;
  if (!preserveState) {
    // 全新结果：重置分页与选择（首次渲染 / 失败后重渲染）
    state.pages = { purchased: 1, diy: 1 };
    state.selected = { purchased: null, diy: null };
    // 新结果进来：自增戳，让 renderCategoryPage 的「首次清空选择」重新生效一次
    state.resultStamp += 1;
    state.selectionClearedPages = {};
  }
  // P0-2：AI 更新完成时保留用户操作（视图/滚动/展开/仍存在的选择）。
  // preserveState 时不改 resultStamp，renderCategoryPage 的
  // 「首次清空选择」（selectionClearedPages[pageKey] !== resultStamp）
  // 就不会触发，用户已做的选择得以保留。被新结果剔除的选中卡：
  // 清掉选择并显式提示，不静默重置。
  if (preserveState && oldSelected) {
    const newIds = new Set(
      (results.candidates || []).map((card) => String(card.product_id || ""))
    );
    for (const type of ["purchased", "diy"]) {
      const selectedId = oldSelected[type];
      if (selectedId && !newIds.has(String(selectedId))) {
        state.selected[type] = null;
        setResultStatus(
          "warning",
          "你之前选的那件这次没进来，重新挑一件就好"
        );
      }
    }
  }
  renderPageNotices(results);
  renderSceneTaboo(results);
  // 后台增强进行中：显示等待提示面板（购买/手作两个视图各一条，一起切）
  const waiting = $("#difyWaiting");
  const diyWaiting = $("#diyDifyWaiting");
  const showWaiting = payload.dify_status === "running";
  if (waiting) waiting.classList.toggle("hidden", !showWaiting);
  if (diyWaiting) diyWaiting.classList.toggle("hidden", !showWaiting);
  // 模式状态收敛为轻量文字：AI 状态放结果页状态条（renderCategoryPage 写入），
  // 顶栏胶囊只保留内部调试 title，不再占用用户注意力。
  const modePill = $("#modePill");
  modePill.textContent = showWaiting ? "正在挑" : "";
  modePill.removeAttribute("title");
  const silentFallback = [
    "invalid_structured_output_fallback",
    "empty_result_fallback",
  ].includes(payload.dify_status);
  if (
    payload.source !== "dify"
    && payload.dify_status !== "running"
    && payload.dify_error
    && !silentFallback
  ) {
    const error = payload.dify_error;
    const summary = [
      error.code,
      error.status ? `HTTP ${error.status}` : "",
      error.message,
    ].filter(Boolean).join(" · ");
    modePill.title = summary;
    // 技术细节（错误码/HTTP 状态）只留在胶囊 title 里供调试，
    // 用户正文只说「结果是什么、推荐是否可用」，不暴露内部组件名。
    setResultStatus(
      "error",
      `<div><strong>这次先给你快速结果</strong><span>AI 那边没回应，下面这些是按你的条件筛的，照样能挑。想再试一次，重新提交就行。</span></div>`
    );
  }
  $("#sidebarStatus")?.remove();
  const selectedType = results.intent?.gift_mode === "diy" ? "diy" : "purchased";
  $$("[data-mode-nav]").forEach((item) => {
    const mode = item.dataset.modeNav;
    item.classList.toggle(
      "hidden",
      !(
        results.intent?.gift_mode === "all"
        || mode === selectedType
      )
    );
  });
  // 结果导览：条件摘要 chips + 状态条 AI 文案 + 选择备注（两个视图都写）
  const intent = results.intent || {};
  const condLabels = [
    intent.occasion_label,
    intent.relation_label,
    (intent.budget_min != null || intent.budget_max != null)
      ? `¥${intent.budget_min ?? 0}–${intent.budget_max ?? "∞"}`
      : "",
  ].filter(Boolean);
  const condHtml = condLabels.map((c) => `<span>${escapeHtml(c)}</span>`).join("");
  // 状态条 AI 文案（Market Ready）：单行说明，区分本地初筛与 AI 补充。
  // 2026-11-17：Dify 偶发返回 0 张精选（模型这轮没给排序），source 仍是 dify，
  // 旧文案照写「推荐理由已按你的条件更新」——用户看到的却一张理由都没有，
  // 等于替一次失败背书。现在按「真的有没有 AI 卡」说话。
  const aiCardCount = (results.candidates || []).filter(isAiCard).length;
  const aiStateText = showWaiting
    ? `<strong>${aiCardCount ? "挑好了" : "先看这几件"}</strong><span class="ai-tail">　·　我还在写理由</span>`
    : (payload.source === "dify"
        ? (aiCardCount
            ? `<strong>挑好了</strong><span class="ai-tail">　·　挑了 ${aiCardCount} 件，都写了理由</span>`
            : `<strong>挑好了</strong><span class="ai-tail">　·　这轮没挑出特别合适的，下面这些是按条件筛的</span>`)
        : "");
  const choiceNote = results.intent?.gift_mode === "all"
    ? "这次选择：成品礼物 + 亲手做"
    : (selectedType === "diy" ? "这次选择：亲手做" : "这次选择：成品礼物");
  for (const [prefix, aiText, note] of [
    ["purchased", aiStateText, selectedType === "purchased" || results.intent?.gift_mode === "all" ? choiceNote : "这次选择：成品礼物"],
    ["diy", aiStateText, selectedType === "diy" || results.intent?.gift_mode === "all" ? choiceNote : "这次选择：亲手做"],
  ]) {
    const cond = $(`#${prefix}Conditions`);
    if (cond) cond.innerHTML = condHtml;
    const ai = $(`#${prefix}AiState`);
    if (ai) ai.innerHTML = aiText;
    const cn = $(`#${prefix}ChoiceNote`);
    if (cn) cn.textContent = note;
  }
  // 双模式时让另一个 tab 也可见；单模式时隐藏整个切换行（方案：单选直接给结果）
  const dualMode = results.intent?.gift_mode === "all";
  $("#purchasedTabs").classList.toggle("hidden", !dualMode);
  $("#diyTabs").classList.toggle("hidden", !dualMode);
  if (preserveState) {
    // P0-2：AI 完成时不抢视图、不重置状态，只在用户当前所在页重渲染。
    // selectedType 是本次结果的首选类型，但不能覆盖用户手动切到的视图。
    const currentType =
      state.activeView === "diyView"
        ? "diy"
        : state.activeView === "purchasedView"
          ? "purchased"
          : selectedType;
    renderCategoryPage(currentType);
    // P0-2：恢复 AI 更新前展开的手作详情（仍在新结果里的卡片才有）。
    if (openDetailsIds.length) {
      $$(".gift-card[data-card-id]").forEach((card) => {
        if (openDetailsIds.includes(String(card.dataset.cardId))) {
          const details = card.querySelector(".diy-details");
          if (details) details.open = true;
        }
      });
    }
  } else {
    renderCategoryPage(selectedType);
    setView(selectedType === "diy" ? "diyView" : "purchasedView");
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

// 顾问上下文：把本次表单摘要和当前选中卡带进 chat，避免用户重复输入。
// 只有真的提交过结果才有内容；否则返回 {}，行为与旧版一致。
function buildChatContext() {
  const result = state.result || {};
  const intent = result.intent || {};
  const summary = {};
  if (intent.occasion) summary.occasion = intent.occasion_label || intent.occasion;
  if (intent.occasion_custom) summary.occasion_custom = intent.occasion_custom;
  if (intent.recipient_relation) summary.recipient_relation = intent.relation_label || intent.recipient_relation;
  if (intent.budget_min != null || intent.budget_max != null) {
    summary.budget = `¥${intent.budget_min ?? "?"}–${intent.budget_max ?? "?"}`;
  }
  if (intent.gift_mode) summary.gift_mode = intent.gift_mode;
  const hard = intent.hard_preferences || [];
  if (hard.length) summary.hard_preferences = hard;
  if (state.requestPayload?.free_text) summary.user_notes = state.requestPayload.free_text;
  if (state.requestPayload?.avoid_custom) summary.avoid = state.requestPayload.avoid_custom;
  for (const type of ["purchased", "diy"]) {
    const id = state.selected?.[type];
    if (!id) continue;
    const card = (result.candidates || []).find((item) => item.product_id === id);
    if (card) {
      summary[`selected_${type === "purchased" ? "gift" : "diy"}`] = {
        title: card.title,
        price: card.price_band || card.price || "",
      };
    }
  }
  // P0-3：把当前推荐候选标题带进顾问上下文，让顾问回答和页面推荐共享
  // 同一组候选依据（最多 5 条，避免上下文膨胀）。
  const candidateTitles = (result.candidates || [])
    .map((item) => String(item.title || "").trim())
    .filter(Boolean)
    .slice(0, 5);
  if (candidateTitles.length) summary.recommended_candidates = candidateTitles;
  return Object.keys(summary).length ? summary : {};
}

const DIFY_WAIT_TIPS = [
  "我在挑，一两分钟。",
  "先把容易踩雷的筛掉…",
  "在给每件想一句为什么…",
  "快了，再等等。",
];

async function pollDifyJob(jobId) {
  const tipsNode = $("#difyWaitTip");
  // 2026-11-16：原来这里每 12 秒轮换一句安抚文案，会把轮询写入的
  // 「已等待 N 秒」覆盖掉。改成只在还没拿到 elapsed_s 时兜底一次，
  // 之后交给 pollDifyJobInner 的实时秒数。
  let tipTimer = null;
  if (tipsNode) {
    let tipIndex = 0;
    tipTimer = window.setInterval(() => {
      // 轮询一旦写过实时秒数就不再插手。
      if (tipsNode.dataset.liveSeconds === "1") return;
      tipIndex = (tipIndex + 1) % DIFY_WAIT_TIPS.length;
      tipsNode.textContent = DIFY_WAIT_TIPS[tipIndex];
    }, 12000);
  }
  try {
    await pollDifyJobInner(jobId);
  } finally {
    if (tipTimer) window.clearInterval(tipTimer);
    if (tipsNode) tipsNode.closest(".dify-waiting")?.classList.add("hidden");
  }
}

async function pollDifyJobInner(jobId) {
  // 2026-11-16：连续失败计数。原来任何一次非 200 或网络抖动都直接 return，
  // 界面永久停在「正在检查约束」，用户只能干等 —— 这就是「等了五分钟没结果」的成因。
  let consecutiveErrors = 0;
  // tipsNode 在 pollDifyJob 里是局部变量，这里要自己取一次。
  const liveTipNode = $("#difyWaitTip");
  for (let attempt = 0; attempt < 180; attempt += 1) {
    if (state.difyJobId !== jobId) return;
    await wait(2000);
    try {
      const response = await fetch(
        `${API_BASE}/api/recommend/status?job_id=${encodeURIComponent(jobId)}`
      );
      // 2026-11-16：原来这里直接 return，一次非 200 就永久停在等待界面，
      // 用户只能看到「正在检查约束」。改成继续重试，连续失败才报错。
      if (!response.ok) {
        consecutiveErrors += 1;
        if (consecutiveErrors >= 5) {
          state.difyJobId = null;
          setResultStatus("error", "没拿到结果，再提交一次试试。");
          return;
        }
        continue;
      }
      consecutiveErrors = 0;
      const status = await response.json();
      // 2026-11-16：把已等待秒数写进提示，让用户看得出它还在干活。
      // Dify 端实测 35~90 秒，光看一句静态文案没法区分「在跑」和「死了」。
      if (status.status === "running" && status.elapsed_s && liveTipNode) {
        liveTipNode.dataset.liveSeconds = "1";
        liveTipNode.textContent =
          `AI 正在按你的场景重新挑，已等 ${Math.round(status.elapsed_s)} 秒（一般 40~90 秒）。填好的东西不会丢。`;
      }
      if (status.status === "succeeded" && status.payload) {
        state.difyJobId = null;
        // P0-2：AI 更新完成，保留用户操作（视图/滚动/展开/选择），
        // 只做合并式更新；被剔除的选中卡由 renderResults 提示。
        // 2026-11-16：渲染异常必须让用户看见，不能被下面的 catch 吞掉后
        // 永久停在等待界面。
        try {
          renderResults(status.payload, true);
        } catch (renderError) {
          console.error("[pollDifyJob] renderResults failed", renderError);
          setResultStatus("error", "结果拿到了，但页面没渲染出来，刷新一下。");
        }
        return;
      }
      if (status.status === "failed") {
        state.difyJobId = null;
        renderResults({
          source: "local_rule_mode",
          dify_status: "request_failed",
          dify_error: status.error,
          result: state.result,
        });
        return;
      }
    } catch (networkError) {
      // 网络抖动不算失败：连续 5 次才判定放弃，否则继续轮询。
      consecutiveErrors += 1;
      if (consecutiveErrors >= 5) {
        state.difyJobId = null;
        setResultStatus("error", "网络断了，再试一次。");
        return;
      }
    }
  }
  // 2026-11-16：180 次（6 分钟）用完仍无结果时明确告知，不再静默停住。
  state.difyJobId = null;
  setResultStatus("error", "等太久了，重新提交一次吧。填的东西还在。");
}

async function submitRecommendation(event) {
  event.preventDefault();
  const button = $("#nextButton");
  button.disabled = true;
  button.textContent = "正在按条件筛…";
  setResultStatus("loading", "正在按你的条件筛候选…");
  try {
    state.requestPayload = collectPayload();
    const response = await fetch(`${API_BASE}/api/recommend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.requestPayload),
    });
    if (!response.ok) {
      let message = "";
      try {
        const problem = await response.json();
        message = problem && problem.error && problem.error.message ? problem.error.message : "";
      } catch {
        message = "";
      }
      const error = new Error(message || "request failed");
      error.isServerMessage = Boolean(message);
      throw error;
    }
    const result = await response.json();
    state.difyJobId = result.dify_job_id || null;
    setResultStatus(null, "");
    renderResults(result);
    if (state.difyJobId) pollDifyJob(state.difyJobId);
  } catch (error) {
    if (error && error.isServerMessage) {
      // 后端明确拒绝（比如预算倒挂）：把原因讲清楚，不是服务挂了。
      // 部分浏览器会在表单提交被拒后丢弃 number input 的值，
      // 这里把刚提交的预算回填回去，兑现"已经填好的内容不会丢"。
      restoreBudgetInputs();
      setResultStatus(
        "error",
        `<div><strong>这次没有生成推荐</strong><span>${escapeHtml(error.message)}。改一下输入再试；已经填好的内容不会丢。</span></div>`
      );
    } else {
      setResultStatus(
        "error",
        "<div><strong>暂时没能生成推荐</strong><span>服务没响应。重新提交一次就行，填过的东西都还在。</span></div>"
      );
    }
  } finally {
    button.disabled = false;
    button.innerHTML = "帮我挑礼物";
  }
}

// 提交被后端拒绝时，把刚发送的预算值回填到输入框。
// state.requestPayload 里存的是提交瞬间的值（null 表示原本就没填）。
function restoreBudgetInputs() {
  const payload = state.requestPayload;
  if (!payload) return;
  const min = $("#budgetMin");
  const max = $("#budgetMax");
  if (min && payload.budget_min != null) min.value = String(payload.budget_min);
  if (max && payload.budget_max != null) max.value = String(payload.budget_max);
}

function renderAssistantMsg(htmlContent, sourcesHtml = "") {
  return `
    <div class="chat-message assistant">
      <div class="chat-avatar">礼</div>
      <div class="chat-bubble">
        <div class="chat-text">${htmlContent}</div>
        ${sourcesHtml ? `<div class="chat-sources">${sourcesHtml}</div>` : ""}
      </div>
    </div>
  `;
}

function renderUserMsg(textContent) {
  return `
    <div class="chat-message user">
      <div class="chat-bubble">
        <div class="chat-text">${escapeHtml(textContent)}</div>
      </div>
      <div class="chat-avatar-user">✦</div>
    </div>
  `;
}

async function submitChat(event) {
  event.preventDefault();
  const input = $("#chatInput");
  const question = input.value.trim();
  if (!question) return;
  const messages = $("#chatMessages");
  messages.insertAdjacentHTML("beforeend", renderUserMsg(question));
  input.value = "";

  const pending = document.createElement("div");
  pending.className = "chat-message assistant pending";
  pending.innerHTML = `
    <div class="chat-avatar">礼</div>
    <div class="chat-bubble">
      <span class="pixel-spin">▪</span> <span class="pending-stage-text">已收到，正在推演并整理回复…</span>
    </div>
  `;
  messages.appendChild(pending);
  messages.scrollTop = messages.scrollHeight;

  let answerText = "";
  let stageShown = { received: true, retrieving: false };
  const setStage = (text) => {
    const stageEl = pending.querySelector(".pending-stage-text");
    if (stageEl) stageEl.textContent = text;
    else pending.textContent = text;
    messages.scrollTop = messages.scrollHeight;
  };

  try {
    const response = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, context: buildChatContext() }),
    });
    if (!response.ok || !response.body) {
      // fall back to the old blocking endpoint for a plain error answer
      const alt = await fetch(`${API_BASE}/api/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, context: {} }) });
      const data = await alt.json();
      pending.remove();
      const sourceLine = data.sources?.length ? `<small>来源：${data.sources.map((source) => `<a href="${escapeHtml(safeUrl(source.url))}" target="_blank" rel="noreferrer">${escapeHtml(source.title)}</a>`).join("、")}</small>` : "";
      messages.insertAdjacentHTML("beforeend", renderAssistantMsg(escapeHtml(cleanChatText(data.answer)), sourceLine));
      messages.scrollTop = messages.scrollHeight;
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalSources = null;
    let fallbackPayload = null;

    const handleEvent = (eventName, data) => {
      if (eventName === "stage") {
        if (data.stage === "retrieving" && !stageShown.retrieving) {
          stageShown.retrieving = true;
          setStage("在知识库里找相关内容…");
        } else if (data.stage === "local_answer") {
          setStage("在整理本地资料…");
        }
      } else if (eventName === "delta") {
        if (!stageShown.organizing) {
          stageShown.organizing = true;
          pending.remove();
          const live = document.createElement("div");
          live.className = "chat-message assistant";
          live.id = "chatLiveAnswer";
          live.innerHTML = `
            <div class="chat-avatar">礼</div>
            <div class="chat-bubble">
              <div class="chat-text" id="chatLiveText"></div>
            </div>
          `;
          messages.appendChild(live);
        }
        answerText += data.text || "";
        const liveText = $("#chatLiveText");
        if (liveText) {
          liveText.textContent = cleanChatText(answerText);
          messages.scrollTop = messages.scrollHeight;
        }
      } else if (eventName === "done") {
        finalSources = data.sources || [];
      } else if (eventName === "fallback") {
        fallbackPayload = data.payload;
      }
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let eventName = "message";
        let dataStr = "";
        for (const line of rawEvent.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
        }
        if (!dataStr) continue;
        try { handleEvent(eventName, JSON.parse(dataStr)); } catch { /* ignore malformed chunk */ }
      }
    }

    pending.remove();
    $("#chatLiveAnswer")?.remove();

    // 来源条目统一渲染：有 url 给链接，没 url 显示文档名。
    const sourceLineOf = (sources) => {
      const list = sources || [];
      if (!list.length) return "";
      return `<small>来源：${list.map((source) =>
        source.url && source.url !== "#"
          ? `<a href="${escapeHtml(safeUrl(source.url))}" target="_blank" rel="noreferrer">${escapeHtml(source.title)}</a>`
          : escapeHtml(source.title)
      ).join("、")}</small>`;
    };

    if (fallbackPayload) {
      messages.insertAdjacentHTML("beforeend", renderAssistantMsg(escapeHtml(cleanChatText(fallbackPayload.answer)), sourceLineOf(fallbackPayload.sources)));
    } else if (answerText) {
      messages.insertAdjacentHTML("beforeend", renderAssistantMsg(escapeHtml(cleanChatText(answerText)), sourceLineOf(finalSources)));
    } else {
      messages.insertAdjacentHTML("beforeend", renderAssistantMsg("这会儿没连上，等下再问一次。"));
    }
  } catch {
    pending.remove();
    $("#chatLiveAnswer")?.remove();
    if (answerText) {
      messages.insertAdjacentHTML("beforeend", renderAssistantMsg(escapeHtml(cleanChatText(answerText))));
    } else {
      messages.insertAdjacentHTML("beforeend", renderAssistantMsg("这会儿没连上，等下再问一次。"));
    }
  }
  messages.scrollTop = messages.scrollHeight;
}

function formatRate(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function renderResearch(data) {
  const summary = data?.summary || {};
  const primary = summary.gift_right_constraint_first || {};
  const metrics = [
    ["测试场景", `${data?.experiment?.scenario_count || 0} 个`, "离线场景库"],
    ["硬约束违反率", formatRate(primary.hard_constraint_violation_rate), "GiftRight 规则优先模式"],
    ["理由有据率", formatRate(primary.evidence_grounding_rate), "每条理由都有出处"],
  ];
  $("#researchMetrics").innerHTML = metrics.map(([label, value, note]) => `
    <div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></div>
  `).join("");

  const labels = {
    gift_right_constraint_first: "GiftRight 约束优先",
    ablation_without_rules: "去除规则层",
    price_delivery_heuristic: "仅价格/时效启发式",
  };
  $("#researchRows").innerHTML = Object.entries(summary).map(([key, item]) => `
    <tr>
      <td>${escapeHtml(labels[key] || key)}</td>
      <td>${escapeHtml(formatRate(item.hard_constraint_violation_rate))}</td>
      <td>${escapeHtml(formatRate(item.evidence_grounding_rate))}</td>
      <td>${escapeHtml(formatRate(item.seed_scenario_hit_rate))}</td>
      <td>${escapeHtml(Number(item.average_eligible_candidates || 0).toFixed(2))}</td>
    </tr>
  `).join("");
}

async function openResearch() {
  $$(".workspace-view").forEach((view) => view.classList.add("hidden"));
  $("#researchPanel").classList.remove("hidden");
  $("#researchMetrics").innerHTML = '<div class="metric"><span>正在读取</span><strong>...</strong><small>加载离线评测结果</small></div>';
  try {
    const response = await fetch(`${API_BASE}/api/research`);
    if (!response.ok) throw new Error("research request failed");
    renderResearch(await response.json());
  } catch {
    $("#researchMetrics").innerHTML = '<div class="metric"><span>研究数据</span><strong>读不到</strong><small>评测结果文件不在</small></div>';
    $("#researchRows").innerHTML = "";
  }
}

$$(".top-link").forEach((item) =>
  item.addEventListener("click", () => {
    if (item.dataset.view === "basicView") {
      navigateToGiftView(false);
    } else {
      setView(item.dataset.view);
    }
  })
);

$$("[data-go]").forEach((item) =>
  item.addEventListener("click", () => {
    if (item.classList.contains("results-edit")) {
      navigateToGiftView(true);
    } else if (item.dataset.go === "basicView") {
      navigateToGiftView(false);
    } else {
      setView(item.dataset.go);
    }
  })
);
// 结果页的「买一件 / 亲手做」tab
$$(".tab[data-view]").forEach((item) => item.addEventListener("click", () => setView(item.dataset.view)));
$$(".choice").forEach((button) => button.addEventListener("click", () => {
  if (button.closest("#occasionChoices")) state.occasion = selectChoice("#occasionChoices", button);
  if (button.closest("#relationChoices")) state.relation = selectChoice("#relationChoices", button);
  if (button.closest("#recipientGenderChoices")) state.recipientGender = selectChoice("#recipientGenderChoices", button);
  if (button.closest("#giftModeChoices")) {
    button.classList.toggle("selected");
    state.giftModes = $$("#giftModeChoices .choice.selected").map((item) => item.dataset.value);
    if (!state.giftModes.length) {
      button.classList.add("selected");
      state.giftModes = [button.dataset.value];
    }
    state.giftMode = {
      value: state.giftModes.length === 2 ? "all" : state.giftModes[0],
      label: state.giftModes.length === 2 ? "购买 + 自制礼物" : button.dataset.label,
    };
  }
  trailSync();
}));
$$(".examples button").forEach((button) => button.addEventListener("click", () => {
  $("#freeText").value = button.dataset.text;
  trailSync();
}));
// 预算、自定义说法、心意、忌口变化都要更新左侧逐项回显。
// 2026-11-19：原来只绑了预算两个框，自定义说法和心意填了不回显。
["#budgetMin", "#budgetMax", "#occasionCustom", "#relationCustom", "#freeText", "#avoidCustom"].forEach((id) => {
  $(id)?.addEventListener("input", trailSync);
});
$$("#preferenceChoices input").forEach((input) => input.addEventListener("change", trailSync));
$("#giftForm").addEventListener("submit", submitRecommendation);
$("#chatForm").addEventListener("submit", submitChat);
// 研究证据视图是答辩/内部材料，不对普通用户露出入口。
// 保留完整渲染逻辑，用 URL 参数 ?debug=research 或控制台 openResearch() 仍可打开。
function isInternalUiEnabled() {
  try {
    return new URLSearchParams(window.location.search).has("debug");
  } catch {
    return false;
  }
}
const internalUi = isInternalUiEnabled();
const resBtn = $("#researchButton");
if (resBtn) {
  resBtn.classList.toggle("hidden", !internalUi);
  resBtn.addEventListener("click", openResearch);
}
const closeResBtn = $("#closeResearchButton");
if (closeResBtn) {
  closeResBtn.addEventListener("click", () => {
    $("#researchPanel")?.classList.add("hidden");
    setView(state.activeView === "researchPanel" ? "basicView" : state.activeView);
  });
}

// ── 四问向导 ──────────────────────────────────────────────
// 一次只问一个问题：1 场合 → 2 对象 → 3 方式/预算 → 4 心意。
// 最后一步的「帮我挑礼物」才真正提交表单。
const WIZARD_STEP_LABELS = ["场合", "对象", "方式与预算", "心意与避开"];
// wizardStep 已提到文件顶部声明（trailSync 要读它），这里不再重复声明。

function wizardSync(options) {
  const opts = options || {};
  $$(".wizard-step").forEach((el) => el.classList.toggle("active", Number(el.dataset.step) === wizardStep));
  $$(".wizard-dot").forEach((el) => el.classList.toggle("on", Number(el.dataset.dot) <= wizardStep));
  const label = $("#wizardProgressLabel");
  if (label) label.textContent = `第 ${wizardStep} / 4 步 · ${WIZARD_STEP_LABELS[wizardStep - 1]}`;
  // 左侧问题栏跟随当前步（设计稿第 4 节：左问题、右选项的工作区）
  const stepCopy = SCENE_STEPS[wizardStep];
  if (stepCopy) {
    const q = $("#asideQuestion");
    const h = $("#asideHelp");
    if (q) q.innerHTML = stepCopy.question;
    if (h) h.textContent = stepCopy.help;
  }
  trailSync();
  $("#wizardPrev").classList.toggle("hidden", wizardStep === 1);
  // 第一步的「返回首页」：滚回页面顶部（hero）——设计稿的退出动作
  $("#wizardHome").classList.toggle("hidden", wizardStep !== 1);
  const last = wizardStep === 4;
  $("#wizardNext").classList.toggle("hidden", last);
  $("#nextButton").classList.toggle("hidden", !last);
  // 只在换步时滚动。初始化那次不滚——否则首屏的场景入口会被直接跳过，
  // 用户打开网站看到的是第 4 步表单，而不是「送礼这件事，先想想你要送给谁」。
  // 2026-11-17：换成 scrollToWizard，理由同 applyScene —— scrollIntoView 会被
  // 粘性页头盖住第一行。
  if (!opts.initial) {
    scrollToWizard();
  }
}

$("#wizardNext").addEventListener("click", () => {
  if (wizardStep < 4) { wizardStep += 1; wizardSync(); }
});
$("#wizardPrev").addEventListener("click", () => {
  if (wizardStep > 1) { wizardStep -= 1; wizardSync(); }
});
$("#wizardHome").addEventListener("click", () => {
  // 2026-11-17：进向导时 hero 被 wizard-active 收起了，光滚回顶部是一片空白。
  // 退出向导必须同时撤掉这个状态，并把步骤退回第 1 步。
  // 传 initial 是不要它滚到向导 —— 这里要回的是首屏。
  document.body.classList.remove("wizard-active");
  wizardStep = 1;
  wizardSync({ initial: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
});
// 初始同步一次：让第一步的「返回首页」可见（设计稿第一步左下有它）。
// 传 initial 是为了不滚动——首屏必须停在场景入口，不能被自动带到表单。
wizardSync({ initial: true });

/* ============================================================
   融入像素方案优势：快捷情境预设、规则透明看板、手写贺卡与轻提示
   ============================================================ */

const PRESET_CONFIGS = {
  student: {
    label: "同学室友",
    occasion: { value: "birthday", label: "生日" },
    relation: { value: "classmate", label: "同学" },
    budgetMin: 50,
    budgetMax: 150,
    freeText: "实用小物件，日常用得上，预算友好无负担",
    avoid: [],
  },
  bestie: {
    label: "闺蜜好友",
    occasion: { value: "celebration", label: "庆祝" },
    relation: { value: "friend", label: "朋友" },
    budgetMin: 100,
    budgetMax: 300,
    freeText: "高颜值审美走心，适合拍照打卡，平时注重生活质感",
    avoid: [],
  },
  elder: {
    label: "拜访长辈",
    occasion: { value: "holiday", label: "节日" },
    relation: { value: "family", label: "长辈/亲戚" },
    budgetMin: 300,
    budgetMax: 800,
    freeText: "体面大方，注重养生、健康与实用，避开生冷禁忌",
    avoid: ["non_alcoholic"],
  },
  colleague: {
    label: "职场同事",
    occasion: { value: "thank_you", label: "答谢" },
    relation: { value: "colleague", label: "同事" },
    budgetMin: 80,
    budgetMax: 200,
    freeText: "适度得体，有礼貌有边界感，办公或日常实用",
    avoid: [],
  },
};

function showAppToast(msg, duration = 2200) {
  const t = $("#appToast");
  if (!t) return;
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(showAppToast.timer);
  showAppToast.timer = setTimeout(() => t.classList.remove("show"), duration);
}

function applyPreset(key) {
  const p = PRESET_CONFIGS[key];
  if (!p) return;

  state.occasion = { ...p.occasion };
  $$("#occasionChoices .choice").forEach((item) => {
    item.classList.toggle("selected", item.dataset.value === p.occasion.value);
  });

  state.relation = { ...p.relation };
  $$("#relationChoices .choice").forEach((item) => {
    item.classList.toggle("selected", item.dataset.value === p.relation.value);
  });

  const minEl = $("#budgetMin");
  const maxEl = $("#budgetMax");
  if (minEl) minEl.value = p.budgetMin;
  if (maxEl) maxEl.value = p.budgetMax;

  const ftEl = $("#freeText");
  if (ftEl) ftEl.value = p.freeText;

  $$("#preferenceChoices input").forEach((cb) => {
    cb.checked = p.avoid.includes(cb.value);
  });

  $$(".preset-chip").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.preset === key);
  });

  document.body.classList.add("wizard-active");
  trailSync();
  wizardStep = 3;
  wizardSync();
  showAppToast(`⚡ 已应用「${p.label}」预设，已为您跳至预算步骤`);
}

$$(".preset-chip").forEach((btn) => {
  btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
});

const guardToggleBtn = $("#guardToggleBtn");
const guardBody = $("#guardBody");
if (guardToggleBtn && guardBody) {
  guardToggleBtn.addEventListener("click", () => {
    const isHidden = guardBody.classList.toggle("hidden");
    guardToggleBtn.textContent = isHidden ? "查看拦截明细 ▾" : "收起明细 ▴";
  });
}

const letterDialog = $("#letterDialog");
const closeLetterBtn = $("#closeLetterBtn");
const copyLetterBtn = $("#copyLetterBtn");

if (closeLetterBtn && letterDialog) {
  closeLetterBtn.addEventListener("click", () => letterDialog.close());
  letterDialog.addEventListener("click", (e) => {
    if (e.target === letterDialog) letterDialog.close();
  });
}

// 贺卡状态管理与多风格文案引擎
const letterCardState = {
  candidate: null,
  title: "",
  cleanTitle: "",
  relationLabel: "朋友",
  occasionLabel: "特别的日子",
  currentStyle: "custom",
  styleIndices: { custom: 0, warm: 0, casual: 0 },
  options: { custom: [], warm: [], casual: [] },
};

function buildGreetingOptions(candidate, rawTitle) {
  const relationLabel =
    state.intent?.relation_label ||
    state.intent?.recipient_relation ||
    state.relation?.label ||
    "朋友";
  const occasionLabel =
    state.intent?.occasion_label ||
    state.intent?.occasion ||
    state.occasion?.label ||
    "特别的日子";

  const title = (rawTitle || candidate?.display_title || candidate?.title || candidate?.name || "心仪好礼").trim();
  const cleanTitle = title.replace(/（[^）]*）|\([^)]*\)/g, "").trim() || title;

  const giftMeaning = (candidate?.meaning || candidate?.gift_meaning || "").trim();
  const difyCardMsg = (state.result?.card_message || "").trim();

  // 1. 心意定制款（优先融合 Dify card_message / 礼物深度寓意 / 送礼偏好）
  const customList = [];
  if (difyCardMsg && difyCardMsg.length > 8) {
    customList.push(difyCardMsg);
  }
  if (giftMeaning && giftMeaning.length > 10) {
    let cardFromMeaning = giftMeaning;
    if (!cardFromMeaning.includes("祝") && !cardFromMeaning.includes("愿")) {
      cardFromMeaning = `愿这份「${cleanTitle}」，${giftMeaning}。祝${occasionLabel}快乐，事事顺心！`;
    }
    customList.push(cardFromMeaning);
  }
  customList.push(
    `挑选了这份「${cleanTitle}」送给你，愿它替我带来一份妥帖与心意。祝${occasionLabel}如意，万事顺遂！`
  );
  customList.push(
    `见字如面！这份「${cleanTitle}」想陪你度过更多舒心惬意的时光。愿新的一岁（或阶段）平安顺意，常有欢喜！`
  );

  // 2. 温情真挚款（深厚情谊、长久陪伴与温暖底气）
  const warmList = [
    `岁月漫长，愿这份「${cleanTitle}」替我陪伴你的寻常日子。祝${occasionLabel}快乐，平安喜乐，诸事顺遂！`,
    `感谢一路以来的相伴与关照。挑了这份「${cleanTitle}」送你，愿每一个平凡日常都充满温暖与底气。`,
    `千言万语，尽在礼中。愿这份「${cleanTitle}」能带给你一份切实的笃定与好心情，祝${occasionLabel}如意！`,
  ];

  // 3. 轻松日常款（年轻同辈/好友/清爽不煽情）
  const casualList = [
    `挑了半天觉得这个「${cleanTitle}」最衬你，实用又不啰嗦。${occasionLabel}快乐，吃好喝好，继续开心！`,
    `见物如面！这份「${cleanTitle}」请查收，别客气随便用，祝生活顺风顺水无烦恼！`,
    `一眼相中了这款「${cleanTitle}」，感觉非你莫属。祝${occasionLabel}尽兴，生活常有小确幸！`,
  ];

  return {
    candidate,
    title,
    cleanTitle,
    relationLabel,
    occasionLabel,
    options: {
      custom: customList,
      warm: warmList,
      casual: casualList,
    },
  };
}

function renderLetterCardContent() {
  const badge = $("#letterItemBadge");
  if (badge) {
    badge.textContent = `✦ 「${letterCardState.cleanTitle}」`;
  }

  const recTitle = $("#letterRecipientTitle");
  if (recTitle && !recTitle.dataset.userEdited) {
    recTitle.textContent = `致亲爱的${letterCardState.relationLabel}：`;
  }

  $$("#letterStyleTabs .letter-style-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.style === letterCardState.currentStyle);
  });

  const list = letterCardState.options[letterCardState.currentStyle] || [];
  const idx = (letterCardState.styleIndices[letterCardState.currentStyle] || 0) % (list.length || 1);
  const msgText = list[idx] || `愿这份「${letterCardState.cleanTitle}」，伴你度过每一个温暖美好的日常。祝${letterCardState.occasionLabel}快乐！`;

  const msgEl = $("#letterMessageText");
  if (msgEl) {
    msgEl.textContent = msgText;
  }
}

function openLetterDialogForGift(itemOrTitle) {
  let candidate = null;
  let title = "";
  if (typeof itemOrTitle === "object" && itemOrTitle !== null) {
    candidate = itemOrTitle;
    title = candidate.display_title || candidate.title || candidate.name || "心仪好礼";
  } else {
    title = itemOrTitle || "心仪好礼";
    candidate = (state.result?.candidates || []).find(
      (c) => (c.display_title || c.title || c.name) === title || c.product_id === title
    );
  }

  const generated = buildGreetingOptions(candidate, title);
  letterCardState.candidate = candidate;
  letterCardState.title = generated.title;
  letterCardState.cleanTitle = generated.cleanTitle;
  letterCardState.relationLabel = generated.relationLabel;
  letterCardState.occasionLabel = generated.occasionLabel;
  letterCardState.options = generated.options;
  letterCardState.currentStyle = "custom";
  letterCardState.styleIndices = { custom: 0, warm: 0, casual: 0 };

  const recTitle = $("#letterRecipientTitle");
  if (recTitle) delete recTitle.dataset.userEdited;

  renderLetterCardContent();

  const letterDialog = $("#letterDialog");
  if (letterDialog) letterDialog.showModal();
}

// 风格 Tab 切换
document.addEventListener("click", (e) => {
  const tab = e.target.closest("#letterStyleTabs .letter-style-tab");
  if (!tab) return;
  const style = tab.dataset.style;
  if (!style || style === letterCardState.currentStyle) return;
  letterCardState.currentStyle = style;
  renderLetterCardContent();
});

// 「🎲 换一句」按钮
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#shuffleLetterBtn");
  if (!btn) return;
  const curStyle = letterCardState.currentStyle;
  letterCardState.styleIndices[curStyle] = (letterCardState.styleIndices[curStyle] || 0) + 1;
  renderLetterCardContent();
  showAppToast("🎲 已切换该风格下的不同心意文案！");
});

// 「💬 顾问润色」按钮
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#askAdvisorLetterBtn");
  if (!btn) return;
  const letterDialog = $("#letterDialog");
  if (letterDialog) letterDialog.close();

  setView("chatView");
  const input = $("#chatInput");
  if (input) {
    input.value = `我想送「${letterCardState.cleanTitle}」给我的${letterCardState.relationLabel}（${letterCardState.occasionLabel}），请帮我定制 2 句更有温度、不过分煽情的手写随礼贺卡祝福语。`;
    input.focus();
    $("#chatForm")?.dispatchEvent(new Event("submit", { cancelable: true }));
  }
});

// 「📋 复制贺卡全篇」按钮
if (copyLetterBtn) {
  copyLetterBtn.addEventListener("click", async () => {
    const rec = ($("#letterRecipientTitle")?.textContent || "").trim();
    const body = ($("#letterMessageText")?.textContent || "").trim();
    const sig = ($("#letterSenderSign")?.textContent || "心意随行").trim();

    const fullCard = `${rec}\n\n${body}\n\n—— ${sig}`;
    try {
      await navigator.clipboard.writeText(fullCard);
      showAppToast("📋 贺卡全文（称呼+正文+署名）已复制！");
    } catch {
      showAppToast("复制成功！");
    }
  });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".letter-card-button");
  if (!btn) return;
  const card = btn.closest(".gift-card");
  const cardId = card?.dataset?.cardId;
  const title = btn.dataset.cardTitle || card?.dataset?.title || "心仪好礼";
  const candidate = (state.result?.candidates || []).find(
    (c) => c.product_id === cardId || (c.display_title || c.title || c.name) === title
  );
  openLetterDialogForGift(candidate || title);
});

// 顾问快速问答 Chips 交互绑定
function initChatPromptChips() {
  $$(".prompt-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const prompt = chip.dataset.prompt;
      if (!prompt) return;
      const input = $("#chatInput");
      if (input) {
        input.value = prompt;
        $("#chatForm")?.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });
  });
}
initChatPromptChips();



/* ============================================================
   2026-11-19 · 像素粒子背景
   ------------------------------------------------------------
   为什么是 canvas 而不是 CSS 背景：
   CSS 做不了「随机大小 + 各自不同速度 + 循环回绕」，
   用伪元素堆几十个 div 又会污染 DOM。
   canvas 一层就够，且能用 requestAnimationFrame 精确控帧。

   设计上刻意压得很轻：
   - 方块只有 2~4px，颜色取自 --ink，alpha 0.05~0.12
   - 速度 0.1~0.35 px/帧（60fps 下每秒 6~21px，几乎是静止的）
   - 只在背景，pointer-events: none，不拦任何点击
   - 屏幕越大方块越多，但有上限
   - prefers-reduced-motion 直接不画

   回退：删掉这个函数 + index.html 里的 canvas + styles.css 里的 #pixelField
   ============================================================ */
function initPixelField() {
  const canvas = document.getElementById("pixelField");
  if (!canvas) return;

  // 尊重减少动效偏好：直接不启动
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    canvas.remove();
    return;
  }

  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let w = 0;
  let h = 0;
  let dots = [];
  let raf = 0;
  let stopped = false;

  // 粒子数量按面积算，但设上限 —— 4K 屏也不至于卡
  function count() {
    return Math.min(90, Math.max(30, Math.round((w * h) / 20000)));
  }

  // 粒子调色板：温暖琥珀金、经典印章红、深邃墨黑、清爽薄荷绿
  const FIELD_PALETTE = [
    "rgb(245, 158, 11)",  // 琥珀金
    "rgb(225, 29, 72)",   // 印章红
    "rgb(30, 41, 59)",    // 炭墨深蓝
    "rgb(16, 185, 129)"   // 薄荷绿
  ];

  function makeDots() {
    const n = count();
    dots = [];
    for (let i = 0; i < n; i += 1) {
      dots.push({
        x: Math.random() * w,
        y: Math.random() * h,
        size: 2 + Math.floor(Math.random() * 4),   // 2/3/4/5px 纯正整数方块
        vx: (Math.random() - 0.5) * 0.24,          // 横向轻微漂移
        vy: -0.08 - Math.random() * 0.28,          // 向上漂移
        color: FIELD_PALETTE[Math.floor(Math.random() * FIELD_PALETTE.length)],
        alpha: 0.22 + Math.random() * 0.18         // 0.22 ~ 0.40 清晰通透，极具氛围
      });
    }
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth;
    h = window.innerHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    makeDots();
  }

  function draw() {
    if (stopped) return;
    ctx.clearRect(0, 0, w, h);
    for (let i = 0; i < dots.length; i += 1) {
      const d = dots[i];
      d.x += d.vx;
      d.y += d.vy;

      // 回绕：从上方飘出去就回到下方，从左边出去就从右边进来
      if (d.y < -6) { d.y = h + 6; d.x = Math.random() * w; }
      if (d.x < -6) d.x = w + 6;
      if (d.x > w + 6) d.x = -6;

      // 取整到整数像素，这是「像素感」的关键
      const px = Math.round(d.x);
      const py = Math.round(d.y);

      ctx.globalAlpha = d.alpha;
      ctx.fillStyle = d.color;
      ctx.fillRect(px, py, d.size, d.size);
    }
    ctx.globalAlpha = 1;
    raf = window.requestAnimationFrame(draw);
  }

  resize();
  draw();

  // 窗口变化时重建，防抖 200ms
  let rt = 0;
  window.addEventListener("resize", () => {
    window.clearTimeout(rt);
    rt = window.setTimeout(resize, 200);
  });

  // 页面切到后台就停，省电
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopped = true;
      window.cancelAnimationFrame(raf);
    } else if (!stopped) {
      // 已经停过就不再重启（比如 reduced-motion 移除过 canvas）
    } else {
      stopped = false;
      draw();
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initPixelField);
} else {
  initPixelField();
}

/* ============================================================
2026-11-20 · 点击粒子
------------------------------------------------------------
点哪儿，哪儿炸开一小簇方块 —— 9~14 颗 2~4px 的实心像素，
径向散开、受重力、边飞边缩小，约 0.5 秒消失。

和背景粒子（initPixelField）的分工：
  背景粒子 = 常驻的纸面颗粒，极慢，几乎静止，z-index 0 铺在内容下面
  点击粒子 = 一次性的反馈，快、短、有方向，必须盖在最上层才看得见
所以单独一个 canvas #clickBurst，不复用 #pixelField。

颜色只用两个，不引入新色：
  普通位置    → 墨色 #222924
  主按钮/卡片/入口这类有分量的地方 → 掺一半太阳色 #F4CF51，给一点奖励感

用 pointerdown 而不是 click：按下去就炸，不用等手指抬起。

回退：删掉这个函数 + index.html 的 #clickBurst + styles.css 的 #clickBurst 规则
============================================================ */
function initClickBurst() {
  const canvas = document.getElementById("clickBurst");
  if (!canvas) return;

  // 尊重减少动效偏好：直接不启动，canvas 摘掉
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    canvas.remove();
    return;
  }

  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  // 上限，防止连点时粒子无限堆积拖慢主线程
  const MAX_PARTS = 200;
  const INK = "rgb(28,35,33)";
  const SUN = "rgb(245,158,11)";
  const SEAL = "rgb(225,29,72)";
  const WHITE = "rgb(255,255,255)";

  // 命中这些元素时粒子掺太阳色与印章红 —— 它们是有分量的操作
  const WARM_SELECTOR = [
    ".button.primary",
    ".button.secondary",
    ".select-card-button",
    ".letter-card-button",
    ".gift-card",
    ".choice",
    ".scene",
    ".scene-other",
    ".tab",
    ".seal",
    ".tag-option",
    ".pixel-action-btn",
    ".pixel-skip-btn"
  ].join(",");

  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let w = 0;
  let h = 0;
  let parts = [];
  let raf = 0;
  let running = false;

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth;
    h = window.innerHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function tick() {
    ctx.clearRect(0, 0, w, h);

    for (let i = parts.length - 1; i >= 0; i -= 1) {
      const p = parts[i];

      p.vy += 0.17;      // 重力：先冲出去，再往下坠
      p.vx *= 0.985;     // 空气阻力
      p.x += p.vx;
      p.y += p.vy;
      p.life -= p.decay;

      if (p.life <= 0) {
        parts.splice(i, 1);
        continue;
      }

      // 尺寸随寿命收缩，但始终取整 —— 有小数就会抗锯齿，糊成灰点
      const size = Math.max(1, Math.round(p.size * p.life));
      ctx.globalAlpha = p.life > 0.5 ? 1 : p.life * 2;
      ctx.fillStyle = p.color;
      ctx.fillRect(Math.round(p.x), Math.round(p.y), size, size);
    }

    ctx.globalAlpha = 1;

    if (parts.length > 0) {
      raf = window.requestAnimationFrame(tick);
    } else {
      // 粒子清空后停掉 rAF，不要空转
      running = false;
      ctx.clearRect(0, 0, w, h);
    }
  }

  function burst(x, y, warm) {
    const n = warm ? 16 : 10;
    const colors = warm
      ? [SUN, SUN, SEAL, INK, WHITE]
      : [INK, INK, SUN, SEAL];

    for (let i = 0; i < n; i += 1) {
      if (parts.length >= MAX_PARTS) break;

      // 均匀散开一圈，不要全挤在一个方向
      const angle = Math.random() * Math.PI * 2;
      const speed = 1.2 + Math.random() * 2.8;

      parts.push({
        x: x,
        y: y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed - 0.9,        // 整体上抛一点
        size: 2 + Math.floor(Math.random() * 4),  // 2/3/4/5px 纯正方块
        life: 1,
        decay: 0.026 + Math.random() * 0.022,     // 约 0.45~0.7 秒
        color: colors[Math.floor(Math.random() * colors.length)]
      });
    }

    if (!running) {
      running = true;
      raf = window.requestAnimationFrame(tick);
    }
  }

  window.triggerCustomPixelBurst = function(x, y, count = 36) {
    const colors = [SUN, SUN, SEAL, "#10B981", "#3B82F6", WHITE];
    for (let i = 0; i < count; i += 1) {
      if (parts.length >= MAX_PARTS) break;
      const angle = (Math.PI * 2 * i) / count + (Math.random() - 0.5) * 0.4;
      const speed = 2.0 + Math.random() * 4.5;
      parts.push({
        x: x,
        y: y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed - 1.2,
        size: 2 + Math.floor(Math.random() * 4),
        life: 1,
        decay: 0.018 + Math.random() * 0.018,
        color: colors[Math.floor(Math.random() * colors.length)]
      });
    }
    if (!running) {
      running = true;
      raf = window.requestAnimationFrame(tick);
    }
  };

  document.addEventListener("pointerdown", (event) => {
    // 只认主键；右键菜单不该炸
    if (event.pointerType === "mouse" && event.button !== 0) return;

    const target = event.target;
    const warm = target instanceof Element &&
                 target.closest(WARM_SELECTOR) !== null;

    burst(event.clientX, event.clientY, warm);
  }, { passive: true });

  resize();

  let rt = 0;
  window.addEventListener("resize", () => {
    window.clearTimeout(rt);
    rt = window.setTimeout(resize, 200);
  });

  // 切到后台就清空停掉，回来重新开始
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && running) {
      window.cancelAnimationFrame(raf);
      running = false;
      parts = [];
      ctx.clearRect(0, 0, w, h);
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initClickBurst);
} else {
  initClickBurst();
}
