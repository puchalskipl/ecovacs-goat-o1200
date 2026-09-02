/**
 * Ecovacs GOAT Card
 * -----------------
 * No-build Lovelace card for the ECOVACS GOAT mower integration.
 *
 * YAML usage:
 *   type: custom:ecovacs-goat-card
 *   entity: lawn_mower.mower
 *   battery_entity: sensor.mower_battery_level
 *   area_entity: sensor.mower_mowing_area
 *   progress_entity: sensor.mower_mowing_progress
 *   error_entity: sensor.mower_error
 *   map_entity: sensor.mower_live_map
 *   stop_button: button.mower_end_mowing
 *   trim_button: button.mower_start_edge_trimming
 */

const DEFAULT_CONFIG = {
  entity: "lawn_mower.mower",
  battery_entity: "sensor.mower_battery_level",
  area_entity: "sensor.mower_mowing_area",
  progress_entity: "sensor.mower_mowing_progress",
  error_entity: "sensor.mower_error",
  map_entity: "sensor.mower_live_map",
  stop_button: "button.mower_end_mowing",
  trim_button: "button.mower_start_edge_trimming",
  name: "Mower",
  // Cap the rendered map height (CSS px). The SVG scales with the card
  // width, so in a wide dashboard column an uncapped map fills the screen
  // and pushes the action buttons below the fold.
  map_max_height: 380,
  // Compact-mode switches: the dashboard can surface state, metrics and
  // controls through its own cards and reduce this card to just the map.
  show_header: true,
  show_summary: true,
  show_buttons: true,
};

const CARD_FORMAT_VERSION = "rounded-summary-v2";
const LIVE_STREAM_KEEPALIVE_MS = 65000;
const KEEPALIVE_DURATION_SECONDS = 600;

const STATE_META = {
  mowing: { label: "state_mowing", className: "active", icon: "mdi:robot-mower" },
  paused: { label: "state_paused", className: "paused", icon: "mdi:pause-circle" },
  docked: { label: "state_docked", className: "docked", icon: "mdi:home-lightning-bolt" },
  returning: { label: "state_returning", className: "returning", icon: "mdi:home-import-outline" },
  idle: { label: "state_idle", className: "paused", icon: "mdi:map-marker-alert-outline" },
  error: { label: "state_error", className: "error", icon: "mdi:alert-circle" },
  unavailable: { label: "state_unavailable", className: "muted", icon: "mdi:cloud-alert" },
  unknown: { label: "state_unknown", className: "muted", icon: "mdi:help-circle" },
};

// UI strings per language; the card follows the Home Assistant profile
// language and falls back to English.
const I18N = {
  en: {
    state_mowing: "Mowing",
    state_paused: "Paused",
    state_docked: "Docked",
    state_returning: "Returning",
    state_idle: "Idle",
    state_error: "Error",
    state_unavailable: "Unavailable",
    state_unknown: "Unknown",
    start: "Start",
    resume: "Resume",
    pause: "Pause",
    end: "End",
    dock: "Dock",
    cancel_dock: "Cancel Dock",
    trim: "Trim edges",
    mowing_area: "Mowing area",
    progress: "Progress",
    battery: "Battery",
    waiting_map: "Waiting for live map data",
  },
  pl: {
    state_mowing: "Kosi",
    state_paused: "Wstrzymana",
    state_docked: "W stacji",
    state_returning: "Wraca",
    state_idle: "Bezczynna",
    state_error: "Błąd",
    state_unavailable: "Niedostępna",
    state_unknown: "Nieznany",
    start: "Start",
    resume: "Wznów",
    pause: "Wstrzymaj",
    end: "Zakończ",
    dock: "Do stacji",
    cancel_dock: "Anuluj powrót",
    trim: "Przytnij krawędzie",
    mowing_area: "Obszar koszenia",
    progress: "Postęp",
    battery: "Bateria",
    waiting_map: "Oczekiwanie na dane mapy",
  },
};

const ACTION_OUTCOMES = {
  start: {
    domain: "lawn_mower",
    service: "start_mowing",
    target: "entity",
    optimisticState: "mowing",
    expectedStates: ["mowing"],
    retryAfterMs: 5000,
    timeoutMs: 18000,
    failureMessage: "Mower did not start mowing.",
  },
  pause: {
    domain: "lawn_mower",
    service: "pause",
    target: "entity",
    optimisticState: "paused",
    expectedStates: ["paused"],
    retryAfterMs: 5000,
    timeoutMs: 18000,
    failureMessage: "Mower did not pause.",
  },
  end: {
    domain: "button",
    service: "press",
    target: "stop_button",
    optimisticState: "idle",
    expectedStates: ["idle", "docked"],
    retryAfterMs: 5000,
    timeoutMs: 18000,
    failureMessage: "Mower did not end mowing.",
  },
  dock: {
    domain: "lawn_mower",
    service: "dock",
    target: "entity",
    optimisticState: "returning",
    expectedStates: ["docked"],
    settlingStates: ["returning"],
    retryAfterMs: 5000,
    timeoutMs: 240000,
    failureMessage: "Mower did not start returning to dock.",
  },
  cancel_dock: {
    domain: "lawn_mower",
    service: "dock",
    target: "entity",
    optimisticState: "paused",
    expectedStates: ["paused", "mowing", "idle"],
    // lawn_mower.dock is a toggle here: retrying it while the mower is
    // still slowing down would START a new return-to-dock instead.
    retryAfterMs: null,
    timeoutMs: 30000,
    failureMessage: "Mower did not cancel docking.",
  },
  trim: {
    domain: "button",
    service: "press",
    target: "trim_button",
    optimisticState: "mowing",
    expectedStates: ["mowing"],
    retryAfterMs: 5000,
    timeoutMs: 30000,
    failureMessage: "Mower did not start edge trimming.",
  },
};

