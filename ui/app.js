const conditionsContainer = document.getElementById("conditionsContainer");
const addConditionBtn = document.getElementById("addCondition");
let nextConditionId = 1;

const TRIGGER_RULE_DEFS = {
  LEVEL_GTE: {
    triggerMode: "LEVEL",
    operator: ">=",
    label: "达到/高于阈值（LEVEL + >=）",
  },
  LEVEL_LTE: {
    triggerMode: "LEVEL",
    operator: "<=",
    label: "达到/低于阈值（LEVEL + <=）",
  },
  CROSS_UP: {
    triggerMode: "CROSS_UP",
    operator: ">=",
    label: "上穿阈值（CROSS_UP + >=）",
  },
  CROSS_DOWN: {
    triggerMode: "CROSS_DOWN",
    operator: "<=",
    label: "下穿阈值（CROSS_DOWN + <=）",
  },
};

const METRIC_TRIGGER_RULES = {
  PRICE: ["LEVEL_GTE", "LEVEL_LTE", "CROSS_UP", "CROSS_DOWN"],
  DRAWDOWN_PCT: ["LEVEL_GTE"],
  RALLY_PCT: ["LEVEL_GTE"],
  VOLUME_RATIO: ["LEVEL_GTE", "LEVEL_LTE"],
  AMOUNT_RATIO: ["LEVEL_GTE", "LEVEL_LTE"],
  SPREAD: ["LEVEL_GTE", "LEVEL_LTE", "CROSS_UP", "CROSS_DOWN"],
};

const METRIC_OPTIONS_BY_TYPE = {
  SINGLE_PRODUCT: [
    { metric: "PRICE", label: "价格（PRICE）", priceReference: "" },
    {
      metric: "DRAWDOWN_PCT",
      label: "回撤比例（DRAWDOWN_PCT，基准=激活后最高价）",
      priceReference: "HIGHEST_SINCE_ACTIVATION",
    },
    {
      metric: "RALLY_PCT",
      label: "上涨比例（RALLY_PCT，基准=激活后最低价）",
      priceReference: "LOWEST_SINCE_ACTIVATION",
    },
  ],
  PAIR_PRODUCTS: [
    { metric: "VOLUME_RATIO", label: "成交量比值（VOLUME_RATIO）", priceReference: "" },
    { metric: "AMOUNT_RATIO", label: "成交额比值（AMOUNT_RATIO）", priceReference: "" },
    { metric: "SPREAD", label: "价差（SPREAD）", priceReference: "" },
  ],
};

const METRIC_VALUE_TYPES = {
  PRICE: "USD",
  SPREAD: "USD",
  DRAWDOWN_PCT: "RATIO",
  RALLY_PCT: "RATIO",
  VOLUME_RATIO: "RATIO",
  AMOUNT_RATIO: "RATIO",
};

const FAST_EVALUATION_WINDOWS = ["1m", "2m", "5m"];
const RATIO_EVALUATION_WINDOWS = ["1h", "2h", "4h", "1d", "2d", "5d"];

function isRatioMetric(metric) {
  return metric === "VOLUME_RATIO" || metric === "AMOUNT_RATIO";
}

function getEvaluationWindowOptions(metric) {
  return isRatioMetric(metric) ? RATIO_EVALUATION_WINDOWS : FAST_EVALUATION_WINDOWS;
}

function getConditionType(row) {
  const checkedTypeRadio = row.querySelector(".condition-type-radio:checked");
  return checkedTypeRadio ? checkedTypeRadio.value : "SINGLE_PRODUCT";
}

function syncPriceReferenceByMetric(row) {
  const metricSelect = row.querySelector(".metric-select");
  const priceReferenceInput = row.querySelector(".price-reference-input");
  if (!metricSelect || !priceReferenceInput) return;

  const selected = metricSelect.selectedOptions[0];
  priceReferenceInput.value = selected?.dataset.priceReference || "";
}

function renderMetricOptionsByType(row, preferredMetric) {
  const metricSelect = row.querySelector(".metric-select");
  if (!metricSelect) return;

  const conditionType = getConditionType(row);
  const optionDefs = METRIC_OPTIONS_BY_TYPE[conditionType] || METRIC_OPTIONS_BY_TYPE.SINGLE_PRODUCT;
  const allowedMetrics = optionDefs.map((item) => item.metric);
  const finalMetric = allowedMetrics.includes(preferredMetric) ? preferredMetric : optionDefs[0].metric;

  metricSelect.innerHTML = optionDefs
    .map((item) => {
      const selected = item.metric === finalMetric ? " selected" : "";
      return `<option value="${item.metric}" data-price-reference="${item.priceReference}"${selected}>${item.label}</option>`;
    })
    .join("");

  syncPriceReferenceByMetric(row);
}