const STYLE = `
  :host {
    display: block;
  }
  ha-card {
    padding: 16px;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .title {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .title h2 {
    margin: 0;
    font-size: 1.15rem;
    font-weight: 500;
  }
  .title ha-icon {
    color: var(--primary-color);
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border-radius: 999px;
    padding: 4px 10px;
    font-size: 0.85rem;
    font-weight: 600;
    background: var(--secondary-background-color);
    color: var(--primary-text-color);
    white-space: nowrap;
  }
  .chip.active {
    background: var(--success-color, #43a047);
    color: #fff;
  }
  .chip.paused,
  .chip.returning {
    background: var(--warning-color, #f9a825);
    color: #111;
  }
  .chip.docked {
    background: var(--info-color, #0288d1);
    color: #fff;
  }
  .chip.error {
    background: var(--error-color, #d32f2f);
    color: #fff;
  }
  .chip.muted {
    color: var(--secondary-text-color);
  }
  .chip.pending {
    box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.28) inset;
  }
  .summary {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    margin: 14px 0;
  }
  .metric {
    padding: 10px;
    border: 1px solid var(--divider-color);
    border-radius: 10px;
    background: var(--secondary-background-color);
  }
  .metric .label {
    color: var(--secondary-text-color);
    font-size: 0.78rem;
    margin-bottom: 4px;
  }
  .metric .value {
    font-size: 1.05rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .error-line {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin: 8px 0 12px;
    padding: 8px 10px;
    border-radius: 8px;
    background: rgba(211, 47, 47, 0.12);
    color: var(--error-color, #d32f2f);
  }
  .map {
    margin: 0 0 14px;
    border: 1px solid var(--divider-color);
    border-radius: 12px;
    overflow: hidden;
    background: var(--secondary-background-color);
  }
  .map-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    color: var(--secondary-text-color);
    font-size: 0.85rem;
  }
  .map-title {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--primary-text-color);
    font-weight: 600;
  }
  .map svg {
    display: block;
    width: 100%;
    height: auto;
    min-height: 220px;
    max-height: var(--goat-map-max-height, 380px);
    background: #dfe5ec;
  }
  .map-empty {
    padding: 20px 12px;
    color: var(--secondary-text-color);
    text-align: center;
  }
  /* What is still to be cut: the mower's own remaining lanes, drawn as the
     app draws them — hatching over the lawn that is rubbed out lane by lane.
     Clipped to the lawn so a lane that overhangs the border stays inside. */
  .map-pending {
    fill: none;
    stroke: #4b9a33;
    stroke-width: 0.8;
    stroke-linecap: butt;
    opacity: 0.5;
  }
  /* The lawn: outline filled, obstacles punched out as holes by evenodd. */
  .map-lawn {
    fill: #b7e394;
    stroke: none;
  }
  .map-lawn-outline {
    fill: none;
    stroke: #3e8e28;
    stroke-width: 3;
    stroke-linejoin: round;
  }
  /* Boundary already gone over by the edge lap. The app turns it white as
     the mower works round; sampled straight from a screenshot of one. */
  .map-lawn-outline-done {
    stroke: #fcfcfc;
  }
  /* Edge lap still to drive, painted over the pale boundary. */
  .map-border-pending {
    fill: none;
    stroke: #3e8e28;
    stroke-width: 3;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .map-obstacle-edge {
    fill: #6f9c4e;
    stroke: #4b7a35;
    stroke-width: 1;
    stroke-linejoin: round;
    opacity: 0.9;
  }
  .map-station {
    fill: #263847;
    stroke: #fff;
    stroke-width: 2;
  }
  .map-beacon {
    fill: #5772e8;
    stroke: #fff;
    stroke-width: 3;
  }
  .map-rtk-station {
    fill: #f5a623;
    stroke: #fff;
    stroke-width: 2;
  }
  .map-nogo {
    fill: rgba(229, 57, 53, 0.18);
    stroke: #e53935;
    stroke-width: 1.5;
    stroke-dasharray: 4 3;
  }
  /* Mower marker shaped like the app's: a rounded body with a lighter dot
     showing which way it faces, rather than a bare arrowhead. */
  .map-mower-body {
    fill: #1f3d57;
    stroke: #ffffff;
    stroke-width: 2;
  }
  .map-mower-eye {
    fill: #ffffff;
    opacity: 0.9;
  }
  .actions {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 8px;
  }
  button {
    min-height: 46px;
    border: 0;
    border-radius: 10px;
    background: var(--secondary-background-color);
    color: var(--primary-text-color);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font: inherit;
    position: relative;
    transition:
      background-color 120ms ease,
      box-shadow 120ms ease,
      filter 120ms ease,
      opacity 120ms ease,
      transform 80ms ease;
  }
  button:active:not(:disabled) {
    filter: brightness(0.82);
    transform: translateY(1px) scale(0.98);
  }
  button.pending {
    box-shadow: inset 0 0 0 999px rgba(255, 255, 255, 0.18);
    filter: saturate(1.15);
  }
  button.pending::after {
    content: "";
    width: 10px;
    height: 10px;
    border: 2px solid currentColor;
    border-top-color: transparent;
    border-radius: 50%;
    animation: ecovacs-goat-spin 0.8s linear infinite;
  }
  @keyframes ecovacs-goat-spin {
    to {
      transform: rotate(360deg);
    }
  }
  button.primary {
    background: var(--primary-color);
    color: var(--text-primary-color);
  }
  button.pause {
    background: var(--warning-color, #f9a825);
    color: #111;
  }
  button.stop {
    background: var(--error-color, #d32f2f);
    color: #fff;
  }
  button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .warning {
    color: var(--error-color, #d32f2f);
    padding: 8px 0 0;
  }
  @media (max-width: 450px) {
    .summary {
      grid-template-columns: 1fr;
    }
    .actions {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
`;

class EcovacsGoatCard extends HTMLElement {
  connectedCallback() {
    this._setupVisibilityTracking();
    this._maybeRequestLivePositionStream("card_connected");
    // Renew the live stream periodically, but only while the card is
    // actually visible — _maybeRequestLivePositionStream gates on tab and
    // viewport visibility, so a closed/hidden card sends nothing. The mower
    // accumulates the mowed area itself, so a card opened later still shows
    // everything (matching the official app's model).
    if (!this._liveStreamTimer) {
      this._liveStreamTimer = window.setInterval(
        () => this._maybeRequestLivePositionStream("card_interval"),
        LIVE_STREAM_KEEPALIVE_MS
      );
    }
  }

  disconnectedCallback() {
    window.clearInterval(this._liveStreamTimer);
    this._liveStreamTimer = null;
    // Pending action timers would otherwise keep firing service calls from a
    // detached card (e.g. retrying dock after the user switched views).
    this._clearPendingAction();
    if (this._visibilityHandler) {
      document.removeEventListener("visibilitychange", this._visibilityHandler);
      this._visibilityHandler = null;
    }
    if (this._intersectionObserver) {
      this._intersectionObserver.disconnect();
      this._intersectionObserver = null;
    }
  }

  setConfig(config) {
    this.config = { ...DEFAULT_CONFIG, ...config };
  }

  set hass(hass) {
    this._hass = hass;
    this._checkPendingOutcome();
    if (!this.config || !this.querySelector("ha-card")) {
      this.render();
      return;
    }
    const signature = this._nonMapSignature();
    if (signature === this._nonMapSignatureValue) {
      this._updateMapOnly();
      return;
    }
    this.render();
  }

  getCardSize() {
    return 4;
  }

  render() {
    if (!this.config || !this._hass) {
      return;
    }

    const mower = this._state(this.config.entity);
    const actualState = mower?.state || "unavailable";
    const state = this._displayState(actualState);
    const meta = STATE_META[state] || {
      label: this._label(state),
      className: "muted",
      icon: "mdi:robot-mower-outline",
    };
    const pendingAction = this._pendingAction?.key;
    const chipClass = `${meta.className}${pendingAction ? " pending" : ""}`;

    const area = this._state(this.config.area_entity);
    const progress = this._state(this.config.progress_entity);
    const battery = this._state(this.config.battery_entity);
    const error = this._state(this.config.error_entity);
    const map = this._state(this.config.map_entity);
    const unavailable = !mower || state === "unavailable";
    const returning = state === "returning";
    const mowing = state === "mowing";
    const paused = state === "paused";
    const docked = state === "docked";
    const errorState = state === "error";
    const primaryDisabled = unavailable || returning;
    const endDisabled = unavailable || returning || (!mowing && !paused && !errorState);
    const dockDisabled = unavailable || docked;
    const primaryAction = mowing ? "pause" : "start";
    const primaryLabel = mowing
      ? this._t("pause")
      : paused
        ? this._t("resume")
        : this._t("start");
    const primaryIcon = mowing ? "mdi:pause" : "mdi:play";
    const primaryClass = mowing ? "pause" : "primary";
    const dockLabel = returning ? this._t("cancel_dock") : this._t("dock");
    const dockIcon = returning ? "mdi:home-export-outline" : "mdi:home-import-outline";
    const dockAction = returning ? "cancel_dock" : "dock";
    // Edge trimming is a standalone job: only startable while idle/docked.
    const trimDisabled = unavailable || mowing || paused || returning;

    const mapMaxHeight = Number(this.config.map_max_height) > 0
      ? Number(this.config.map_max_height)
      : 380;
    const headerHtml = this.config.show_header
      ? `
        <div class="header">
          <div class="title">
            <ha-icon icon="mdi:robot-mower-outline"></ha-icon>
            <h2>${this._escape(this.config.name || this._friendlyName(mower) || "Mower")}</h2>
          </div>
          <div class="chip ${chipClass}">
            <ha-icon icon="${meta.icon}"></ha-icon>
            ${this._escape(this._t(meta.label))}
          </div>
        </div>`
      : "";
    const summaryHtml = this.config.show_summary
      ? `
        <div class="summary">
          ${this._metric(this._t("mowing_area"), this._formatAreaState(area))}
          ${this._metric(this._t("progress"), this._formatRoundedState(progress))}
          ${this._metric(this._t("battery"), this._formatState(battery))}
        </div>`
      : "";
    const actionsHtml = this.config.show_buttons
      ? `
        <div class="actions">
          <button class="${this._buttonClass(primaryClass, primaryAction)}" data-action="${primaryAction}" ${primaryDisabled ? "disabled" : ""}>
            <ha-icon icon="${primaryIcon}"></ha-icon>
            <span>${primaryLabel}</span>
          </button>
          <button class="${this._buttonClass("stop", "end")}" data-action="end" ${endDisabled ? "disabled" : ""}>
            <ha-icon icon="mdi:stop"></ha-icon>
            <span>${this._t("end")}</span>
          </button>
          <button class="${this._buttonClass("", dockAction)}" data-action="${dockAction}" ${dockDisabled ? "disabled" : ""}>
            <ha-icon icon="${dockIcon}"></ha-icon>
            <span>${dockLabel}</span>
          </button>
          <button class="${this._buttonClass("", "trim")}" data-action="trim" ${trimDisabled ? "disabled" : ""}>
            <ha-icon icon="mdi:scissors-cutting"></ha-icon>
            <span>${this._t("trim")}</span>
          </button>
        </div>`
      : "";

    this.innerHTML = `
      <ha-card style="--goat-map-max-height: ${mapMaxHeight}px;">
        <style>${STYLE}</style>
        ${headerHtml}
        ${summaryHtml}
        ${this._errorLine(error)}

        <div class="map-slot">${this._mapPanel(map, actualState)}</div>
        ${actionsHtml}
      </ha-card>
    `;

    this.querySelector('[data-action="start"]')?.addEventListener("click", () =>
      this._runStateAction("start")
    );
    this.querySelector('[data-action="pause"]')?.addEventListener("click", () =>
      this._runStateAction("pause")
    );
    this.querySelector('[data-action="end"]')?.addEventListener("click", () =>
      this._runStateAction("end")
    );
    this.querySelector('[data-action="dock"]')?.addEventListener("click", () =>
      this._runStateAction("dock")
    );
    this.querySelector('[data-action="cancel_dock"]')?.addEventListener("click", () =>
      this._runStateAction("cancel_dock")
    );
    this.querySelector('[data-action="trim"]')?.addEventListener("click", () =>
      this._runStateAction("trim")
    );
    this._nonMapSignatureValue = this._nonMapSignature();
  }

  _state(entityId) {
    return entityId ? this._hass.states[entityId] : undefined;
  }

  _displayState(actualState) {
    if (
      this._pendingAction?.type === "state" &&
      this._pendingAction.optimisticState &&
      !this._stateOutcomeReached(this._pendingAction)
    ) {
      return this._pendingAction.optimisticState;
    }
    return actualState;
  }

  _buttonClass(baseClass, key) {
    return [baseClass, this._pendingAction?.key === key ? "pending" : ""]
      .filter(Boolean)
      .join(" ");
  }

  _nonMapSignature() {
    const entityState = (entityId) => {
      const state = this._state(entityId);
      return [
        entityId,
        state?.state,
        state?.attributes?.unit_of_measurement,
        state?.attributes?.description,
        state?.attributes?.friendly_name,
      ];
    };
    return JSON.stringify([
      CARD_FORMAT_VERSION,
      entityState(this.config.entity),
      entityState(this.config.area_entity),
      entityState(this.config.progress_entity),
      entityState(this.config.battery_entity),
      entityState(this.config.error_entity),
      this._pendingAction?.key,
      this._pendingAction?.optimisticState,
    ]);
  }

  _updateMapOnly() {
    const slot = this.querySelector(".map-slot");
    if (!slot) {
      this.render();
      return;
    }
    const mower = this._state(this.config.entity);
    // The map must follow the REAL mower state, not the optimistic one: an
    // optimistic "mowing" after a failed start would wipe the pending lanes
    // before the mower ever moved.
    const mowerState = mower?.state || "unavailable";
    const mapState = this._state(this.config.map_entity);
    const resolved = this._resolveMapData(mapState, mowerState);

    // Skip all DOM work when the underlying map data is unchanged. HA calls
    // `set hass` on every entity update, many of which never touch the map.
    const dataSignature = this._mapDataSignature(resolved, mowerState);
    if (dataSignature === this._lastMapDataSignature) {
      return;
    }

    const render = this._computeMapRender(resolved, mowerState);
    const svg = slot.querySelector("svg");
    // When only the dynamic layers changed (pending lanes, mower marker)
    // patch them in place instead of rebuilding the whole SVG. Tearing down
    // and recreating the SVG (and its background) on every frame is what
    // causes the visible flicker while mowing.
    if (
      render &&
      !render.empty &&
      svg &&
      render.structureSignature === this._lastMapStructureSignature &&
      this._patchMapSvg(svg, render)
    ) {
      this._lastMapDataSignature = dataSignature;
      return;
    }

    if (!render) {
      slot.innerHTML = "";
      this._lastMapStructureSignature = null;
    } else if (render.empty) {
      slot.innerHTML = `<div class="map"><div class="map-empty">${this._t("waiting_map")}</div></div>`;
      this._lastMapStructureSignature = null;
    } else {
      slot.innerHTML = `<div class="map">${this._buildMapSvgHtml(render)}</div>`;
      this._lastMapStructureSignature = render.structureSignature;
    }
    this._lastMapDataSignature = dataSignature;
  }