function syncConditionSpecificFields(row) {
  const conditionType = getConditionType(row);
  const singleProductFields = row.querySelectorAll(".single-product-field");
  const pairFields = row.querySelectorAll(".pair-field");
  const isSingle = conditionType === "SINGLE_PRODUCT";

  singleProductFields.forEach((el) => el.classList.toggle("d-none", !isSingle));
  pairFields.forEach((el) => el.classList.toggle("d-none", isSingle));
  syncPriceReferenceByMetric(row);
}

function syncTriggerHiddenFields(row, ruleKey) {
  const triggerModeInput = row.querySelector(".trigger-mode-input");
  const operatorInput = row.querySelector(".operator-input");
  const ruleDef = TRIGGER_RULE_DEFS[ruleKey] || TRIGGER_RULE_DEFS.LEVEL_GTE;
  if (triggerModeInput) triggerModeInput.value = ruleDef.triggerMode;
  if (operatorInput) operatorInput.value = ruleDef.operator;
}

function renderTriggerRulesByMetric(row, preferredRuleKey) {
  const metricSelect = row.querySelector(".metric-select");
  const triggerRuleSelect = row.querySelector(".trigger-rule");
  if (!metricSelect || !triggerRuleSelect) return;

  const metric = metricSelect.value;
  const allowedRules = METRIC_TRIGGER_RULES[metric] || METRIC_TRIGGER_RULES.PRICE;
  const finalRule = allowedRules.includes(preferredRuleKey) ? preferredRuleKey : allowedRules[0];

  triggerRuleSelect.innerHTML = allowedRules
    .map((ruleKey) => {
      const selected = ruleKey === finalRule ? " selected" : "";
      const label = TRIGGER_RULE_DEFS[ruleKey].label;
      return `<option value="${ruleKey}"${selected}>${label}</option>`;
    })
    .join("");

  syncTriggerHiddenFields(row, finalRule);
}

function syncValueFieldByMetric(row) {
  const metricSelect = row.querySelector(".metric-select");
  const valueInput = row.querySelector(".value-input");
  const valueUnit = row.querySelector(".value-unit");
  const valueTypeInput = row.querySelector(".value-type-input");
  if (!metricSelect || !valueInput || !valueUnit || !valueTypeInput) return;

  const metric = metricSelect.value;
  const valueType = METRIC_VALUE_TYPES[metric] || "USD";

  if (valueType === "RATIO") {
    valueInput.placeholder = "例如 10";
    valueUnit.textContent = "%";
  } else {
    valueInput.placeholder = "例如 88.5";
    valueUnit.textContent = "$";
  }

  valueTypeInput.value = valueType;
}

function renderEvaluationWindowsByMetric(row, preferredWindow) {
  const metricSelect = row.querySelector(".metric-select");
  const evaluationWindowSelect = row.querySelector(".evaluation-window-select");
  if (!metricSelect || !evaluationWindowSelect) return;

  const options = getEvaluationWindowOptions(metricSelect.value);
  const finalValue = options.includes(preferredWindow) ? preferredWindow : options[0];
  evaluationWindowSelect.innerHTML = options
    .map((windowValue) => {
      const selected = windowValue === finalValue ? " selected" : "";
      return `<option value="${windowValue}"${selected}>${windowValue}</option>`;
    })
    .join("");
}

function conditionRowTemplate(conditionId) {
  const typeName = `condition-type-${conditionId}`;
  return `
    <div class="condition-row border rounded p-2" data-condition-id="${conditionId}">
      <div class="row g-2 align-items-end">
        <div class="col-md-2">
          <label class="form-label small mb-1">condition_id（自动生成）</label>
          <input class="form-control form-control-sm condition-id-input" value="${conditionId}" readonly />
        </div>
        <div class="col-md-3">
          <label class="form-label small mb-1">type（单选）</label>
          <div class="btn-group btn-group-sm w-100" role="group" aria-label="condition type">
            <input type="radio" class="btn-check condition-type-radio" name="${typeName}" id="${typeName}-single" value="SINGLE_PRODUCT" checked />
            <label class="btn btn-outline-light" for="${typeName}-single">单产品</label>

            <input type="radio" class="btn-check condition-type-radio" name="${typeName}" id="${typeName}-pair" value="PAIR_PRODUCTS" />
            <label class="btn btn-outline-light" for="${typeName}-pair">双产品</label>
          </div>
        </div>
        <div class="col-md-3">
          <div class="single-product-field">
            <label class="form-label small mb-1">product</label>
            <input class="form-control form-control-sm" placeholder="SLV" />
          </div>
          <div class="pair-field d-none">
            <label class="form-label small mb-1">products（A / B）</label>
            <div class="input-group input-group-sm">
              <input class="form-control" placeholder="product (SPY)" />
              <input class="form-control" placeholder="product_b (QQQ)" />
            </div>
          </div>
        </div>
        <div class="col-md-2">
          <label class="form-label small mb-1">evaluation_window</label>
          <select class="form-select form-select-sm evaluation-window-select">
            <option>1m</option>
            <option>2m</option>
            <option>5m</option>
          </select>
        </div>
        <div class="col-md-2">
          <label class="form-label small mb-1">window_price_basis</label>
          <select class="form-select form-select-sm">
            <option value="CLOSE" selected>收盘价（CLOSE）</option>
            <option value="HIGH">最高价（HIGH）</option>
            <option value="LOW">最低价（LOW）</option>
            <option value="AVG">平均价（AVG）</option>
          </select>
        </div>

        <div class="col-12"></div>

        <div class="col-md-4">
          <label class="form-label small mb-1">指标（含基准）</label>
          <select class="form-select form-select-sm metric-select">
            <option value="PRICE" data-price-reference="">价格（PRICE）</option>
            <option value="DRAWDOWN_PCT" data-price-reference="HIGHEST_SINCE_ACTIVATION">回撤比例（DRAWDOWN_PCT，基准=激活后最高价）</option>
            <option value="RALLY_PCT" data-price-reference="LOWEST_SINCE_ACTIVATION">上涨比例（RALLY_PCT，基准=激活后最低价）</option>
          </select>
          <input type="hidden" class="price-reference-input" value="" />
        </div>
        <div class="col-md-4">
          <label class="form-label small mb-1">触发判定（随指标变化）</label>
          <select class="form-select form-select-sm trigger-rule">
            <option value="LEVEL_GTE">达到/高于阈值（LEVEL + &gt;=）</option>
          </select>
          <input type="hidden" class="trigger-mode-input" value="LEVEL" />
          <input type="hidden" class="operator-input" value=">=" />
        </div>
        <div class="col-md-3">
          <label class="form-label small mb-1">value</label>
          <div class="input-group input-group-sm">
            <input class="form-control value-input" placeholder="例如 88.5" />
            <span class="input-group-text value-unit">$</span>
          </div>
          <input type="hidden" class="value-type-input" value="USD" />
        </div>
        <div class="col-md-1 d-flex align-items-end">
          <button class="btn btn-sm btn-outline-danger w-100 remove-condition">删</button>
        </div>
      </div>
    </div>
  `;
}