  _runStateAction(key) {
    const outcome = ACTION_OUTCOMES[key];
    const entityId = outcome?.target ? this.config[outcome.target] : undefined;
    if (!outcome || !entityId) {
      return;
    }

    this._beginPendingAction({
      key,
      type: "state",
      domain: outcome.domain,
      service: outcome.service,
      entityId,
      optimisticState: outcome.optimisticState,
      expectedStates: outcome.expectedStates,
      settlingStates: outcome.settlingStates,
      alreadySatisfied: outcome.expectedStates?.includes(this._state(this.config.entity)?.state),
      retryAfterMs: outcome.retryAfterMs,
      timeoutMs: outcome.timeoutMs,
      failureMessage: outcome.failureMessage,
      attempts: 1,
    });
    this._callAction(outcome.domain, outcome.service, entityId)
      .then(() => {
        if (["start", "end", "dock", "cancel_dock", "trim"].includes(key)) {
          this._startKeepalive(`card_${key}`);
        }
      })
      .catch(() => this._failPendingAction(key, outcome.failureMessage));
  }

  _startKeepalive(reason) {
    // After a user action, ask the integration for an app-style live window
    // so the map animates immediately; failures are non-critical (the
    // integration's auto_live_map keepalive covers mowing regardless).
    this._maybeRequestLivePositionStream(reason, true, KEEPALIVE_DURATION_SECONDS);
  }

  _beginPendingAction(action) {
    this._clearPendingAction();
    this._pendingAction = action;
    this.render();

    if (action.alreadySatisfied) {
      action.timeoutTimer = window.setTimeout(() => {
        if (this._pendingAction?.key === action.key) {
          this._clearPendingAction();
          this.render();
        }
      }, 900);
      return;
    }

    if (action.retryAfterMs) {
      action.retryTimer = window.setTimeout(() => {
        if (!this._pendingAction || this._pendingAction.key !== action.key) {
          return;
        }
        if (
          this._pendingOutcomeReached(action) ||
          this._pendingActionIsSettling(action) ||
          action.attempts >= 2
        ) {
          return;
        }
        action.attempts += 1;
        this._callAction(action.domain, action.service, action.entityId, action.serviceData).catch(
          () => this._failPendingAction(action.key, action.failureMessage)
        );
      }, action.retryAfterMs);
    }

    action.timeoutTimer = window.setTimeout(() => {
      if (!this._pendingAction || this._pendingAction.key !== action.key) {
        return;
      }
      if (this._pendingOutcomeReached(action)) {
        this._clearPendingAction();
        this.render();
        return;
      }
      this._failPendingAction(action.key, action.failureMessage);
    }, action.timeoutMs);
  }

  _checkPendingOutcome() {
    if (this._pendingAction && this._pendingOutcomeReached(this._pendingAction)) {
      this._clearPendingAction();
    }
  }

  _pendingOutcomeReached(action) {
    if (action.type === "state") {
      return this._stateOutcomeReached(action);
    }
    return false;
  }

  _pendingActionIsSettling(action) {
    if (action.type !== "state") {
      return false;
    }
    return action.settlingStates?.includes(this._state(this.config.entity)?.state);
  }

  _stateOutcomeReached(action) {
    const state = this._state(this.config.entity)?.state;
    return action.expectedStates?.includes(state);
  }

  _failPendingAction(key, message) {
    if (this._pendingAction?.key !== key) {
      return;
    }
    this._clearPendingAction();
    this._showToast(message);
    this.render();
  }

  _clearPendingAction() {
    if (!this._pendingAction) {
      return;
    }
    window.clearTimeout(this._pendingAction.retryTimer);
    window.clearTimeout(this._pendingAction.timeoutTimer);
    this._pendingAction = null;
  }

  _callAction(domain, service, entityId, data = {}) {
    return this._hass.callService(domain, service, {
      entity_id: entityId,
      ...data,
    });
  }

  _setupVisibilityTracking() {
    if (!this._visibilityHandler) {
      this._visibilityHandler = () =>
        this._maybeRequestLivePositionStream("card_visibility");
      document.addEventListener("visibilitychange", this._visibilityHandler);
    }
    if (!this._intersectionObserver && "IntersectionObserver" in window) {
      this._visibleOnPage = false;
      this._intersectionObserver = new IntersectionObserver((entries) => {
        this._visibleOnPage = entries.some((entry) => entry.isIntersecting);
        this._maybeRequestLivePositionStream("card_intersection");
      });
      this._intersectionObserver.observe(this);
    } else if (!("IntersectionObserver" in window)) {
      this._visibleOnPage = true;
    }
  }

  _maybeRequestLivePositionStream(
    reason,
    force = false,
    durationSeconds = 0,
    surfaceErrors = false
  ) {
    if (!this.config || !this._hass || (!force && !this._cardIsVisible())) {
      return Promise.resolve(false);
    }
    if (!force && this._state(this.config.entity)?.state !== "mowing") {
      return Promise.resolve(false);
    }
    const now = Date.now();
    if (
      !force &&
      this._lastLiveStreamRequestAt &&
      now - this._lastLiveStreamRequestAt < LIVE_STREAM_KEEPALIVE_MS
    ) {
      return Promise.resolve(false);
    }
    this._lastLiveStreamRequestAt = now;
    const serviceData = {
      entity_id: this.config.entity,
      reason,
      force,
    };
    if (durationSeconds > 0) {
      serviceData.duration_seconds = durationSeconds;
    }
    return this._hass
      .callService("ecovacs_goat", "request_live_position_stream", serviceData)
      .catch((err) => {
        this._lastLiveStreamRequestAt = 0;
        if (surfaceErrors) {
          throw err;
        }
        return false;
      });
  }

  _cardIsVisible() {
    return (
      this.isConnected &&
      document.visibilityState !== "hidden" &&
      this._visibleOnPage !== false
    );
  }

  _showToast(message) {
    this.dispatchEvent(
      new CustomEvent("hass-notification", {
        detail: { message },
        bubbles: true,
        composed: true,
      })
    );
  }

  _metric(label, value) {
    return `
      <div class="metric">
        <div class="label">${this._escape(label)}</div>
        <div class="value">${value || "Unavailable"}</div>
      </div>
    `;
  }

  _errorLine(error) {
    if (!error || error.state in { unknown: true, unavailable: true }) {
      return "";
    }
    const description = error.attributes?.description;
    const value = this._formatState(error);
    if (error.state === "0" || error.state === "100") {
      return "";
    }
    return `
      <div class="error-line">
        <ha-icon icon="mdi:alert-circle"></ha-icon>
        <span>${value}${description ? ` - ${this._escape(description)}` : ""}</span>
      </div>
    `;
  }

  _mapPanel(mapState, mowerState) {
    const resolved = this._resolveMapData(mapState, mowerState);
    const render = this._computeMapRender(resolved, mowerState);
    this._lastMapDataSignature = this._mapDataSignature(resolved, mowerState);
    if (!render) {
      this._lastMapStructureSignature = null;
      return "";
    }
    if (render.empty) {
      this._lastMapStructureSignature = null;
      return `<div class="map"><div class="map-empty">${this._t("waiting_map")}</div></div>`;
    }
    this._lastMapStructureSignature = render.structureSignature;
    return `<div class="map">${this._buildMapSvgHtml(render)}</div>`;
  }

  // Merge each incoming map update into a persistent model, keeping the last
  // known value for any layer the update omits. The mower's map sensor pushes
  // sparse payloads (e.g. a position update without the garden outline), so
  // rebuilding purely from the latest payload would make layers flash in and
  // out. Treating the geometry as sticky keeps the map stable and robust.
  _resolveMapData(mapState, mowerState) {
    const model = this._mapModel || (this._mapModel = {});

    // Drop the previous run's lanes the moment a fresh mow
    // starts. "Fresh" means entering `mowing` from a non-mowing, non-paused
    // state, so resuming from a pause keeps the existing plan.
    const previousMowerState = this._mapMowerState;
    this._mapMowerState = mowerState;
    if (
      mowerState === "mowing" &&
      previousMowerState !== undefined &&
      previousMowerState !== "mowing" &&
      previousMowerState !== "paused"
    ) {
      delete model.position_history;
      delete model.pending;
      delete model.border;
    }

    const hasAttributes =
      !!mapState && mapState.state !== "unavailable" && !!mapState.attributes;

    if (hasAttributes) {
      const map = mapState.attributes;
      const liveInfo = map.info || {};

      const keep = (key, value) => {
        if (Array.isArray(value)) {
          if (value.length) {
            model[key] = value;
          }
        } else if (value !== undefined && value !== null) {
          model[key] = value;
        }
      };

      keep("outline", liveInfo.outline);
      keep("chainStep", liveInfo.chain_step);
      keep("obstacles", liveInfo.obstacles);
      keep("position_history", map.position_history);
      keep("pending", map.trace?.pending);
      keep("border", map.trace?.border);
      keep("borderMowing", map.border_mowing_enabled);
      keep("current_position", map.current_position);
      keep("charge_positions", map.charge_positions);
      keep("uwb_positions", map.uwb_positions);
      keep("rtk_station", map.rtk_station);
      keep("areas", map.areas);
      keep("no_go_zones", map.no_go_zones);
      keep("zones", map.zones);
    }

    const hasGeometry = Boolean(
      model.outline?.length ||
        model.obstacles?.length ||
        model.charge_positions?.length ||
        model.uwb_positions?.length ||
        model.areas?.length ||
        model.no_go_zones?.length ||
        model.zones?.length ||
        model.position_history?.length ||
        model.pending?.length ||
        model.border?.length ||
        model.current_position ||
        model.rtk_station
    );

    return {
      available: hasAttributes || hasGeometry,
      outline: model.outline,
      chainStep: model.chainStep,
      obstacles: model.obstacles,
      position_history: model.position_history,
      pending: model.pending,
      border: model.border,
      borderMowing: model.borderMowing,
      current_position: model.current_position,
      charge_positions: model.charge_positions,
      uwb_positions: model.uwb_positions,
      rtk_station: model.rtk_station,
      areas: model.areas,
      no_go_zones: model.no_go_zones,
      zones: model.zones,
    };
  }

  // Cheap fingerprint of the resolved map inputs. Used to skip rendering
  // entirely when an unrelated `set hass` update arrives. This runs on EVERY
  // hass update in HA, so it must stay O(1): array lengths plus the last
  // point stand in for full contents (the history only ever appends,
  // and a same-length change always moves the newest point).
  _mapDataSignature(resolved, mowerState) {
    if (!resolved || !resolved.available) {
      return "none";
    }
    const arraySig = (value) => {
      if (!Array.isArray(value) || !value.length) {
        return "0";
      }
      const last = value[value.length - 1];
      return `${value.length}:${last?.x ?? ""},${last?.y ?? ""}`;
    };
    // Segment CONTENT, not just the segment count: the composed border is
    // two segments (arc + tail) for the whole lap, so counting them alone
    // froze the drawn ring — it only shrank when an unrelated field (the
    // mower's position) happened to change the signature. Per-segment point
    // counts plus the arc's front catch every shortening cheaply.
    const segmentsSig = (value) => {
      if (!Array.isArray(value) || !value.length) {
        return "0";
      }
      const lengths = value.map((segment) => segment?.length ?? 0).join(",");
      const first = value[0];
      const front = first?.[first.length - 1];
      return `${lengths}:${front?.x ?? ""},${front?.y ?? ""}`;
    };
    return [
      mowerState,
      arraySig(resolved.outline),
      arraySig(resolved.obstacles),
      arraySig(resolved.position_history),
      segmentsSig(resolved.pending),
      segmentsSig(resolved.border),
      JSON.stringify(resolved.current_position ?? null),
      arraySig(resolved.charge_positions),
      arraySig(resolved.uwb_positions),
      JSON.stringify(resolved.rtk_station ?? null),
      arraySig(resolved.areas),
      arraySig(resolved.no_go_zones),
      arraySig(resolved.zones),
    ].join("|");
  }