function addConditionRow() {
  const conditionId = `c${nextConditionId}`;
  nextConditionId += 1;
  conditionsContainer.insertAdjacentHTML("beforeend", conditionRowTemplate(conditionId));
  const row = conditionsContainer.lastElementChild;
  if (row) {
    renderMetricOptionsByType(row);
    renderEvaluationWindowsByMetric(row);
    renderTriggerRulesByMetric(row);
    syncValueFieldByMetric(row);
    syncConditionSpecificFields(row);
  }
}

function bindConditionEditorNavigation() {
  const params = new URLSearchParams(window.location.search);
  const strategyId = params.get("id") || "S-NEW";

  document.querySelectorAll("[data-strategy-id]").forEach((el) => {
    el.textContent = strategyId;
  });

  const saveBtn = document.getElementById("saveConditionsBtn");
  const cancelLink = document.getElementById("cancelConditionsLink");
  const backLink = document.getElementById("backFromConditionsLink");

  function detailHref(conditionsConfigured) {
    const next = new URLSearchParams(params);
    next.set("id", strategyId);
    if (!next.has("draft")) next.set("draft", "1");
    if (!next.has("actions")) next.set("actions", "0");
    next.set("conditions", conditionsConfigured ? "1" : next.get("conditions") || "0");
    return `./strategy-detail.html?${next.toString()}`;
  }

  const cancelHref = detailHref(false);
  if (cancelLink) cancelLink.href = cancelHref;
  if (backLink) backLink.href = cancelHref;

  if (saveBtn) {
    saveBtn.addEventListener("click", () => {
      window.location.href = detailHref(true);
    });
  }
}

if (conditionsContainer && addConditionBtn) {
  addConditionBtn.addEventListener("click", (event) => {
    event.preventDefault();
    addConditionRow();
  });

  conditionsContainer.addEventListener("change", (event) => {
    const row = event.target.closest(".condition-row");
    if (!row) return;

    if (event.target.classList.contains("condition-type-radio")) {
      const previousMetric = row.querySelector(".metric-select")?.value;
      const previousWindow = row.querySelector(".evaluation-window-select")?.value;
      renderMetricOptionsByType(row, previousMetric);
      renderEvaluationWindowsByMetric(row, previousWindow);
      renderTriggerRulesByMetric(row);
      syncValueFieldByMetric(row);
      syncConditionSpecificFields(row);
    }

    if (event.target.classList.contains("metric-select")) {
      const previousWindow = row.querySelector(".evaluation-window-select")?.value;
      renderEvaluationWindowsByMetric(row, previousWindow);
      renderTriggerRulesByMetric(row);
      syncValueFieldByMetric(row);
      syncConditionSpecificFields(row);
    }

    if (event.target.classList.contains("trigger-rule")) {
      syncTriggerHiddenFields(row, event.target.value);
    }
  });

  conditionsContainer.addEventListener("click", (event) => {
    if (!event.target.classList.contains("remove-condition")) return;
    event.preventDefault();
    event.target.closest(".condition-row").remove();
  });

  addConditionRow();
  bindConditionEditorNavigation();
}