  _computeMapRender(resolved, mowerState) {
    if (!resolved || !resolved.available) {
      return null;
    }

    const garden = this._positions(resolved.outline);
    const obstacles = this._polygons(resolved.obstacles);
    // Each pending lane is its own segment: joining them would draw lines
    // across whatever lies between two lanes (a terrace, the house).
    const pending = (resolved.pending || [])
      .map((segment) => this._positions(segment))
      .filter((segment) => segment.length > 1);
    // The edge lap has its own field: once mostly driven it shrinks to a
    // straight run, so it cannot be told from a mowing lane by shape.
    const pendingLanes = pending;
    const pendingBorder = (resolved.border || [])
      .map((segment) => this._positions(segment))
      .filter((segment) => segment.length > 1);
    // Border states: null = no plan snapshot yet, so during a job the whole
    // lap is still ahead of the mower; [] = the lap is done; a list = this
    // much remains. Off the job nothing is pending, whatever the field says.
    const jobRunning = mowerState === "mowing" || mowerState === "paused";
    const borderUnknown = resolved.border == null;
    // With the mower's "border switch" off, no edge lap is coming at all, so
    // an unknown border must not paint the ring green. Missing information
    // counts as enabled — that is the mower's factory default.
    const borderMowingEnabled = resolved.borderMowing !== false;
    // Not drawn any more (the app shows no trail either), but the recent
    // positions still tell us which way the mower is pointing when its own
    // heading is missing.
    const recentPositions = this._positions(resolved.position_history);
    const charge = this._positions(resolved.charge_positions);
    const rawCurrent = this._position(resolved.current_position);
    const current = this._displayMowerPosition({
      mowerState,
      rawCurrent,
      charge,
    });
    const beacons = this._positions(resolved.uwb_positions);
    const rtkStation = this._position(resolved.rtk_station);
    const noGoZones = this._polygons(resolved.no_go_zones);

    const points = [
      ...garden,
      ...obstacles.flat(),
      ...pending.flat(),
      ...recentPositions,
      ...(current ? [current] : []),
      ...charge,
      ...beacons,
      ...(rtkStation ? [rtkStation] : []),
      ...noGoZones.flat(),
    ];
    if (!points.length) {
      return { empty: true };
    }

    const width = 360;
    const height = 300;
    const bounds = this._mapBounds(points);

    // Static layers: only depend on the persistent geometry + viewport. When
    // these are unchanged we can patch the dynamic layers in place.
    // The lawn: outline filled, obstacles punched out as holes by evenodd —
    // the same picture the app draws.
    const gardenPath = this._closedPath(garden, bounds, width, height);
    const obstaclePaths = obstacles
      .map((obstacle) => this._closedPath(obstacle, bounds, width, height))
      .filter(Boolean);
    const lawnClipId = `egc-lawn-clip-${this._instanceId}`;
    const gardenMarkup = gardenPath
      ? `<clipPath id="${lawnClipId}"><path clip-rule="evenodd" d="${
          gardenPath + obstaclePaths.join("")
        }"></path></clipPath><path class="map-lawn" fill-rule="evenodd" d="${
          gardenPath + obstaclePaths.join("")
        }"></path>`
      : "";
    // The border is drawn above the mowed lanes, like the app: lanes are
    // clipped at the boundary, so without this the dark line all but vanishes
    // once the edge pass paints over it.
    // The boundary's colour tells the edge-lap story: white is "edged", green
    // is "still to edge". At rest it is white (the lap was done last job).
    // While a job runs and no snapshot has told us otherwise, the whole lap is
    // ahead of the mower, so the whole ring is green; once snapshots flow,
    // only the remaining part stays green and it shrinks behind the mower.
    const wholeLapAhead = jobRunning && borderUnknown && borderMowingEnabled;
    const lawnBorderMarkup = gardenPath
      ? `<path class="map-lawn-outline${
          wholeLapAhead ? "" : " map-lawn-outline-done"
        }" d="${gardenPath}"></path>`
      : "";
    const laneClip = gardenPath ? ` clip-path="url(#${lawnClipId})"` : "";
    const obstacleMarkup = obstaclePaths
      .map((path) => `<path class="map-obstacle-edge" d="${path}"></path>`)
      .join("");
    const stationMarkup = charge
      .map((position) => {
        const point = this._project(position, bounds, width, height);
        return `<path class="map-station" d="${this._housePath(point.x, point.y - 10)}"></path>`;
      })
      .join("");
    const noGoMarkup = noGoZones
      .map((zone) => this._closedPath(zone, bounds, width, height))
      .filter(Boolean)
      .map((path) => `<path class="map-nogo" d="${path}"></path>`)
      .join("");
    const beaconMarkup = beacons
      .map((position) => {
        const point = this._project(position, bounds, width, height);
        return `<circle class="map-beacon" cx="${point.x}" cy="${point.y}" r="6"></circle>`;
      })
      .join("");
    const rtkMarkup = rtkStation
      ? (() => {
          const point = this._project(rtkStation, bounds, width, height);
          return `<path class="map-rtk-station" d="M ${point.x} ${point.y - 8} L ${
            point.x + 7
          } ${point.y + 5} L ${point.x - 7} ${point.y + 5} Z"></path>`;
        })()
      : "";

    // Dynamic layers: change on (almost) every position update.
    const segmentsToPath = (segments) =>
      segments
        .map((segment) =>
          segment
            .map((position, index) => {
              const point = this._project(position, bounds, width, height);
              return `${index ? "L" : "M"} ${point.x} ${point.y}`;
            })
            .join(" ")
        )
        .join(" ");
    const pendingPath = segmentsToPath(pendingLanes);
    // Off the job nothing is pending: the composed remainder (arc + template
    // tail) is never empty, so without this gate the ring stayed dark green
    // while the mower sat docked. At rest the boundary is plain white, like
    // the app.
    const borderPath = segmentsToPath(
      jobRunning
        ? this._snapBorderToOutline(
            garden,
            pendingBorder,
            resolved.chainStep,
            current
          )
        : []
    );
    const currentPoint = current ? this._project(current, bounds, width, height) : null;
    const currentHeading = current
      ? this._mowerHeading(current, charge, recentPositions, mowerState)
      : 0;
    const markerAnimation = this._mowerMarkerAnimation(
      current,
      currentPoint,
      bounds,
      width,
      height
    );
    const mowerMarkup = currentPoint
      ? `<g class="map-mower-group" transform="translate(${currentPoint.x} ${currentPoint.y})">${markerAnimation}<g class="map-mower-rotate" transform="rotate(${currentHeading})"><rect class="map-mower-body" x="-9" y="-7" width="18" height="14" rx="5"></rect><circle class="map-mower-eye" cx="2" cy="0" r="2.6"></circle></g></g>`
      : "";

    const structureSignature = JSON.stringify([
      width,
      height,
      gardenMarkup,
      lawnBorderMarkup,
      obstacleMarkup,
      stationMarkup,
      noGoMarkup,
      beaconMarkup,
      rtkMarkup,
    ]);

    return {
      width,
      height,
      gardenMarkup,
      lawnBorderMarkup,
      obstacleMarkup,
      stationMarkup,
      noGoMarkup,
      beaconMarkup,
      rtkMarkup,
      pendingPath,
      borderPath,
      laneClip,
      currentPoint,
      currentHeading,
      markerAnimation,
      mowerMarkup,
      structureSignature,
    };
  }

  _buildMapSvgHtml(render) {
    return `
      <svg viewBox="0 0 ${render.width} ${render.height}" role="img" aria-label="Live mower map">
        ${render.gardenMarkup}
        ${render.obstacleMarkup}
        <path class="map-pending" data-layer="pending"${render.laneClip}${render.pendingPath ? ` d="${render.pendingPath}"` : ""}></path>
        ${render.lawnBorderMarkup}
        <path class="map-border-pending" data-layer="border"${render.borderPath ? ` d="${render.borderPath}"` : ""}></path>
        ${render.stationMarkup}
        ${render.noGoMarkup}
        ${render.beaconMarkup}
        ${render.rtkMarkup}
        ${render.mowerMarkup}
      </svg>
    `;
  }

  _patchMapSvg(svg, render) {
    const group = svg.querySelector(".map-mower-group");
    // The mower marker appeared/disappeared since the last render: fall back to
    // a full rebuild so the element ordering stays correct.
    if (render.currentPoint ? !group : group) {
      return false;
    }

    for (const [layer, path] of [
      ["pending", render.pendingPath],
      ["border", render.borderPath],
    ]) {
      const element = svg.querySelector(`[data-layer="${layer}"]`);
      if (!element) {
        continue;
      }
      if (path) {
        element.setAttribute("d", path);
      } else {
        element.removeAttribute("d");
      }
    }

    if (render.currentPoint && group) {
      group.setAttribute(
        "transform",
        `translate(${render.currentPoint.x} ${render.currentPoint.y})`
      );
      group.innerHTML = `${render.markerAnimation}<g class="map-mower-rotate" transform="rotate(${render.currentHeading})"><rect class="map-mower-body" x="-9" y="-7" width="18" height="14" rx="5"></rect><circle class="map-mower-eye" cx="2" cy="0" r="2.6"></circle></g>`;
    }

    return true;
  }

  _displayMowerPosition({ mowerState, rawCurrent, charge }) {
    if (mowerState === "docked" && charge.length) {
      return rawCurrent || charge[0];
    }
    return rawCurrent;
  }

  _lastPathPosition(path) {
    return path.length ? path[path.length - 1] : null;
  }

  _mapBounds(points) {
    // A loop instead of Math.min(...xs): the point set can grow to tens of
    // thousands of points, and spreading that many arguments overflows the
    // JS engine's argument limit (RangeError).
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const point of points) {
      if (point.x < minX) minX = point.x;
      if (point.x > maxX) maxX = point.x;
      if (point.y < minY) minY = point.y;
      if (point.y > maxY) maxY = point.y;
    }

    if (minX === maxX) {
      minX -= 1000;
      maxX += 1000;
    }
    if (minY === maxY) {
      minY -= 1000;
      maxY += 1000;
    }

    return { minX, maxX, minY, maxY };
  }

  _path(points, bounds, width, height) {
    if (points.length < 2) {
      return "";
    }
    return points
      .map((position, index) => {
        const point = this._project(position, bounds, width, height);
        return `${index === 0 ? "M" : "L"} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`;
      })
      .join(" ");
  }

  _closedPath(points, bounds, width, height) {
    const path = this._path(points, bounds, width, height);
    return path ? `${path} Z` : "";
  }


  _housePath(x, y) {
    const size = 10;
    return [
      `M ${x.toFixed(1)} ${(y - size).toFixed(1)}`,
      `L ${(x + size).toFixed(1)} ${(y - 1).toFixed(1)}`,
      `L ${(x + size * 0.7).toFixed(1)} ${(y - 1).toFixed(1)}`,
      `L ${(x + size * 0.7).toFixed(1)} ${(y + size).toFixed(1)}`,
      `L ${(x - size * 0.7).toFixed(1)} ${(y + size).toFixed(1)}`,
      `L ${(x - size * 0.7).toFixed(1)} ${(y - 1).toFixed(1)}`,
      `L ${(x - size).toFixed(1)} ${(y - 1).toFixed(1)}`,
      "Z",
    ].join(" ");
  }

  _mowerHeading(current, charge, path, mowerState) {
    if (mowerState === "docked" && charge.length) {
      const heading = this._headingToTarget(current, charge[0]);
      if (heading !== null) {
        return heading;
      }
      return Number(charge[0].a ?? current.a ?? 0) + 180;
    }
    return this._heading(current, path);
  }

  _headingToTarget(current, target) {
    const dx = target.x - current.x;
    const dy = -(target.y - current.y);
    if (Math.hypot(dx, dy) < 1) {
      return null;
    }
    return (Math.atan2(dy, dx) * 180) / Math.PI;
  }

  _heading(current, path) {
    const previous = this._previousPosition(current, path);
    if (previous) {
      const dx = current.x - previous.x;
      const dy = -(current.y - previous.y);
      if (Math.hypot(dx, dy) >= 1) {
        return (Math.atan2(dy, dx) * 180) / Math.PI;
      }
    }
    return this._mowerHeadingToSvg(current) ?? 0;
  }

  _mowerHeadingToSvg(position) {
    const heading = Number(position?.a);
    if (!Number.isFinite(heading)) {
      return null;
    }
    return heading - 90;
  }

  _previousPosition(current, path) {
    for (let index = path.length - 1; index >= 0; index -= 1) {
      const candidate = path[index];
      if (Math.hypot(current.x - candidate.x, current.y - candidate.y) > 20) {
        return candidate;
      }
    }
    if (path.length >= 2) {
      return path[path.length - 2];
    }
    return null;
  }

  _project(position, bounds, width, height) {
    const padding = 4;
    const spanX = bounds.maxX - bounds.minX;
    const spanY = bounds.maxY - bounds.minY;
    const scale = Math.min(
      (width - padding * 2) / spanX,
      (height - padding * 2) / spanY
    );
    const drawnWidth = spanX * scale;
    const drawnHeight = spanY * scale;
    const offsetX = (width - drawnWidth) / 2;
    const offsetY = (height - drawnHeight) / 2;

    return {
      x: offsetX + (position.x - bounds.minX) * scale,
      y: height - (offsetY + (position.y - bounds.minY) * scale),
    };
  }

  _mowerMarkerAnimation(current, currentPoint, bounds, width, height) {
    if (!current || !currentPoint) {
      this._lastMowerPosition = null;
      this._lastMowerUpdateAt = null;
      return "";
    }

    const previousPosition = this._lastMowerPosition;
    const previousUpdateAt = this._lastMowerUpdateAt;
    const updateAt = Date.now();
    this._lastMowerPosition = current;
    this._lastMowerUpdateAt = updateAt;

    if (!previousPosition) {
      return "";
    }

    const previousPoint = this._project(previousPosition, bounds, width, height);
    const distance = Math.hypot(
      currentPoint.x - previousPoint.x,
      currentPoint.y - previousPoint.y
    );
    if (distance < 1) {
      return "";
    }

    const elapsedMs = previousUpdateAt ? updateAt - previousUpdateAt : 2500;
    const durationSeconds = Math.min(8, Math.max(1.2, elapsedMs / 1000 + 0.3));

    return `
      <animateTransform
        attributeName="transform"
        type="translate"
        from="${previousPoint.x} ${previousPoint.y}"
        to="${currentPoint.x} ${currentPoint.y}"
        dur="${durationSeconds.toFixed(1)}s"
        fill="freeze"
      ></animateTransform>
    `;
  }

  // The lap chain and the lawn outline come from two different mower data
  // sources and disagree by a cell or two, drifting further from the chain's
  // top-left anchor (visibly off along the right edge). The app never draws
  // the chain — it recolours the lawn boundary by progress — so do the same:
  // keep the outline runs that lie near any remaining border point, drop the
  // rest. The chain only decides WHICH parts of the boundary are pending.
  _snapBorderToOutline(outline, segments, step, mowerPosition) {
    // The integration is now authoritative about what remains of the edge
    // lap: it erodes the border with the mower's own just-cut-cells stream
    // and ratchets every removed cell (trail measured 0.5 m behind the live
    // marker). So this draws the data as-is. The old outline-walk
    // reconstruction is gone — it rebuilt one continuous run from lap start
    // to data front and resurrected every erosion hole in between (observed
    // live 2026-09-02: green stretch riding just behind the mower).
    if (!segments.length) {
      return segments;
    }
    const cell = Number(step) > 0 ? Number(step) : 50;
    const first = segments[0];
    if (
      segments.length === 1 &&
      first.length > 2 &&
      Array.isArray(outline) &&
      outline.length >= 3
    ) {
      const head = first[0];
      const tail = first[first.length - 1];
      if (Math.abs(head.x - tail.x) + Math.abs(head.y - tail.y) <= 2 * cell) {
        // The closed announcement: the whole lap is ahead — draw it as the
        // outline ring so it hugs the lawn boundary exactly.
        return [outline.concat([outline[0]])];
      }
    }
    // Collapsed straight edges span metres, so freshness shaving needs the
    // intermediate points back.
    const densify = (segment) => {
      if (segment.length < 2) {
        return segment.slice();
      }
      const dense = [segment[0]];
      for (let i = 1; i < segment.length; i += 1) {
        const a = segment[i - 1];
        const b = segment[i];
        const span = Math.max(Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        const pieces = Math.max(1, Math.ceil(span / cell));
        for (let k = 1; k <= pieces; k += 1) {
          dense.push({
            x: a.x + ((b.x - a.x) * k) / pieces,
            y: a.y + ((b.y - a.y) * k) / pieces,
          });
        }
      }
      return dense;
    };
    const polylineLength = (points) => {
      let total = 0;
      for (let i = 1; i < points.length; i += 1) {
        total += Math.hypot(
          points[i].x - points[i - 1].x,
          points[i].y - points[i - 1].y
        );
      }
      return total;
    };
    // The data trails the mower by one update (~0.5–1 m). Shave segment ENDS
    // that sit within reach of the live marker so the white edge rides the
    // once-a-second position. Ends only, never the middle: a mower merely
    // driving past an intact stretch (lane phase, transit) must not punch
    // holes the data does not confirm.
    const nearLimit = (cell * 16) ** 2;
    const nearMower = (point) =>
      mowerPosition &&
      (point.x - mowerPosition.x) ** 2 + (point.y - mowerPosition.y) ** 2 <=
        nearLimit;
    const result = [];
    for (const segment of segments) {
      let dense = densify(segment);
      // Erosion leaves the odd 1–2 cell sliver between holes — off-line
      // jitter the cut corridor missed. Real remainders are longer.
      if (dense.length < 2 || polylineLength(dense) < 4 * cell) {
        continue;
      }
      let lo = 0;
      let hi = dense.length - 1;
      while (lo < hi && nearMower(dense[lo])) {
        lo += 1;
      }
      while (hi > lo && nearMower(dense[hi])) {
        hi -= 1;
      }
      const kept = dense.slice(lo, hi + 1);
      if (kept.length >= 2 && polylineLength(kept) >= 2 * cell) {
        result.push(kept);
      }
    }
    return result;
  }

  _positions(value) {
    return Array.isArray(value)
      ? value.map((position) => this._position(position)).filter(Boolean)
      : [];
  }

  _polygons(value) {
    return Array.isArray(value)
      ? value.map((polygon) => this._positions(polygon)).filter((polygon) => polygon.length)
      : [];
  }

  _position(value) {
    if (!value || value.x === undefined || value.y === undefined) {
      return null;
    }
    const x = Number(value.x);
    const y = Number(value.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      return null;
    }
    // Positions flagged invalid (LiDAR/RTK relocalisation glitches) draw as
    // spikes teleporting across the map; drop them entirely.
    if (Number(value.invalid)) {
      return null;
    }
    return {
      ...value,
      x,
      y,
      a: value.a === undefined || value.a === null ? null : Number(value.a),
    };
  }

  _t(key) {
    const language = (
      this._hass?.locale?.language ||
      this._hass?.language ||
      "en"
    ).split("-")[0];
    const table = I18N[language] || I18N.en;
    return table[key] || I18N.en[key] || key;
  }

  _formatState(stateObj) {
    if (!stateObj) {
      return "Unavailable";
    }
    if (stateObj.state === "unknown" || stateObj.state === "unavailable") {
      return this._label(stateObj.state);
    }
    if (this._hass?.formatEntityState) {
      return this._escape(this._hass.formatEntityState(stateObj));
    }
    const unit = stateObj.attributes?.unit_of_measurement;
    return `${this._escape(stateObj.state)}${unit ? ` ${this._escape(unit)}` : ""}`;
  }

  _formatAreaState(stateObj) {
    if (!stateObj) {
      return "Unavailable";
    }
    if (stateObj.state === "unknown" || stateObj.state === "unavailable") {
      return this._label(stateObj.state);
    }

    const targetUnit = this._areaDisplayUnit();
    if (targetUnit !== "ft²") {
      const value = Number(stateObj.state);
      if (!Number.isFinite(value)) {
        return this._formatState(stateObj);
      }
      const unit = stateObj.attributes?.unit_of_measurement;
      return `${this._formatFixed(value, 0)}${unit ? ` ${this._escape(unit)}` : ""}`;
    }

    const value = Number(stateObj.state);
    const sourceUnit = stateObj.attributes?.unit_of_measurement;
    if (!Number.isFinite(value)) {
      return this._formatState(stateObj);
    }

    let squareFeet;
    if (sourceUnit === "ft²") {
      squareFeet = value;
    } else if (sourceUnit === "m²") {
      squareFeet = value * 10.76391041671;
    } else if (sourceUnit === "cm²") {
      squareFeet = value / 929.0304;
    } else if (sourceUnit === "in²") {
      squareFeet = value / 144;
    } else {
      return this._formatState(stateObj);
    }

    return `${this._formatFixed(squareFeet, 0)} ft²`;
  }

  _formatRoundedState(stateObj) {
    if (!stateObj) {
      return "Unavailable";
    }
    if (stateObj.state === "unknown" || stateObj.state === "unavailable") {
      return this._label(stateObj.state);
    }
    const value = Number(stateObj.state);
    if (!Number.isFinite(value)) {
      return this._formatState(stateObj);
    }
    const unit = stateObj.attributes?.unit_of_measurement;
    return `${Math.round(value)}${unit ? ` ${this._escape(unit)}` : ""}`;
  }

  _areaDisplayUnit() {
    return (
      this._hass?.config?.unit_system?.area ||
      this._hass?.config?.unit_system?.area_unit ||
      (this._hass?.locale?.unit_system === "us_customary" ? "ft²" : undefined)
    );
  }

  _formatFixed(value, digits) {
    const language = this._hass?.locale?.language || navigator.language;
    return new Intl.NumberFormat(language, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }

  _friendlyName(stateObj) {
    return stateObj?.attributes?.friendly_name;
  }

  _label(value) {
    return String(value || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }

  _escape(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

}

// Guard against double-registration: the integration now auto-loads this card,
// so an existing manual Lovelace resource pointing at the old path would
// otherwise try to define the element a second time and throw.
if (!customElements.get("ecovacs-goat-card")) {
  customElements.define("ecovacs-goat-card", EcovacsGoatCard);

  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "ecovacs-goat-card",
    name: "Ecovacs GOAT Card",
    description: "Control an ECOVACS GOAT mower: live map, start/pause, dock, and edge trimming.",
  });
}
