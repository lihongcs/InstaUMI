const elements = {
  sessionName: document.querySelector("#session-name"),
  captureSettingsForm: document.querySelector("#capture-settings-form"),
  taskNameInput: document.querySelector("#task-name-input"),
  outputRootInput: document.querySelector("#output-root-input"),
  saveCaptureSettings: document.querySelector("#save-capture-settings"),
  expectedSessionDir: document.querySelector("#expected-session-dir"),
  newCaptureTask: document.querySelector("#new-capture-task"),
  clearTimestamps: document.querySelector("#clear-timestamps"),
  totalState: document.querySelector("#total-state"),
  elapsed: document.querySelector("#elapsed"),
  totalButton: document.querySelector("#total-button"),
  totalEndButton: document.querySelector("#total-end-button"),
  segmentButton: document.querySelector("#segment-button"),
  episodeCount: document.querySelector("#episode-count"),
  currentSegment: document.querySelector("#current-segment"),
  message: document.querySelector("#message-bar"),
  logDetail: document.querySelector("#log-detail"),
  eventsBody: document.querySelector("#events-body"),
  modeSlam: document.querySelector("#mode-slam"),
  modeSegment: document.querySelector("#mode-segment"),
  pedalModeField: document.querySelector("#pedal-mode-field"),
  slamCalibrationBlock: document.querySelector("#slam-calibration-block"),
  slamMarks: document.querySelector("#slam-marks"),
  effectiveDuration: document.querySelector("#effective-duration"),
  instaStatus: document.querySelector("#insta-status"),
  trackerStatus: document.querySelector("#tracker-status"),
  pedalStatus: document.querySelector("#pedal-status"),
  instaDetail: document.querySelector("#insta-detail"),
  trackerDetail: document.querySelector("#tracker-detail"),
  pedalDetail: document.querySelector("#pedal-detail"),
  pedalCardState: document.querySelector("#pedal-card-state"),
  pedalLiveIndicator: document.querySelector("#pedal-live-indicator"),
  pedalLiveText: document.querySelector("#pedal-live-text"),
  pedalLiveProgress: document.querySelector("#pedal-live-progress"),
  scanInsta: document.querySelector("#scan-insta"),
  connectInsta: document.querySelector("#connect-insta"),
  cameraTransportLabel: document.querySelector("#camera-transport-label"),
  cameraTitle: document.querySelector("#camera-title"),
  cameraLogTitle: document.querySelector("#camera-log-title"),
  mvsLiveRow: document.querySelector("#mvs-live-row"),
  usbCameraPreviewPanel: document.querySelector("#usb-camera-preview-panel"),
  cameraPreviewGrid: document.querySelector("#camera-preview-grid"),
  cameraPreviewToolbar: document.querySelector("#camera-preview-toolbar"),
  cameraViewPanorama: document.querySelector("#camera-view-panorama"),
  cameraViewFlat: document.querySelector("#camera-view-flat"),
  cameraViewModeNote: document.querySelector("#camera-view-mode-note"),
  cameraClockProtocol: document.querySelector("#camera-clock-protocol"),
  releaseOtherHtc: document.querySelector("#release-other-htc"),
  recoverHtc: document.querySelector("#recover-htc"),
  armDual: document.querySelector("#arm-dual"),
  armSingle: document.querySelector("#arm-single"),
  armModeHint: document.querySelector("#arm-mode-hint"),
  cameraTransportBluetooth: document.querySelector("#camera-transport-bluetooth"),
  cameraTransportWired: document.querySelector("#camera-transport-wired"),
  cameraTransportMvs: document.querySelector("#camera-transport-mvs"),
  cameraTransportHint: document.querySelector("#camera-transport-hint"),
  bleLog: document.querySelector("#ble-log"),
  cameraCard: document.querySelector("#camera-card"),
  trackerCard: document.querySelector("#tracker-card"),
  pedalCard: document.querySelector("#pedal-card"),
  cameraCount: document.querySelector("#camera-count"),
  cameraCountLabel: document.querySelector("#camera-count-label"),
  trackerCount: document.querySelector("#tracker-count"),
  trackerCountLabel: document.querySelector("#tracker-count-label"),
  cameraDeviceList: document.querySelector("#camera-device-list"),
  trackerDeviceList: document.querySelector("#tracker-device-list"),
  trajectoryPanel: document.querySelector("#trajectory-panel"),
  trajectoryPanelAnchor: document.querySelector("#trajectory-panel-anchor"),
  trajectoryCanvas: document.querySelector("#tracker-canvas"),
  trajectoryEmpty: document.querySelector("#trajectory-empty"),
  trajectoryLegend: document.querySelector("#trajectory-legend"),
  trajectoryScale: document.querySelector("#trajectory-scale"),
  trajectoryLabels: document.querySelector("#trajectory-labels"),
  undoTimestamp: document.querySelector("#undo-timestamp"),
  resetView: document.querySelector("#reset-view"),
  lockView: document.querySelector("#lock-view"),
  clearTrajectory: document.querySelector("#clear-trajectory"),
  quitApp: document.querySelector("#quit-app"),
  annotationOpenForm: document.querySelector("#annotation-open-form"),
  annotationEditor: document.querySelector("#annotation-editor"),
  annotationCount: document.querySelector("#annotation-count"),
  annotationDetail: document.querySelector("#annotation-detail"),
  offlineAnnotationRoot: document.querySelector("#offline-annotation-root"),
  openOfflineAnnotation: document.querySelector("#open-offline-annotation"),
  closeOfflineAnnotation: document.querySelector("#close-offline-annotation"),
  annotationPreviousEpisode: document.querySelector("#annotation-previous-episode"),
  annotationCompleteEpisode: document.querySelector("#annotation-complete-episode"),
  annotationEpisodeLabel: document.querySelector("#annotation-episode-label"),
  annotationOfflineProgress: document.querySelector("#annotation-offline-progress"),
  annotationFrameDetail: document.querySelector("#annotation-frame-detail"),
  annotationWorkspaceName: document.querySelector("#annotation-workspace-name"),
  annotationWorkspacePath: document.querySelector("#annotation-workspace-path"),
  annotationLoaderHint: document.querySelector("#annotation-loader-hint"),
  gripperPreviewForm: document.querySelector("#gripper-preview-form"),
  gripperPreviewRoot: document.querySelector("#gripper-preview-root"),
  gripperPreviewCapture: document.querySelector("#gripper-preview-capture"),
  gripperPreviewEpisode: document.querySelector("#gripper-preview-episode"),
  gripperPreviewStatus: document.querySelector("#gripper-preview-status"),
  gripperPreviewEditor: document.querySelector("#gripper-preview-editor"),
  gripperPreviewDatasetName: document.querySelector("#gripper-preview-dataset-name"),
  gripperPreviewDatasetPath: document.querySelector("#gripper-preview-dataset-path"),
  gripperPreviewMeta: document.querySelector("#gripper-preview-meta"),
  gripperPreviewPrevious: document.querySelector("#gripper-preview-previous"),
  gripperPreviewNext: document.querySelector("#gripper-preview-next"),
  gripperPreviewEpisodeLabel: document.querySelector("#gripper-preview-episode-label"),
  gripperPreviewSource: document.querySelector("#gripper-preview-source"),
  gripperThresholdRange: document.querySelector("#gripper-threshold-range"),
  gripperThresholdMm: document.querySelector("#gripper-threshold-mm"),
  gripperThresholdLabel: document.querySelector("#gripper-threshold-label"),
  gripperThresholdCommand: document.querySelector("#gripper-threshold-command"),
  gripperPreviewWarning: document.querySelector("#gripper-preview-warning"),
  gripperPreviewLegend: document.querySelector("#gripper-preview-legend"),
  gripperContinuousCanvas: document.querySelector("#gripper-continuous-canvas"),
  gripperBinaryCanvas: document.querySelector("#gripper-binary-canvas"),
  gripperPreviewStats: document.querySelector("#gripper-preview-stats"),
};

const cameraPreviewViews = Object.fromEntries(["right", "left"].map((hand) => [
  `insta_${hand}`,
  {
    hand,
    role: `insta_${hand}`,
    card: document.querySelector(`#camera-preview-card-${hand}`),
    stage: document.querySelector(`#camera-preview-stage-${hand}`),
    image: document.querySelector(`#camera-preview-image-${hand}`),
    empty: document.querySelector(`#camera-preview-empty-${hand}`),
    state: document.querySelector(`#camera-preview-state-${hand}`),
    detail: document.querySelector(`#camera-preview-detail-${hand}`),
  },
]));

const annotationViews = Object.fromEntries(["left", "right"].map((hand) => [
  `insta_${hand}`,
  {
    hand,
    stage: document.querySelector(`#annotation-stage-${hand}`),
    image: document.querySelector(`#annotation-image-${hand}`),
    canvas: document.querySelector(`#annotation-canvas-${hand}`),
    empty: document.querySelector(`#annotation-empty-${hand}`),
    count: document.querySelector(`#annotation-count-${hand}`),
    detail: document.querySelector(`#annotation-detail-${hand}`),
    frame: document.querySelector(`#annotation-frame-${hand}`),
    clear: document.querySelector(`#clear-target-annotations-${hand}`),
  },
]));

let requestBusy = false;
let statusRefreshBusy = false;
let trackerRefreshBusy = false;
let pedalRefreshBusy = false;
let lastRuntimeStatus = {};
let totalStartViewportLock = null;
let annotationLoaderNotice = null;
let gripperPreviewPayload = null;
let gripperPreviewBusy = false;
let gripperPreviewNotice = null;
let gripperPreviewDrawFrame = null;
let gripperPreviewEstimateBusy = false;
let gripperPreviewEstimateJobId = null;
let gripperPreviewEstimateCaptureRoot = null;
let gripperPreviewEstimateEpisode = null;
let gripperPreviewEstimateCacheHit = false;
let gripperPreviewEstimatePollTimer = null;
let gripperPreviewEstimatePollGeneration = 0;
let gripperPreviewEstimatePollFailures = 0;
let gripperPreviewDiscoveryBusy = false;
let gripperPreviewDiscovery = null;
let gripperPreviewDiscoveryTimer = null;
let gripperPreviewDiscoveryGeneration = 0;
let gripperPreviewDiscoveryInputRoot = "";
let gripperPreviewAutoSelectionKey = "";
let gripperThresholdInitialized = false;

const TRACK_COLORS = ["#16a26a", "#087af5", "#7a5af8", "#ff8a00", "#db3b69"];
const TRACKER_3D_VIEW_STORAGE_KEY = "instaumi.tracker3d.view.v1";
const GRIPPER_PREVIEW_PATH_STORAGE_KEY = "instadatacap.gripperPreview.path.v1";
const GRIPPER_PREVIEW_THRESHOLD_STORAGE_KEY = "instadatacap.gripperPreview.thresholdMm.v2";
const GRIPPER_PREVIEW_CAPTURE_STORAGE_KEY = "instadatacap.gripperPreview.capture.v1";
const GRIPPER_PREVIEW_EPISODE_STORAGE_KEY = "instadatacap.gripperPreview.episode.v1";
const GRIPPER_PREVIEW_ESTIMATE_POLL_MS = 1000;
const GRIPPER_PREVIEW_ESTIMATE_MAX_POLL_FAILURES = 5;

async function fetchJson(url, options = {}, timeoutMs = 2000) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.message || `HTTP ${response.status}`);
    if (!payload) throw new Error("The service returned an unrecognized response");
    return payload;
  } finally {
    window.clearTimeout(timeout);
  }
}

function suppressPedalSpaceDefault(event) {
  if (event.code !== "Space" && event.key !== " ") return;
  const target = event.target;
  const acceptsText = target instanceof Element
    && Boolean(target.closest('input[type="text"], input:not([type]), textarea, [contenteditable="true"]'));
  if (!acceptsText) {
    // The USB pedal also presents itself as a keyboard Space key. The Linux
    // evdev reader handles capture control; the browser must not scroll or
    // activate a focused button for the same physical press.
    event.preventDefault();
  }
}

document.addEventListener("keydown", suppressPedalSpaceDefault, { capture: true });
document.addEventListener("keyup", suppressPedalSpaceDefault, { capture: true });

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const hours = String(Math.floor(total / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const secs = String(total % 60).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
}

function formatPreciseDuration(seconds) {
  const totalMs = Math.max(0, Math.round(Number(seconds || 0) * 1000));
  const hours = String(Math.floor(totalMs / 3_600_000)).padStart(2, "0");
  const minutes = String(Math.floor((totalMs % 3_600_000) / 60_000)).padStart(2, "0");
  const secs = String(Math.floor((totalMs % 60_000) / 1000)).padStart(2, "0");
  const millis = String(totalMs % 1000).padStart(3, "0");
  return `${hours}:${minutes}:${secs}.${millis}`;
}

function setSignal(element, text, state) {
  const label = element.querySelector("span");
  if (label) label.textContent = text;
  element.className = `signal-label state-${state}`;
}

function setCardState(element, state) {
  element.classList.remove("state-ready", "state-connected", "state-lost", "state-offline", "state-working");
  element.classList.add(`state-${state}`);
}

function adapterStatus(status, name) {
  return (status.adapters || {})[name] || {};
}

function trackerState(item) {
  const age = Number(item.age_ms ?? 999999);
  if (age > 1500) return { key: "lost", label: "No recent pose" };
  if (Number(item.tracking_status) === 2) return { key: "ready", label: "Ready" };
  return { key: "lost", label: "Tracking lost" };
}

function displayRole(item, index) {
  const role = String(item.role || "").toLowerCase();
  if (role.includes("left")) return "HTC Left";
  if (role.includes("right")) return "HTC Right";
  return item.role || "HTC Unassigned";
}

function armSide(item, index = 0) {
  const role = String(item.role || "").toLowerCase();
  if (role.includes("right")) return "right";
  if (role.includes("left")) return "left";
  return index === 0 ? "right" : index === 1 ? "left" : "unknown";
}

function eventDisplayTitle(event) {
  const type = String(event?.type || "");
  if (type === "total_start") return "Capture started";
  if (type === "total_end") return "Capture ended";
  if (type === "total_end_failed") return "Capture end failed";
  if (type === "slam_timestamp") {
    const match = String(event?.title || "").match(/(\d+)$/);
    return `SLAM marker ${match ? match[1] : ""}`.trim();
  }
  if (type === "segment_start") return "Episode started";
  if (type === "segment_end") return "Episode ended";
  return event?.title || type || "Unknown event";
}

function armColor(side, fallbackIndex = 0) {
  if (side === "right") return "#16a26a";
  if (side === "left") return "#087af5";
  return TRACK_COLORS[fallbackIndex % TRACK_COLORS.length];
}

function renderSlamMarks(events, canDelete) {
  const slots = [0, 1].map((index) => {
    const event = events[index];
    if (!event) {
      return `<div class="slam-mark empty"><span>Marker ${index + 1}</span><time>Waiting</time></div>`;
    }
    const actualTime = new Date(event.unix_time_ms).toLocaleTimeString("en-GB", {
      hour12: false,
      fractionalSecondDigits: 3,
    });
    const deleteButton = canDelete
      ? `<button type="button" class="slam-mark-delete" data-delete-slam="${escapeHtml(event.id)}" aria-label="Delete marker ${index + 1}" title="Delete this marker">×</button>`
      : "";
    return `<div class="slam-mark done"><span>Marker ${index + 1}</span><time>${actualTime}</time>${deleteButton}</div>`;
  });
  elements.slamMarks.innerHTML = slots.join("");
}

function shortAddress(value) {
  const text = String(value || "");
  if (text.length <= 11) return text || "Unknown ID";
  return `${text.slice(0, 8)}…${text.slice(-5)}`;
}

function render(status) {
  const isTotalStartUpdate = Boolean(status.starting)
    || (!lastRuntimeStatus.running && Boolean(status.running));
  if (isTotalStartUpdate && !totalStartViewportLock) {
    totalStartViewportLock = { left: window.scrollX, top: window.scrollY };
  }
  const viewportBeforeRender = totalStartViewportLock;
  if (
    (status.starting || status.running)
    && document.activeElement === elements.totalButton
  ) {
    elements.totalButton.blur();
  }
  lastRuntimeStatus = status;
  const insta = status.insta || adapterStatus(status, "insta");
  const tracker = adapterStatus(status, "tracker");
  const pedal = status.pedal || {};
  const deviceAction = status.device_action || {};
  const transitioning = Boolean(status.starting || status.stopping);
  trajectory.applyServerViewState(status.ui_tracker_view);

  elements.sessionName.textContent = status.session_id
    ? `${status.task_name || "Untitled task"} · ${status.session_id}`
    : `${status.task_name || "Untitled task"} · No capture session`;
  if (document.activeElement !== elements.taskNameInput) {
    elements.taskNameInput.value = status.task_name || "";
  }
  if (document.activeElement !== elements.outputRootInput) {
    elements.outputRootInput.value = status.output_root || "";
  }
  const settingsLocked = !status.can_edit_capture_settings;
  elements.taskNameInput.disabled = settingsLocked;
  elements.outputRootInput.disabled = settingsLocked;
  elements.saveCaptureSettings.disabled = settingsLocked;
  elements.saveCaptureSettings.textContent = settingsLocked ? "Locked during capture" : "Save settings";
  elements.newCaptureTask.disabled = !status.can_start_new_task || transitioning;
  elements.expectedSessionDir.textContent = status.running && status.session_dir
    ? status.session_dir
    : status.expected_session_dir || "Waiting for settings";
  elements.totalState.textContent = status.starting
    ? "Starting devices"
    : status.stopping ? "Stopping devices" : status.running ? "Capturing" : "Ready to start";
  elements.elapsed.textContent = formatDuration(status.duration_sec);
  elements.effectiveDuration.textContent = formatPreciseDuration(status.effective_duration_sec);
  elements.totalButton.disabled = Boolean(status.running || transitioning);
  elements.totalEndButton.disabled = !status.running || transitioning;
  document.body.classList.toggle("capture-running", Boolean(status.running));

  const singleArm = status.arm_mode === "single_arm";
  elements.armDual.classList.toggle("active", !singleArm);
  elements.armSingle.classList.toggle("active", singleArm);
  const armModeLocked = Boolean(
    status.running || transitioning || deviceAction.running || requestBusy
  );
  elements.armDual.disabled = armModeLocked;
  elements.armSingle.disabled = armModeLocked;
  const expectedTrackers = Number(status.expected_trackers || (singleArm ? 1 : 2));
  elements.armModeHint.textContent = singleArm
    ? "Single-arm mode uses Insta Right + HTC Right"
    : `Dual-arm mode requires ${expectedTrackers} trackers ready`;
  const selectedCameraTransport = String(
    status.camera_transport || (isUsbWebcam(insta) ? "usb_webcam" : "bluez_direct"),
  );
  const availableCameraTransports = new Set(
    status.available_camera_transports || ["bluez_direct", "usb_webcam", "mvs"],
  );
  const cameraTransportLocked = !Boolean(status.can_switch_camera_transport)
    || transitioning
    || Boolean(deviceAction.running)
    || requestBusy;
  elements.cameraTransportBluetooth.classList.toggle(
    "active",
    selectedCameraTransport === "bluez_direct",
  );
  elements.cameraTransportWired.classList.toggle(
    "active",
    selectedCameraTransport === "usb_webcam",
  );
  elements.cameraTransportMvs.classList.toggle(
    "active",
    selectedCameraTransport === "mvs",
  );
  elements.cameraTransportBluetooth.disabled = cameraTransportLocked
    || !availableCameraTransports.has("bluez_direct");
  elements.cameraTransportWired.disabled = cameraTransportLocked
    || !availableCameraTransports.has("usb_webcam");
  elements.cameraTransportMvs.disabled = cameraTransportLocked
    || !availableCameraTransports.has("mvs");
  elements.cameraTransportHint.textContent = status.running || transitioning
    ? "Camera connection is locked during capture"
    : deviceAction.running && deviceAction.action === "set_camera_transport"
      ? "Switching connection; no new stream is opened"
      : selectedCameraTransport === "usb_webcam"
        ? "PC MJPEG recording · Start Preview opens USB streams"
        : selectedCameraTransport === "mvs"
          ? "PC RGB recording · Start Preview opens both configured MVS serials"
          : "Camera-local recording · Connect opens Bluetooth control";
  trajectory.setVisibleRoles(singleArm ? ["htc_right"] : ["htc_right", "htc_left"]);
  trajectory.setHistoryActive(Boolean(status.running && status.segment_running));

  elements.segmentButton.textContent = status.segment_running ? "End episode" : "Start episode";
  elements.segmentButton.classList.toggle("running", status.segment_running);
  elements.segmentButton.disabled = !status.running || transitioning;
  elements.episodeCount.textContent = String(status.completed_segments || 0);
  elements.currentSegment.textContent = status.segment_running
    ? `Episode ${status.current_segment} in progress`
    : status.current_segment > 0 ? `Episode ${status.current_segment} complete` : "No active episode";
  elements.message.textContent = status.last_pedal_message || status.last_message || "Waiting for action";
  elements.logDetail.textContent = status.session_dir ? `${status.session_dir}/timestamp_log.jsonl` : "Not created";

  elements.modeSlam.classList.toggle("active", status.pedal_mode === "slam");
  elements.modeSegment.classList.toggle("active", status.pedal_mode === "segment");
  const slamCalibrationSupported = status.slam_calibration_supported !== false;
  elements.pedalModeField.hidden = !slamCalibrationSupported;
  elements.slamCalibrationBlock.hidden = !slamCalibrationSupported;
  elements.modeSlam.disabled = !status.running || transitioning;
  elements.modeSegment.disabled = !status.running || transitioning;
  renderSlamMarks(status.slam_events || [], Boolean(status.can_delete_slam_timestamp));
  const undoable = status.last_undoable_event;
  const undoType = undoable?.type || "";
  const canUndoSegment = status.can_undo_timestamp && ["segment_start", "segment_end"].includes(undoType);
  elements.undoTimestamp.disabled = !canUndoSegment;
  elements.undoTimestamp.textContent = canUndoSegment
    ? `Delete: ${eventDisplayTitle(undoable)}`
    : "Delete latest episode timestamp";
  elements.clearTimestamps.disabled = !status.can_clear_timestamps;

  renderCamera(insta, status, deviceAction);
  renderTracker(tracker, expectedTrackers);
  renderPedal(pedal);
  renderBleLogs([...(status.device_logs || []), ...(insta.recent_logs || [])]);
  renderEvents(status.recent_events || [], status);
  renderActiveViewAnnotations(status);
  renderGripperPreviewAvailability(status);

  document.querySelectorAll("button[data-action='slam']").forEach((button) => {
    button.disabled = !status.running || transitioning || status.slam_count >= 2;
  });

  // Hardware start can take several seconds while status polling continues.
  // Keep DOM updates and button focus changes from moving the operator's view.
  if (viewportBeforeRender && (
    window.scrollX !== viewportBeforeRender.left
    || window.scrollY !== viewportBeforeRender.top
  )) {
    window.scrollTo(viewportBeforeRender.left, viewportBeforeRender.top);
  }
  if (viewportBeforeRender && !status.starting) {
    window.requestAnimationFrame(() => {
      if (
        window.scrollX !== viewportBeforeRender.left
        || window.scrollY !== viewportBeforeRender.top
      ) {
        window.scrollTo(viewportBeforeRender.left, viewportBeforeRender.top);
      }
      if (totalStartViewportLock === viewportBeforeRender) {
        totalStartViewportLock = null;
      }
    });
  }
}

function isUsbWebcam(insta = {}) {
  return String(insta.type || "").toLowerCase() === "usb_webcam";
}

function isMvsCamera(insta = {}) {
  return String(insta.type || "").toLowerCase() === "mvs";
}

function isHostPreviewCamera(insta = {}) {
  return isUsbWebcam(insta) || isMvsCamera(insta);
}

function cameraRole(item = {}) {
  return String(item.role || item.camera_role || "").toLowerCase();
}

function cameraIdentity(item = {}, fallback = "") {
  return String(item.id || item.serial || item.device || item.path || item.address || item.name || fallback);
}

function cameraRoleLabel(item = {}, mvsCamera = false) {
  const role = cameraRole(item);
  if (role === "insta_right") return mvsCamera ? "Right Wrist" : "Insta Right";
  if (role === "insta_left") return mvsCamera ? "Left Wrist" : "Insta Left";
  return mvsCamera ? "Unassigned Wrist Camera" : "Insta Unassigned";
}

function setMvsLiveLayout(enabled) {
  document.body.classList.toggle("camera-transport-mvs", enabled);
  if (enabled) {
    elements.mvsLiveRow.hidden = false;
    if (elements.trajectoryPanel.parentElement !== elements.mvsLiveRow) {
      elements.mvsLiveRow.append(elements.trajectoryPanel);
    }
    [cameraPreviewViews.insta_right, cameraPreviewViews.insta_left].forEach((view) => {
      if (view.card.parentElement !== elements.mvsLiveRow) {
        elements.mvsLiveRow.append(view.card);
      }
    });
    return;
  }

  elements.mvsLiveRow.hidden = true;
  if (elements.trajectoryPanel.parentElement !== elements.trajectoryPanelAnchor.parentElement) {
    elements.trajectoryPanelAnchor.insertAdjacentElement("afterend", elements.trajectoryPanel);
  }
  [cameraPreviewViews.insta_right, cameraPreviewViews.insta_left].forEach((view) => {
    if (view.card.parentElement !== elements.cameraPreviewGrid) {
      elements.cameraPreviewGrid.append(view.card);
    }
  });
}

function cameraPreviewRunning(insta = {}) {
  if (Object.prototype.hasOwnProperty.call(insta, "preview_running")) {
    return Boolean(insta.preview_running);
  }
  if (Object.prototype.hasOwnProperty.call(insta, "previewing")) {
    return Boolean(insta.previewing);
  }
  return (insta.cameras || []).some((item) => Boolean(
    item.preview_running
    || item.previewing
    || item.streaming
    || item.source_ready
    || item.stream_state === "streaming",
  ));
}

function normalizeCameraViewMode(insta = {}) {
  if (isMvsCamera(insta)) return "mvs";
  const requested = String(insta.view_mode || insta.camera_view_mode || insta.projection || "panorama").toLowerCase();
  return requested === "flat" ? "flat" : "panorama";
}

function cameraItemReady(item = {}, usbWebcam = false) {
  if (Object.prototype.hasOwnProperty.call(item, "ready")) return Boolean(item.ready);
  if (!Boolean(item.connected)) return false;
  if (usbWebcam) return !Boolean(item.stale || item.preview_stale);
  return item.notifying !== false;
}

function cameraFrameSequence(item = {}) {
  const value = item.frame_seq
    ?? item.preview_frame_seq
    ?? item.latest_frame_seq
    ?? item.latest_sequence
    ?? item.sequence;
  const sequence = Number(value);
  return Number.isFinite(sequence) && sequence >= 0 ? sequence : null;
}

function cameraPreviewVersion(item = {}) {
  const version = item.preview_version;
  if (version !== undefined && version !== null && String(version) !== "") {
    return String(version);
  }
  const sequence = cameraFrameSequence(item);
  return sequence === null ? null : String(sequence);
}

function cameraFrameAgeMs(item = {}) {
  const value = item.preview_age_ms
    ?? item.frame_age_ms
    ?? item.last_frame_age_ms
    ?? item.latest_source_age_ms;
  const age = Number(value);
  return Number.isFinite(age) && age >= 0 ? age : null;
}

function renderUsbCameraPreviews(insta, status, deviceAction) {
  const mvsCamera = isMvsCamera(insta);
  setMvsLiveLayout(mvsCamera);
  const viewMode = normalizeCameraViewMode(insta);
  const previewRunning = cameraPreviewRunning(insta);
  const captureLocked = Boolean(status.running || status.starting || status.stopping || status.transitioning);
  const modeLocked = captureLocked || Boolean(deviceAction.running) || requestBusy;
  const x3Only = String(insta.required_camera_model || "") === "Insta360 X3";
  const allowedRoles = new Set(status.camera_roles || insta.capture_roles || ["insta_right", "insta_left"]);
  const camerasByRole = new Map((insta.cameras || []).map((item) => [cameraRole(item), item]));

  elements.cameraPreviewToolbar.hidden = mvsCamera;
  elements.cameraViewPanorama.classList.toggle("active", viewMode === "panorama");
  elements.cameraViewFlat.classList.toggle("active", viewMode === "flat");
  elements.cameraViewPanorama.setAttribute("aria-pressed", String(viewMode === "panorama"));
  elements.cameraViewFlat.setAttribute("aria-pressed", String(viewMode === "flat"));
  elements.cameraViewPanorama.disabled = modeLocked || x3Only;
  elements.cameraViewFlat.disabled = modeLocked;
  elements.cameraViewModeNote.textContent = viewMode === "flat"
    ? "Flat selects the native 1920×1080 Webcam profile. Its pixels form a vertical dual-fisheye stack. The worker JPEG is shown as-is; no browser post-processing is applied."
    : x3Only
      ? "Insta360 X3 Webcam has no in-camera 2:1 panorama profile; use Flat View."
      : "Panorama selects the camera-native 2880×1440 2:1 profile. The worker JPEG is shown as-is; no browser post-processing is applied.";

  const staleAfterMs = Number(insta.preview_stale_after_ms ?? insta.max_frame_age_ms ?? 1500);
  Object.entries(cameraPreviewViews).forEach(([role, view]) => {
    const visible = !allowedRoles.size || allowedRoles.has(role);
    view.card.hidden = !visible;
    if (!visible) return;

    const camera = camerasByRole.get(role) || {};
    view.image.alt = mvsCamera
      ? `Live ${view.hand}-wrist MVS camera preview`
      : `Live ${view.hand}-wrist USB camera preview`;
    const connected = Boolean(camera.connected);
    const ready = cameraItemReady(camera, true);
    const sequence = cameraFrameSequence(camera);
    const previewVersion = cameraPreviewVersion(camera);
    const previewKey = previewVersion ?? (sequence === null ? null : String(sequence));
    const ageMs = cameraFrameAgeMs(camera);
    const explicitlyStale = Boolean(camera.stale || camera.preview_stale || camera.stream_stalled);
    const stale = explicitlyStale || (previewRunning && ageMs !== null && ageMs > staleAfterMs);
    const hasFrame = previewKey !== null || Boolean(camera.preview_available || camera.has_preview_frame);
    const failedSequence = view.image.dataset.failedSequence;
    const loadFailed = hasFrame && failedSequence === String(previewKey);
    const modeClass = mvsCamera ? "mode-mvs" : viewMode === "flat" ? "mode-flat" : "mode-panorama";
    view.stage.classList.remove("mode-flat", "mode-panorama", "mode-mvs", "state-stale", "state-error");
    view.stage.classList.add(modeClass);
    view.stage.classList.toggle("state-stale", stale);
    view.stage.classList.toggle("state-error", loadFailed);

    if (
      hasFrame
      && previewKey !== null
      && view.image.dataset.sourceVersion !== previewKey
      && view.image.dataset.previewRequestPending !== "1"
    ) {
      view.image.dataset.sequence = previewKey;
      view.image.dataset.sourceVersion = previewVersion || previewKey;
      view.image.dataset.previewRequestPending = "1";
      delete view.image.dataset.loadedSequence;
      delete view.image.dataset.failedSequence;
      view.image.src = `/api/camera-preview/${role}.jpg?version=${encodeURIComponent(previewKey)}`;
    }

    const emptyTitle = view.empty.querySelector("strong");
    const emptyDetail = view.empty.querySelector("span");
    if (stale) {
      view.image.hidden = !view.image.src;
      view.empty.hidden = false;
      emptyTitle.textContent = "Preview stale";
      emptyDetail.textContent = "No recent camera frame was received. Recording readiness is disabled.";
      view.state.textContent = "Stale";
    } else if (loadFailed) {
      view.image.hidden = true;
      view.empty.hidden = false;
      emptyTitle.textContent = "Preview unavailable";
      emptyDetail.textContent = "The latest preview image could not be loaded. The camera stream will be retried on a new frame.";
      view.state.textContent = "Error";
    } else if (hasFrame) {
      view.image.hidden = false;
      view.empty.hidden = true;
      view.state.textContent = camera.recording || insta.recording ? "Recording" : "Live";
    } else {
      view.image.hidden = true;
      view.empty.hidden = false;
      emptyTitle.textContent = previewRunning && connected ? "Waiting for first frame" : "Preview not started";
      emptyDetail.textContent = connected
        ? "Start preview to verify this camera before capture."
        : `Connect the ${view.hand} camera and start preview.`;
      view.state.textContent = ready ? "Ready" : connected ? "Connected" : "Disconnected";
    }
    view.state.dataset.state = view.state.textContent.toLowerCase();

    const width = Number(camera.native_width ?? camera.width ?? 0);
    const height = Number(camera.native_height ?? camera.height ?? 0);
    const fps = Number(camera.native_fps ?? camera.fps ?? 0);
    const format = width > 0 && height > 0
      ? `${width}×${height}${fps > 0 ? ` @ ${fps.toFixed(fps % 1 ? 2 : 0)} fps` : ""}`
      : "Format pending";
    const device = camera.serial || camera.device || camera.path || "Camera identity pending";
    const freshness = ageMs === null ? "frame age unavailable" : `frame age ${ageMs.toFixed(1)} ms`;
    view.detail.textContent = `${device} · ${format} · ${freshness}`;
  });
}

let cameraPreviewRefreshCounter = 0;

function refreshUsbCameraPreviewFrames() {
  if (document.hidden) return;
  const insta = lastRuntimeStatus.insta || adapterStatus(lastRuntimeStatus, "insta");
  if (!isHostPreviewCamera(insta) || !cameraPreviewRunning(insta)) return;
  const camerasByRole = new Map((insta.cameras || []).map((item) => [cameraRole(item), item]));
  const now = Date.now();
  Object.entries(cameraPreviewViews).forEach(([role, view]) => {
    if (view.card.hidden || view.image.dataset.previewRequestPending === "1") return;
    const camera = camerasByRole.get(role) || {};
    if (
      !cameraItemReady(camera, true)
      || !Boolean(camera.preview_available || camera.has_preview_frame)
      || Boolean(camera.stale || camera.preview_stale || camera.stream_stalled)
    ) return;

    const sourceVersion = cameraPreviewVersion(camera) || "live";
    cameraPreviewRefreshCounter += 1;
    view.image.dataset.sequence = sourceVersion;
    view.image.dataset.sourceVersion = sourceVersion;
    view.image.dataset.previewRequestPending = "1";
    delete view.image.dataset.loadedSequence;
    delete view.image.dataset.failedSequence;
    view.image.src = `/api/camera-preview/${role}.jpg?version=${encodeURIComponent(sourceVersion)}&live=${encodeURIComponent(`${now}-${cameraPreviewRefreshCounter}`)}`;
  });
}

function renderCamera(insta, status, deviceAction) {
  const usbWebcam = isUsbWebcam(insta);
  const mvsCamera = isMvsCamera(insta);
  const hostPreview = usbWebcam || mvsCamera;
  if (elements.cameraClockProtocol) {
    const title = elements.cameraClockProtocol.querySelector("strong");
    const detail = elements.cameraClockProtocol.querySelector("span");
    if (hostPreview) {
      title.textContent = mvsCamera ? "MVS frame continuity required" : "USB frame continuity required";
      detail.textContent = mvsCamera
        ? "MVS frames use SDK host timestamps converted to monotonic exposure-midpoint time. Both camera frame indexes and timestamps must remain increasing."
        : "USB Webcam frames use host-monotonic V4L2 start-of-exposure timestamps. No gyro-correlation motion is required; both camera frame indexes must remain continuous.";
    } else {
      title.textContent = "Clock-sync motion required";
      detail.textContent = "After total start, every 10–15 minutes, and before total end, rotate each rigid X5 + HTC assembly for 5–10 seconds using non-periodic multi-axis motion.";
    }
  }
  const cameras = Array.isArray(insta.cameras) ? insta.cameras : [];
  const connected = Boolean(insta.connected);
  const countedConnected = cameras.filter((item) => Boolean(item.connected)).length;
  const connectedValue = insta.connected_cameras ?? insta.connected_cameras_estimate;
  const connectedCount = Number(connectedValue ?? (countedConnected || (connected ? 1 : 0)));
  const expectedCount = Number(insta.expected_cameras ?? status.expected_cameras ?? 2);
  const previewRunning = hostPreview && cameraPreviewRunning(insta);
  const staleAfterMs = Number(insta.preview_stale_after_ms ?? insta.max_frame_age_ms ?? 1500);
  const staleCamera = hostPreview && cameras.some((item) => {
    const ageMs = cameraFrameAgeMs(item);
    return Boolean(item.stale || item.preview_stale || item.stream_stalled)
      || (previewRunning && ageMs !== null && ageMs > staleAfterMs);
  });
  const stale = hostPreview && Boolean(insta.stale || insta.preview_stale || insta.stream_stalled || staleCamera);
  const hasExplicitReady = Object.prototype.hasOwnProperty.call(insta, "ready");
  const ready = hasExplicitReady
    ? Boolean(insta.ready) && !stale
    : expectedCount > 0 && connectedCount >= expectedCount && !stale;
  const simulated = ["dry_run", "bluez_remote_simulated", "bluez_direct_simulated"].includes(insta.type);
  const state = simulated ? "connected" : ready ? "ready" : connectedCount > 0 ? (stale ? "lost" : "connected") : "offline";
  const action = String(deviceAction.action || "");
  const scanning = ["scan_insta", "scan_cameras"].includes(action);
  const connecting = ["connect_insta", "connect_cameras"].includes(action);
  const disconnecting = ["disconnect_insta", "disconnect_cameras"].includes(action);
  const switchingTransport = action === "set_camera_transport";
  const captureLocked = Boolean(status.running || status.starting || status.stopping || status.transitioning);

  elements.cameraTransportLabel.textContent = mvsCamera ? "HIKROBOT · MVS" : usbWebcam ? "INSTA360 · USB" : "INSTA360";
  elements.cameraTitle.textContent = mvsCamera ? "MVS Wrist Cameras" : usbWebcam ? "USB Cameras" : "Bluetooth cameras";
  elements.cameraLogTitle.textContent = hostPreview ? "Camera action log" : "Bluetooth action log";
  setMvsLiveLayout(mvsCamera);
  // In MVS mode the two preview cards are moved into ``mvsLiveRow`` beside
  // the HTC canvas.  Hide only their now-empty USB wrapper; never hide or
  // detach the live MVS row itself.
  elements.usbCameraPreviewPanel.hidden = !hostPreview || mvsCamera;
  elements.mvsLiveRow.hidden = !mvsCamera;

  setCardState(elements.cameraCard, deviceAction.running ? "working" : state);
  elements.cameraCount.textContent = `${connectedCount} / ${expectedCount}`;
  elements.cameraCountLabel.textContent = deviceAction.running
    ? switchingTransport ? "Switching connection" : scanning ? "Scanning" : disconnecting ? "Stopping preview" : "Connecting"
    : ready ? "All ready" : stale ? "Stream stale" : connectedCount > 0 ? "Partially connected" : "Disconnected";
  setSignal(
    elements.instaStatus,
    simulated
      ? "Camera simulation"
      : ready
        ? `Cameras ${connectedCount}/${expectedCount} ready`
        : stale
          ? `${mvsCamera ? "MVS" : "USB"} camera stream stale`
          : connectedCount > 0 ? `Cameras ${connectedCount}/${expectedCount}` : "Cameras disconnected",
    simulated ? "connected" : state,
  );

  const allowedCameraRoles = new Set(status.camera_roles || insta.capture_roles || []);
  const cameraVisible = (item) => !allowedCameraRoles.size || allowedCameraRoles.has(cameraRole(item));
  const discoveredUnits = new Map();
  [...(insta.discovered_cameras || []), ...cameras].filter(cameraVisible).forEach((item, index) => {
    const key = cameraIdentity(item, `camera-${index}`);
    discoveredUnits.set(key, { ...discoveredUnits.get(key), ...item });
  });
  const units = [...discoveredUnits.values()];
  elements.cameraDeviceList.innerHTML = units.length
    ? units.map((item) => {
        const itemConnected = Boolean(item.connected);
        const itemReady = cameraItemReady(item, hostPreview);
        const itemStale = hostPreview && Boolean(item.stale || item.preview_stale || item.stream_stalled);
        const unitState = itemReady ? "ready" : itemConnected ? (itemStale ? "lost" : "connected") : "offline";
        const identity = hostPreview
          ? [item.serial, item.device || item.path, item.name].filter(Boolean).join(" · ") || "USB camera"
          : `${shortAddress(item.address)} · ${item.name || "Insta360"}`;
        const itemLabel = itemReady ? (hostPreview && previewRunning ? "Streaming" : "Connected") : itemStale ? "Stale" : itemConnected ? "Connected" : "Discovered";
        return `<div class="unit-row state-${unitState}"><i></i><div><strong>${escapeHtml(cameraRoleLabel(item, mvsCamera))}</strong><span>${escapeHtml(identity)}</span></div><b>${itemLabel}</b></div>`;
      }).join("")
    : [mvsCamera ? "Right Wrist" : "Insta Right", mvsCamera ? "Left Wrist" : "Insta Left"].slice(0, expectedCount).map((roleName) => `<div class="unit-row state-offline"><i></i><div><strong>${roleName}</strong><span>${mvsCamera ? "Waiting for configured MVS serial" : usbWebcam ? "Waiting for USB discovery" : "Waiting for scan"}</span></div><b>Disconnected</b></div>`).join("");

  const names = cameras.map((item) => item.name || item.serial || item.device || item.address).filter(Boolean).join(", ");
  if (hostPreview) {
    elements.instaDetail.textContent = stale
      ? "USB preview is stale; fresh frames are required before capture"
      : ready
        ? `${names || "Insta360 USB"} · ${insta.recording ? "Recording to PC" : previewRunning ? "Live preview" : "USB ready"}`
        : connectedCount > 0
          ? `${connectedCount} detected; waiting for ${Math.max(0, expectedCount - connectedCount)} camera(s) or fresh frames`
          : mvsCamera ? "Enumerate MVS cameras, then start preview" : "Refresh USB cameras, then start preview";
  } else {
    elements.instaDetail.textContent = ready
      ? `${names || "Insta360"} · ${insta.recording ? "Recording" : "Bluetooth idle"}`
      : connectedCount > 0 ? `${connectedCount} connected; ${Math.max(0, expectedCount - connectedCount)} remaining` : "Scan for powered-on Insta360 cameras";
  }

  elements.scanInsta.disabled = captureLocked || Boolean(deviceAction.running);
  elements.connectInsta.disabled = captureLocked || Boolean(deviceAction.running) || (!hostPreview && ready);
  if (hostPreview) {
    elements.scanInsta.textContent = deviceAction.running && scanning
      ? (mvsCamera ? "Enumerating…" : "Refreshing…")
      : (mvsCamera ? "Enumerate MVS Cameras" : "Refresh USB Cameras");
    elements.connectInsta.textContent = deviceAction.running && connecting
      ? "Starting preview…"
      : deviceAction.running && disconnecting ? "Stopping preview…" : previewRunning ? "Stop Preview" : "Start Preview";
    renderUsbCameraPreviews(insta, status, deviceAction);
  } else {
    elements.scanInsta.textContent = deviceAction.running && scanning ? "Scanning…" : "Scan Bluetooth";
    elements.connectInsta.textContent = deviceAction.running && connecting ? "Connecting…" : ready ? "Cameras ready" : "Connect cameras";
  }
}

function renderTracker(tracker, expectedOverride) {
  if (!tracker || !Object.keys(tracker).length) {
    trajectory.ingest([]);
    setSignal(elements.trackerStatus, "Trackers disabled", "connected");
    setCardState(elements.trackerCard, "offline");
    elements.trackerCount.textContent = `0 / ${expectedOverride || 0}`;
    elements.trackerCountLabel.textContent = "Disabled";
    elements.trackerDeviceList.innerHTML = '<div class="unit-placeholder">pyvut is disabled in the current configuration</div>';
    elements.trackerDetail.textContent = "pyvut is disabled in the current configuration";
    return;
  }

  const expected = Number(tracker.expected_trackers || expectedOverride || 2);
  const readyCount = Number(tracker.ready_trackers || 0);
  const recentCount = Number(tracker.recent_trackers || 0);
  const state = tracker.tracking_state || (tracker.connected ? "connected" : "offline");
  const cardState = state === "disconnected" || state === "receiver_error"
    ? "offline"
    : state === "waiting" || state === "tracking_lost"
      ? "lost"
      : state === "partial" ? "connected" : state;
  const label = tracker.type === "simulated_tracker"
    ? "Tracker simulation"
    : state === "ready" ? "All trackers ready"
      : state === "partial" ? `${readyCount} / ${expected} Trackers ready`
        : state === "tracking_lost" ? "Tracker pose tracking lost"
          : state === "waiting" ? "Receiver ready · waiting for Tracker RF"
            : state === "receiver_error" ? "HTC receiver read loop stopped"
              : state === "connected" ? "Trackers connected" : "HTC receiver disconnected";

  setSignal(elements.trackerStatus, label, cardState);
  setCardState(elements.trackerCard, cardState);
  elements.trackerCount.textContent = `${readyCount} / ${expected}`;
  elements.trackerCountLabel.textContent = state === "ready" ? "All ready"
    : state === "partial" ? "Partially ready"
      : state === "tracking_lost" ? "Pose lost"
        : state === "waiting" ? "RF waiting"
          : state === "receiver_error" ? "Receiver error"
            : state === "connected" ? "Receiver connected" : "Disconnected";

  const singleArm = lastRuntimeStatus.arm_mode === "single_arm";
  const desiredRoles = singleArm ? ["htc_right"] : ["htc_right", "htc_left"];
  const allowedRoles = new Set(desiredRoles);
  const states = [...(tracker.tracker_states || [])]
    .filter((item) => allowedRoles.has(String(item.role || "").toLowerCase()))
    .sort((a, b) => String(a.role || a.mac).localeCompare(String(b.role || b.mac)));
  const currentStates = states.filter((item) => Number(item.age_ms ?? 999999) <= 1500);
  const displayStates = currentStates.length ? currentStates : states;
  const statesByRole = new Map(
    displayStates.map((item) => [String(item.role || "").toLowerCase(), item]),
  );
  const rows = desiredRoles.map((role, index) => {
    const item = statesByRole.get(role);
    const roleName = role === "htc_right" ? "HTC Right" : "HTC Left";
    if (!item) {
      const waiting = Boolean(tracker.receiver_healthy);
      return `<div class="unit-row state-${waiting ? "lost" : "offline"}"><i></i><div><strong>${roleName}</strong><span>${waiting ? "Receiver open; wake/move Tracker" : `Waiting for physical ${roleName}`}</span></div><b>${waiting ? "RF waiting" : "Disconnected"}</b></div>`;
    }
    const itemState = trackerState(item);
    const statusCode = item.tracking_status_raw ?? item.tracking_status ?? "-";
    const stableId = item.hardware_serial || item.physical_id || item.mac;
    return `<div class="unit-row state-${itemState.key}"><i></i><div><strong>${roleName}</strong><span>${escapeHtml(shortAddress(stableId))} · Status ${escapeHtml(statusCode)}</span></div><b>${itemState.label}</b></div>`;
  });
  elements.trackerDeviceList.innerHTML = rows.join("") || '<div class="unit-placeholder">Waiting for tracker data</div>';
  const rfCount = Number(tracker.rf_connected_trackers || 0);
  elements.trackerDetail.textContent = tracker.receiver_healthy
    ? `${tracker.mode || "pyvut"} · Dongle healthy · RF slots ${rfCount}/${expected} · recent packets ${recentCount}/${expected} · 6DoF ${readyCount}/${expected} · ${tracker.recording ? "Recording" : "Live monitoring"}`
    : `${tracker.message || tracker.error || "Waiting for pyvut receiver"}`;
  elements.recoverHtc.disabled = Boolean(lastRuntimeStatus.running)
    || Boolean(lastRuntimeStatus.transitioning)
    || requestBusy;
  elements.recoverHtc.textContent = state === "ready" ? "Reconnect / Pair" : "Repair HTC connection";
  elements.releaseOtherHtc.disabled = Boolean(tracker.connected)
    || Boolean(lastRuntimeStatus.running)
    || Boolean(lastRuntimeStatus.transitioning)
    || requestBusy;
  elements.releaseOtherHtc.textContent = tracker.connected
    ? "HTC owned by DataCap"
    : "Stop other HTC service";

  trajectory.ingest(tracker.connected ? states : []);
}

function renderPedal(pedal) {
  const connected = Boolean(pedal.connected);
  setSignal(elements.pedalStatus, connected ? "Pedal connected" : "Pedal disconnected", connected ? "ready" : "offline");
  setCardState(elements.pedalCard, connected ? "ready" : "offline");
  elements.pedalCardState.textContent = connected ? "Connected" : "Disconnected";
  elements.pedalDetail.textContent = connected ? `${pedal.device} · Dedicated HID input` : (pedal.message || "USB foot pedal not detected");
}

function renderPedalLive(pedal) {
  const connected = Boolean(pedal.connected);
  const pressed = connected && Boolean(pedal.pressed);
  elements.pedalCard.classList.toggle("pedal-pressed", pressed);
  elements.pedalLiveIndicator.classList.toggle("pressed", pressed);
  elements.pedalLiveIndicator.classList.toggle("offline", !connected);
  if (!connected) {
    elements.pedalLiveText.textContent = "Pedal disconnected";
    elements.pedalLiveProgress.textContent = "Waiting for USB input device";
    return;
  }
  if (pressed) {
    elements.pedalLiveText.textContent = pedal.transition === "long_triggered" ? "Long press triggered" : "Pedal pressed";
  } else if (pedal.transition === "released") {
    elements.pedalLiveText.textContent = "Pedal released";
  } else {
    elements.pedalLiveText.textContent = "Pedal idle";
  }
  if (!pedal.capture_running) {
    const count = Number(pedal.start_count || 0);
    const required = Number(pedal.start_required || 3);
    const remaining = Number(pedal.start_window_remaining_sec || 0);
    elements.pedalLiveProgress.textContent = count > 0
      ? `Start progress ${count} / ${required} · ${remaining.toFixed(1)} s remaining`
      : `Start progress 0 / ${required}`;
  } else if (pedal.pedal_mode === "slam") {
    elements.pedalLiveProgress.textContent = `SLAM calibration · ${Number(pedal.slam_count || 0)} / 2 recorded`;
  } else {
    const segment = Number(pedal.current_segment || 0);
    elements.pedalLiveProgress.textContent = pedal.segment_running
      ? `Episode ${segment} in progress · Next press ends it`
      : `Episode mode · Next press starts episode ${segment + 1}`;
  }
}

function renderBleLogs(logs) {
  if (!logs.length) {
    elements.bleLog.innerHTML = '<li class="empty-log">No scan has been run</li>';
    return;
  }
  const unique = new Map();
  logs.forEach((item) => unique.set(`${item.unix_time_ns || 0}:${item.action || ""}:${item.message || ""}`, item));
  const ordered = [...unique.values()]
    .sort((a, b) => Number(a.unix_time_ns || 0) - Number(b.unix_time_ns || 0))
    .slice(-10)
    .reverse();
  elements.bleLog.innerHTML = ordered.map((item) => {
    const when = item.unix_time_ns ? new Date(Number(item.unix_time_ns) / 1e6).toLocaleTimeString("en-GB", { hour12: false }) : "--:--:--";
    const errors = (item.errors || []).length ? ` · ${(item.errors || []).slice(-2).join("; ")}` : "";
    return `<li class="${item.ok === false ? "error" : ""}"><time>${when}</time><span>${escapeHtml(item.message || item.action || "BLE event")}${escapeHtml(errors)}</span></li>`;
  }).join("");
}

function renderEvents(events, status = {}) {
  if (!events.length) {
    elements.eventsBody.innerHTML = '<tr><td colspan="6" class="empty">No timestamps</td></tr>';
    return;
  }
  elements.eventsBody.innerHTML = [...events].reverse().map((event, index) => {
    const time = new Date(event.unix_time_ms).toLocaleTimeString("en-GB", { hour12: false, fractionalSecondDigits: 3 });
    const canDelete = Boolean(status.can_undo_timestamp && index === 0 && ["segment_start", "segment_end"].includes(event.type));
    const action = canDelete ? '<button class="inline-delete" type="button" data-undo-latest>Delete</button>' : '<span class="not-deletable">—</span>';
    return `<tr><td>${escapeHtml(eventDisplayTitle(event))}</td><td>${event.segment_index ?? "-"}</td><td>${escapeHtml(event.input_source || "-")}</td><td>${time}</td><td>${Number(event.input_latency_ms || 0).toFixed(1)} ms</td><td>${action}</td></tr>`;
  }).join("");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
}

class TrackerTrajectory3D {
  constructor(canvas, empty, legend, labelLayer) {
    this.canvas = canvas;
    this.empty = empty;
    this.legend = legend;
    this.labelLayer = labelLayer;
    this.tracks = new Map();
    this.lastTimestamp = new Map();
    this.historyActive = false;
    this.visibleRoles = new Set(["htc_right", "htc_left"]);
    this.fixedView = Object.freeze({ center: [0, 0, 0], range: 1, groundZ: 0 });
    // The camera looks from the physical +X/back side. A fixed +90 degree
    // camera roll rotates the complete rendered canvas counter-clockwise;
    // tracker positions and orientations are never modified.
    this.azimuth = 0;
    this.elevation = 0.32;
    this.distance = 3.4;
    this.screenRoll = Math.PI / 2;
    // Orbit input is interpreted in the already-rolled screen coordinate
    // system.  A slightly faster default keeps a full view change from
    // requiring several long drags; Shift temporarily enables fine control.
    this.orbitRadiansPerPixel = 0.0115;
    this.viewLocked = false;
    this.hasLocalViewState = false;
    this.serverViewApplied = false;
    this.serverPersistTimer = null;
    this.loadViewState();
    this.dragging = false;
    this.lastPointer = null;
    this.drawPending = false;
    this.maxPoints = 900;
    this.gl = null;
    this.program = null;
    this.positionBuffer = null;
    this.colorBuffer = null;
    this.webglError = null;
    this.initializeWebGL();
    this.bindInteraction();
    this.updateViewControls();
    new ResizeObserver(() => this.requestDraw()).observe(canvas.parentElement);
  }

  loadViewState() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(TRACKER_3D_VIEW_STORAGE_KEY) || "null");
      if (!saved || saved.version !== 1) return;
      if (Number.isFinite(saved.azimuth)) this.azimuth = saved.azimuth;
      if (Number.isFinite(saved.elevation)) {
        this.elevation = Math.max(-1.2, Math.min(1.2, saved.elevation));
      }
      if (Number.isFinite(saved.distance)) {
        this.distance = Math.max(1.1, Math.min(12, saved.distance));
      }
      this.viewLocked = Boolean(saved.locked);
      this.hasLocalViewState = true;
    } catch (error) {
      console.warn("Ignoring invalid saved HTC 3D view:", error);
    }
  }

  persistViewState() {
    try {
      window.localStorage.setItem(TRACKER_3D_VIEW_STORAGE_KEY, JSON.stringify({
        version: 1,
        azimuth: this.azimuth,
        elevation: this.elevation,
        distance: this.distance,
        locked: this.viewLocked,
      }));
    } catch (error) {
      console.warn("Unable to persist the HTC 3D view:", error);
    }
  }

  currentViewState() {
    return {
      version: 1,
      azimuth: this.azimuth,
      elevation: this.elevation,
      distance: this.distance,
      locked: this.viewLocked,
    };
  }

  applyServerViewState(saved) {
    if (this.serverViewApplied) return;
    this.serverViewApplied = true;
    if (this.hasLocalViewState || !saved || saved.version !== 1) return;
    const azimuth = Number(saved.azimuth);
    const elevation = Number(saved.elevation);
    const distance = Number(saved.distance);
    if (![azimuth, elevation, distance].every(Number.isFinite)) return;
    this.azimuth = azimuth;
    this.elevation = Math.max(-1.2, Math.min(1.2, elevation));
    this.distance = Math.max(1.1, Math.min(12, distance));
    this.viewLocked = Boolean(saved.locked);
    this.persistViewState();
    this.updateViewControls();
    this.requestDraw();
  }

  persistViewStateToServer() {
    window.clearTimeout(this.serverPersistTimer);
    this.serverPersistTimer = null;
    fetchJson("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "save_tracker_view", value: this.currentViewState() }),
    }, 3000).catch((error) => {
      console.warn("Unable to persist the HTC 3D view to application settings:", error);
    });
  }

  scheduleServerViewPersist() {
    window.clearTimeout(this.serverPersistTimer);
    this.serverPersistTimer = window.setTimeout(() => this.persistViewStateToServer(), 350);
  }

  updateViewControls() {
    this.canvas.classList.toggle("view-locked", this.viewLocked);
    if (elements.lockView) {
      elements.lockView.textContent = this.viewLocked ? "Unlock view" : "Lock current view";
      elements.lockView.setAttribute("aria-pressed", String(this.viewLocked));
    }
    this.updateViewReadout();
  }

  updateViewReadout() {
    const azimuthDegrees = ((this.azimuth * 180 / Math.PI) % 360 + 360) % 360;
    const elevationDegrees = this.elevation * 180 / Math.PI;
    const zoom = 3.4 / this.distance;
    const lockLabel = this.viewLocked ? "LOCKED" : "editable";
    elements.trajectoryScale.textContent =
      `view az ${azimuthDegrees.toFixed(1)}° · el ${elevationDegrees.toFixed(1)}° · roll +90.0° · scale ${zoom.toFixed(2)}× · ${lockLabel}`;
  }

  toggleViewLock() {
    this.viewLocked = !this.viewLocked;
    this.persistViewState();
    this.persistViewStateToServer();
    this.updateViewControls();
    this.requestDraw();
  }

  initializeWebGL() {
    try {
      const gl = this.canvas.getContext("webgl", {
        alpha: false,
        antialias: true,
        depth: true,
        premultipliedAlpha: false,
        preserveDrawingBuffer: false,
      });
      if (!gl) throw new Error("WebGL is unavailable in this browser");
      this.gl = gl;
      this.program = this.createProgram(
        `
          attribute vec3 a_position;
          attribute vec4 a_color;
          uniform mat4 u_mvp;
          uniform float u_point_size;
          varying vec4 v_color;
          void main() {
            gl_Position = u_mvp * vec4(a_position, 1.0);
            gl_PointSize = u_point_size;
            v_color = a_color;
          }
        `,
        `
          precision mediump float;
          uniform float u_round_points;
          varying vec4 v_color;
          void main() {
            if (u_round_points > 0.5) {
              vec2 delta = gl_PointCoord - vec2(0.5);
              if (dot(delta, delta) > 0.25) discard;
            }
            gl_FragColor = v_color;
          }
        `,
      );
      this.locations = {
        position: gl.getAttribLocation(this.program, "a_position"),
        color: gl.getAttribLocation(this.program, "a_color"),
        mvp: gl.getUniformLocation(this.program, "u_mvp"),
        pointSize: gl.getUniformLocation(this.program, "u_point_size"),
        roundPoints: gl.getUniformLocation(this.program, "u_round_points"),
      };
      this.positionBuffer = gl.createBuffer();
      this.colorBuffer = gl.createBuffer();
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      this.webglError = null;
    } catch (error) {
      this.gl = null;
      this.webglError = error instanceof Error ? error.message : String(error);
      console.error("Unable to initialize the HTC WebGL view:", error);
    }
  }

  compileShader(type, source) {
    const gl = this.gl;
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || "unknown shader error";
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  }

  createProgram(vertexSource, fragmentSource) {
    const gl = this.gl;
    const vertexShader = this.compileShader(gl.VERTEX_SHADER, vertexSource);
    const fragmentShader = this.compileShader(gl.FRAGMENT_SHADER, fragmentSource);
    const program = gl.createProgram();
    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    gl.deleteShader(vertexShader);
    gl.deleteShader(fragmentShader);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program) || "unknown WebGL link error";
      gl.deleteProgram(program);
      throw new Error(message);
    }
    return program;
  }

  setVisibleRoles(roles) {
    const next = new Set((roles || []).map((role) => String(role).toLowerCase()));
    const changed = next.size !== this.visibleRoles.size
      || [...next].some((role) => !this.visibleRoles.has(role));
    if (!changed) return;
    this.visibleRoles = next;
    this.tracks.forEach((track, key) => {
      if (!this.visibleRoles.has(String(track.sourceRole || "").toLowerCase())) {
        this.tracks.delete(key);
        this.lastTimestamp.delete(key);
      }
    });
    this.updateLegend();
    this.requestDraw();
  }

  setHistoryActive(active) {
    const next = Boolean(active);
    if (this.historyActive === next) return;
    this.historyActive = next;
    this.clearHistory();
  }

  clearHistory() {
    this.tracks.forEach((track) => {
      track.points = [];
    });
    this.view = null;
    this.updateLegend();
    this.requestDraw();
  }

  bindInteraction() {
    this.canvas.addEventListener("pointerdown", (event) => {
      if (this.viewLocked) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      this.dragging = true;
      this.lastPointer = [event.clientX, event.clientY];
      this.canvas.setPointerCapture(event.pointerId);
      this.canvas.classList.add("dragging");
    });
    this.canvas.addEventListener("pointermove", (event) => {
      if (!this.dragging || !this.lastPointer) return;
      event.preventDefault();
      const coalesced = typeof event.getCoalescedEvents === "function"
        ? event.getCoalescedEvents()
        : [];
      const samples = coalesced.length ? coalesced : [event];
      samples.forEach((sample) => {
        const dx = sample.clientX - this.lastPointer[0];
        const dy = sample.clientY - this.lastPointer[1];
        this.lastPointer = [sample.clientX, sample.clientY];

        // cameraMvp() applies screenRoll after constructing the camera. Undo
        // that roll for pointer deltas so a horizontal/vertical screen drag
        // remains horizontal/vertical to the user. With the fixed +90 degree
        // roll this is equivalent to orbitDx=dy and orbitDy=-dx.
        const cosine = Math.cos(this.screenRoll);
        const sine = Math.sin(this.screenRoll);
        const orbitDx = dx * cosine + dy * sine;
        const orbitDy = -dx * sine + dy * cosine;
        const precision = event.shiftKey ? 0.25 : 1;
        const sensitivity = this.orbitRadiansPerPixel * precision;
        this.azimuth += orbitDx * sensitivity;
        this.elevation = Math.max(-1.2, Math.min(1.2, this.elevation - orbitDy * sensitivity));
      });
      this.requestDraw();
    });
    const endDrag = () => {
      const changed = this.dragging;
      this.dragging = false;
      this.lastPointer = null;
      this.canvas.classList.remove("dragging");
      if (changed) {
        this.persistViewState();
        this.scheduleServerViewPersist();
      }
    };
    this.canvas.addEventListener("pointerup", endDrag);
    this.canvas.addEventListener("pointercancel", endDrag);
    this.canvas.addEventListener("lostpointercapture", endDrag);
    this.canvas.addEventListener("wheel", (event) => {
      if (this.viewLocked) return;
      event.preventDefault();
      this.distance = Math.max(1.1, Math.min(12, this.distance * Math.exp(event.deltaY * 0.001)));
      this.persistViewState();
      this.scheduleServerViewPersist();
      this.requestDraw();
    }, { passive: false });
  }

  ingest(states) {
    let changed = false;
    const activeKeys = new Set();
    states.forEach((item, index) => {
      const sourceRole = String(item.role || "").toLowerCase();
      if (this.visibleRoles.size && !this.visibleRoles.has(sourceRole)) return;
      const position = item.end_effector_position;
      const rotation = item.end_effector_rotation;
      if (!Array.isArray(position) || position.length < 3 || !position.every(Number.isFinite)) return;
      if (Number(item.age_ms ?? 999999) > 1500) return;
      if (Number(item.tracking_status) !== 2) return;

      // A reconnect may assign a different dongle MAC/slot to the same arm.
      // Keep exactly one visualization per logical role and discard the old
      // device history when its physical identity changes.
      const key = sourceRole || String(item.tracker_index ?? item.mac ?? index);
      activeKeys.add(key);
      const deviceMac = String(item.mac || "");
      const existingTrack = this.tracks.get(key);
      if (existingTrack && existingTrack.mac !== deviceMac) {
        existingTrack.points = [];
        existingTrack.currentPoint = null;
        existingTrack.rotation = null;
        this.lastTimestamp.delete(key);
        changed = true;
      }
      const timestamp = String(item.last_host_unix_time_ns || item.tracker_timestamp_ms || "");
      if (timestamp && this.lastTimestamp.get(key) === timestamp) return;
      this.lastTimestamp.set(key, timestamp);
      if (!this.tracks.has(key)) {
        const side = armSide(item, this.tracks.size);
        this.tracks.set(key, {
          points: [],
          currentPoint: null,
          color: armColor(side, this.tracks.size),
          side,
          eefLabel: side === "right" ? "Right" : side === "left" ? "Left" : "EEF",
          role: displayRole(item, this.tracks.size),
          mac: item.mac || "",
          status: Number(item.tracking_status),
          rotation: null,
          calibrated: Boolean(item.end_effector_position),
          sourceRole,
        });
      }
      const track = this.tracks.get(key);
      track.side = armSide(item, index);
      track.color = armColor(track.side, index);
      track.eefLabel = track.side === "right" ? "Right" : track.side === "left" ? "Left" : "EEF";
      track.role = displayRole(item, index);
      track.mac = deviceMac;
      track.sourceRole = sourceRole;
      track.status = Number(item.tracking_status);
      track.calibrated = Boolean(item.end_effector_position);
      track.rotation = Array.isArray(rotation) && rotation.length >= 4 && rotation.every(Number.isFinite)
        ? this.normalizedWxyz(rotation.slice(0, 4).map(Number))
        : null;
      track.currentPoint = position.slice(0, 3).map(Number);
      if (this.historyActive) {
        track.points.push([...track.currentPoint]);
        if (track.points.length > this.maxPoints) track.points.splice(0, track.points.length - this.maxPoints);
      }
      changed = true;
    });

    // Do not render disconnected, stale, tracking-lost, or replaced devices.
    this.tracks.forEach((_track, key) => {
      if (!activeKeys.has(key)) {
        this.tracks.delete(key);
        this.lastTimestamp.delete(key);
        changed = true;
      }
    });
    if (changed) {
      this.updateLegend();
      this.requestDraw();
    }
    this.empty.hidden = [...this.tracks.values()].some((track) => Array.isArray(track.currentPoint));
  }

  totalPoints() {
    let total = 0;
    this.tracks.forEach((track) => { total += track.points.length; });
    return total;
  }

  updateLegend() {
    this.legend.innerHTML = [...this.tracks.values()].map((track) => {
      const point = track.currentPoint || [0, 0, 0];
      const poseLabel = track.calibrated ? "HTC → End effector" : "Raw HTC";
      return `<div class="track-legend-item"><i style="--track-color:${track.color}"></i><div><strong>${escapeHtml(track.role)}</strong><span>${escapeHtml(shortAddress(track.mac))} · ${poseLabel}</span><code>_05 EEF X ${point[0].toFixed(3)} · Y ${point[1].toFixed(3)} · Z ${point[2].toFixed(3)} m</code><code>WebGL 3D · camera roll CCW 90° · values unchanged</code></div><b class="${track.status === 2 ? "ready" : "lost"}">${track.status === 2 ? "Ready" : "Tracking lost"}</b></div>`;
    }).join("");
  }

  resetView() {
    this.azimuth = 0;
    this.elevation = 0.32;
    this.distance = 3.4;
    this.screenRoll = Math.PI / 2;
    this.viewLocked = false;
    this.persistViewState();
    this.persistViewStateToServer();
    this.updateViewControls();
    this.requestDraw();
  }

  clear() {
    this.tracks.clear();
    this.lastTimestamp.clear();
    this.legend.innerHTML = "";
    if (this.labelLayer) this.labelLayer.innerHTML = "";
    this.empty.hidden = false;
    this.view = null;
    this.requestDraw();
  }

  requestDraw() {
    if (this.drawPending || document.hidden) return;
    this.drawPending = true;
    requestAnimationFrame(() => {
      this.drawPending = false;
      this.draw();
    });
  }

  rotateVectorByWxyz(vector, quaternion) {
    const [w, x, y, z] = quaternion;
    const norm = Math.hypot(w, x, y, z);
    if (!Number.isFinite(norm) || norm < 1e-9) return vector;
    const qw = w / norm;
    const qx = x / norm;
    const qy = y / norm;
    const qz = z / norm;
    const [vx, vy, vz] = vector;
    return [
      (1 - 2 * (qy * qy + qz * qz)) * vx + 2 * (qx * qy - qz * qw) * vy + 2 * (qx * qz + qy * qw) * vz,
      2 * (qx * qy + qz * qw) * vx + (1 - 2 * (qx * qx + qz * qz)) * vy + 2 * (qy * qz - qx * qw) * vz,
      2 * (qx * qz - qy * qw) * vx + 2 * (qy * qz + qx * qw) * vy + (1 - 2 * (qx * qx + qy * qy)) * vz,
    ];
  }

  normalizedWxyz(quaternion) {
    const norm = Math.hypot(...quaternion);
    return Number.isFinite(norm) && norm > 1e-9
      ? quaternion.map((value) => value / norm)
      : [1, 0, 0, 0];
  }

  mat4Multiply(left, right) {
    const out = new Float32Array(16);
    for (let column = 0; column < 4; column += 1) {
      for (let row = 0; row < 4; row += 1) {
        out[column * 4 + row] =
          left[row] * right[column * 4]
          + left[4 + row] * right[column * 4 + 1]
          + left[8 + row] * right[column * 4 + 2]
          + left[12 + row] * right[column * 4 + 3];
      }
    }
    return out;
  }

  mat4Perspective(fieldOfViewRadians, aspect, near, far) {
    const f = 1 / Math.tan(fieldOfViewRadians / 2);
    const inverseRange = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * inverseRange, -1,
      0, 0, 2 * far * near * inverseRange, 0,
    ]);
  }

  normalized3(vector) {
    const length = Math.hypot(vector[0], vector[1], vector[2]);
    if (!Number.isFinite(length) || length < 1e-9) return [0, 0, 0];
    return vector.map((value) => value / length);
  }

  cross3(a, b) {
    return [
      a[1] * b[2] - a[2] * b[1],
      a[2] * b[0] - a[0] * b[2],
      a[0] * b[1] - a[1] * b[0],
    ];
  }

  dot3(a, b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  }

  mat4LookAt(eye, target, up) {
    const backward = this.normalized3(eye.map((value, index) => value - target[index]));
    const right = this.normalized3(this.cross3(up, backward));
    const cameraUp = this.cross3(backward, right);
    return new Float32Array([
      right[0], cameraUp[0], backward[0], 0,
      right[1], cameraUp[1], backward[1], 0,
      right[2], cameraUp[2], backward[2], 0,
      -this.dot3(right, eye), -this.dot3(cameraUp, eye), -this.dot3(backward, eye), 1,
    ]);
  }

  mat4RotationZ(angle) {
    const cosine = Math.cos(angle);
    const sine = Math.sin(angle);
    return new Float32Array([
      cosine, sine, 0, 0,
      -sine, cosine, 0, 0,
      0, 0, 1, 0,
      0, 0, 0, 1,
    ]);
  }

  cameraMvp(width, height) {
    const target = this.fixedView.center;
    const horizontal = Math.cos(this.elevation) * this.distance;
    const eye = [
      target[0] + horizontal * Math.cos(this.azimuth),
      target[1] + horizontal * Math.sin(this.azimuth),
      target[2] - Math.sin(this.elevation) * this.distance,
    ];
    const view = this.mat4LookAt(eye, target, [0, 0, -1]);
    const rolledView = this.mat4Multiply(this.mat4RotationZ(this.screenRoll), view);
    const projection = this.mat4Perspective(48 * Math.PI / 180, width / height, 0.02, 50);
    return this.mat4Multiply(projection, rolledView);
  }

  projectToScreen(point, mvp, width, height) {
    const vector = [point[0], point[1], point[2], 1];
    const clip = [0, 0, 0, 0];
    for (let row = 0; row < 4; row += 1) {
      for (let column = 0; column < 4; column += 1) {
        clip[row] += mvp[column * 4 + row] * vector[column];
      }
    }
    if (!Number.isFinite(clip[3]) || clip[3] <= 1e-6) return null;
    const ndc = clip.slice(0, 3).map((value) => value / clip[3]);
    if (
      ndc[0] < -1.05 || ndc[0] > 1.05
      || ndc[1] < -1.05 || ndc[1] > 1.05
      || ndc[2] < -1 || ndc[2] > 1
    ) return null;
    return [
      (ndc[0] * 0.5 + 0.5) * width,
      (1 - (ndc[1] * 0.5 + 0.5)) * height,
      ndc[2],
    ];
  }

  colorRgba(color, alpha = 1) {
    const match = /^#([0-9a-f]{6})$/i.exec(String(color));
    if (!match) return [1, 1, 1, alpha];
    const value = Number.parseInt(match[1], 16);
    return [
      ((value >> 16) & 255) / 255,
      ((value >> 8) & 255) / 255,
      (value & 255) / 255,
      alpha,
    ];
  }

  repeatedColors(count, color) {
    const output = new Float32Array(count * 4);
    for (let index = 0; index < count; index += 1) output.set(color, index * 4);
    return output;
  }

  renderPrimitive(mode, positions, colors, mvp, { pointSize = 1, lineWidth = 1 } = {}) {
    const gl = this.gl;
    if (!positions.length) return;
    const positionArray = positions instanceof Float32Array ? positions : new Float32Array(positions);
    const vertexCount = positionArray.length / 3;
    const colorArray = colors instanceof Float32Array
      ? colors
      : this.repeatedColors(vertexCount, colors);
    gl.useProgram(this.program);
    gl.uniformMatrix4fv(this.locations.mvp, false, mvp);
    gl.uniform1f(this.locations.pointSize, pointSize);
    gl.uniform1f(this.locations.roundPoints, mode === gl.POINTS ? 1 : 0);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.positionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, positionArray, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.locations.position);
    gl.vertexAttribPointer(this.locations.position, 3, gl.FLOAT, false, 0, 0);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.colorBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, colorArray, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.locations.color);
    gl.vertexAttribPointer(this.locations.color, 4, gl.FLOAT, false, 0, 0);

    gl.lineWidth(lineWidth);
    gl.drawArrays(mode, 0, vertexCount);
  }

  appendSegment(positions, colors, start, end, color) {
    positions.push(...start, ...end);
    colors.push(...color, ...color);
  }

  poseAxesGeometry(origin, rotation, length) {
    if (!rotation) return { positions: [], colors: [] };
    const positions = [];
    const colors = [];
    [
      { vector: [length, 0, 0], color: [1, 0.31, 0.31, 1] },
      { vector: [0, length, 0], color: [0.16, 0.86, 0.52, 1] },
      { vector: [0, 0, length], color: [0.25, 0.57, 1, 1] },
    ].forEach((axis) => {
      const direction = this.rotateVectorByWxyz(axis.vector, rotation);
      const end = origin.map((value, index) => value + direction[index]);
      this.appendSegment(positions, colors, origin, end, axis.color);
    });
    return { positions, colors: new Float32Array(colors) };
  }

  sampleLineSegments(positions, colors, samplesPerSegment = 24) {
    const sampledPositions = [];
    const sampledColors = [];
    const segmentCount = Math.floor(positions.length / 6);
    for (let segment = 0; segment < segmentCount; segment += 1) {
      const positionOffset = segment * 6;
      const colorOffset = segment * 8;
      for (let sample = 0; sample <= samplesPerSegment; sample += 1) {
        const ratio = sample / samplesPerSegment;
        for (let axis = 0; axis < 3; axis += 1) {
          const start = positions[positionOffset + axis];
          const end = positions[positionOffset + 3 + axis];
          sampledPositions.push(start + (end - start) * ratio);
        }
        for (let channel = 0; channel < 4; channel += 1) {
          const start = colors[colorOffset + channel];
          const end = colors[colorOffset + 4 + channel];
          sampledColors.push(start + (end - start) * ratio);
        }
      }
    }
    return {
      positions: new Float32Array(sampledPositions),
      colors: new Float32Array(sampledColors),
    };
  }

  drawGrid(mvp) {
    const gl = this.gl;
    const view = this.fixedView;
    const gridRange = 1.5;
    const gridStep = 0.25;
    const positions = [];
    const colors = [];
    const floorColor = [0.39, 0.52, 0.71, 0.24];
    const wallColor = [0.33, 0.44, 0.61, 0.13];
    for (let value = -gridRange; value <= gridRange + 1e-6; value += gridStep) {
      this.appendSegment(
        positions,
        colors,
        [value, -gridRange, view.groundZ],
        [value, gridRange, view.groundZ],
        floorColor,
      );
      this.appendSegment(
        positions,
        colors,
        [-gridRange, value, view.groundZ],
        [gridRange, value, view.groundZ],
        floorColor,
      );
    }
    const wallTopZ = view.groundZ - 1.5;
    for (let z = wallTopZ; z <= view.groundZ + 1e-6; z += gridStep) {
      this.appendSegment(positions, colors, [-gridRange, gridRange, z], [gridRange, gridRange, z], wallColor);
      this.appendSegment(positions, colors, [-gridRange, -gridRange, z], [-gridRange, gridRange, z], wallColor);
    }
    for (let value = -gridRange; value <= gridRange + 1e-6; value += gridStep) {
      this.appendSegment(
        positions,
        colors,
        [value, gridRange, view.groundZ],
        [value, gridRange, wallTopZ],
        wallColor,
      );
      this.appendSegment(
        positions,
        colors,
        [-gridRange, value, view.groundZ],
        [-gridRange, value, wallTopZ],
        wallColor,
      );
    }
    this.renderPrimitive(gl.LINES, positions, new Float32Array(colors), mvp);
  }

  drawWorldAxes(mvp) {
    const positions = [];
    const colors = [];
    const origin = [0, 0, 0];
    this.appendSegment(positions, colors, origin, [0.7, 0, 0], [1, 0.31, 0.31, 1]);
    this.appendSegment(positions, colors, origin, [0, 0.7, 0], [0.16, 0.86, 0.52, 1]);
    this.appendSegment(positions, colors, origin, [0, 0, 0.7], [0.25, 0.57, 1, 1]);
    const axisColors = new Float32Array(colors);
    this.renderPrimitive(this.gl.LINES, positions, axisColors, mvp, { lineWidth: 3 });
    this.renderPrimitive(this.gl.POINTS, positions, axisColors, mvp, { pointSize: 6 });
    this.renderPrimitive(this.gl.POINTS, origin, [1, 1, 1, 1], mvp, { pointSize: 7 });
  }

  drawTrack(track, mvp) {
    const gl = this.gl;
    const currentPoint = track.currentPoint;
    if (!currentPoint) return;
    const trackColor = this.colorRgba(track.color, 0.96);
    if (this.historyActive && track.points.length >= 2) {
      const trajectory = new Float32Array(track.points.flat());
      this.renderPrimitive(gl.LINE_STRIP, trajectory, trackColor, mvp, { lineWidth: 3 });
      this.renderPrimitive(
        gl.POINTS,
        trajectory,
        this.colorRgba(track.color, 0.42),
        mvp,
        { pointSize: 3.5 },
      );
      this.renderPrimitive(gl.POINTS, track.points[0], trackColor, mvp, { pointSize: 11 });
      this.renderPrimitive(gl.POINTS, track.points[0], [0.04, 0.07, 0.11, 1], mvp, { pointSize: 5 });
    }

    const axes = this.poseAxesGeometry(currentPoint, track.rotation, 0.13);
    this.renderPrimitive(gl.LINES, axes.positions, axes.colors, mvp, { lineWidth: 4 });
    // WebGL 1 implementations commonly clamp lineWidth to 1 px. Dense round
    // samples make the EEF direction axes reliably thick on every browser.
    const thickAxes = this.sampleLineSegments(axes.positions, axes.colors);
    this.renderPrimitive(gl.POINTS, thickAxes.positions, thickAxes.colors, mvp, { pointSize: 8 });
    this.renderPrimitive(
      gl.POINTS,
      currentPoint,
      track.status === 2 ? trackColor : [0.85, 0.24, 0.24, 1],
      mvp,
      { pointSize: 18 },
    );
    this.renderPrimitive(gl.POINTS, currentPoint, [1, 1, 1, 1], mvp, { pointSize: 10 });
  }

  updateTrackLabels(mvp, width, height) {
    if (!this.labelLayer) return;
    const labels = [];
    this.tracks.forEach((track) => {
      if (!track.currentPoint) return;
      const screen = this.projectToScreen(track.currentPoint, mvp, width, height);
      if (!screen) return;
      const side = track.side === "right" ? "right" : track.side === "left" ? "left" : "unknown";
      const label = side === "right" ? "RIGHT" : side === "left" ? "LEFT" : "EEF";
      labels.push(
        `<div class="trajectory-hand-label ${side}" style="left:${screen[0].toFixed(1)}px;top:${screen[1].toFixed(1)}px;--track-color:${track.color}"><i></i>${label}</div>`,
      );
    });
    this.labelLayer.innerHTML = labels.join("");
  }

  draw() {
    if (!this.gl) {
      this.empty.hidden = false;
      const strong = this.empty.querySelector("strong");
      const detail = this.empty.querySelector("span");
      if (strong) strong.textContent = "WebGL 3D unavailable";
      if (detail) detail.textContent = this.webglError || "Enable hardware acceleration and reload the page";
      return;
    }
    const gl = this.gl;
    const rect = this.canvas.getBoundingClientRect();
    const cssWidth = Math.max(1, rect.width);
    const cssHeight = Math.max(1, rect.height);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(cssWidth * dpr));
    const height = Math.max(1, Math.round(cssHeight * dpr));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    gl.viewport(0, 0, width, height);
    gl.clearColor(0.035, 0.059, 0.094, 1);
    gl.clearDepth(1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const mvp = this.cameraMvp(cssWidth, cssHeight);
    this.drawGrid(mvp);
    this.drawWorldAxes(mvp);
    this.tracks.forEach((track) => this.drawTrack(track, mvp));
    this.updateTrackLabels(mvp, cssWidth, cssHeight);
    this.updateViewReadout();
  }
}

function activeViewAnnotationState(status = lastRuntimeStatus) {
  return status.active_view_annotations || { items: [], assets: [] };
}

function annotationAssetForRole(state, cameraRole) {
  return (state.assets || []).find((item) => item.camera_role === cameraRole) || null;
}

function drawActiveViewAnnotations(cameraRole = null) {
  const selectedViews = cameraRole
    ? [[cameraRole, annotationViews[cameraRole]]]
    : Object.entries(annotationViews);
  selectedViews.forEach(([_role, view]) => {
    const { image, canvas } = view;
    if (image.hidden || !image.complete || !image.naturalWidth) {
      canvas.width = 1;
      canvas.height = 1;
      return;
    }
    const rect = image.getBoundingClientRect();
    const scale = window.devicePixelRatio || 1;
    canvas.style.left = `${image.offsetLeft}px`;
    canvas.style.top = `${image.offsetTop}px`;
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    canvas.width = Math.max(1, Math.round(rect.width * scale));
    canvas.height = Math.max(1, Math.round(rect.height * scale));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    const assetId = image.dataset.assetId;
    const state = activeViewAnnotationState();
    const points = (state.items || []).filter(
      (item) => item.reference_asset_id === assetId,
    );
    points.forEach((item, index) => {
      const x = Number(item.x_norm) * rect.width;
      const y = Number(item.y_norm) * rect.height;
      ctx.beginPath();
      ctx.arc(x, y, 10, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255, 62, 55, .28)";
      ctx.fill();
      ctx.lineWidth = 3;
      ctx.strokeStyle = "#ff3e37";
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(x, y, 2.8, 0, Math.PI * 2);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.font = "700 12px ui-monospace, monospace";
      ctx.fillText(String(index + 1), x + 13, y - 10);
    });
  });
}

function renderActiveViewAnnotations(status) {
  const state = activeViewAnnotationState(status);
  const offline = state.mode === "offline";
  const offlineState = state.offline || {};
  const captureLocked = Boolean(status.running || status.starting || status.stopping);
  elements.annotationOpenForm.hidden = offline;
  elements.annotationEditor.hidden = !offline;
  elements.offlineAnnotationRoot.disabled = captureLocked || requestBusy;
  elements.openOfflineAnnotation.disabled = captureLocked || requestBusy;
  elements.closeOfflineAnnotation.disabled = captureLocked || requestBusy;
  if (captureLocked) {
    elements.annotationLoaderHint.textContent = "End the current capture before opening annotation data.";
    elements.annotationLoaderHint.dataset.state = "warning";
  } else if (annotationLoaderNotice) {
    elements.annotationLoaderHint.textContent = annotationLoaderNotice.text;
    elements.annotationLoaderHint.dataset.state = annotationLoaderNotice.state;
  } else {
    elements.annotationLoaderHint.textContent = "Existing capture directories work directly. Only transferred files are read; cameras are never connected or scanned.";
    elements.annotationLoaderHint.dataset.state = "idle";
  }

  if (!offline) {
    Object.values(annotationViews).forEach((view) => {
      view.image.hidden = true;
      view.image.removeAttribute("src");
      delete view.image.dataset.assetId;
      view.canvas.width = 1;
      view.canvas.height = 1;
    });
    try {
      if (!elements.offlineAnnotationRoot.value) {
        elements.offlineAnnotationRoot.value = window.localStorage.getItem("instadatacap.annotationWorkspace") || "";
      }
    } catch (_error) {
      // A disabled localStorage must not affect capture or annotation controls.
    }
    return;
  }

  const workspaceRoot = offlineState.workspace_root || "";
  elements.annotationWorkspaceName.textContent = offlineState.task || workspaceRoot.split("/").filter(Boolean).pop() || "Active-view workspace";
  elements.annotationWorkspacePath.textContent = workspaceRoot;
  elements.annotationEpisodeLabel.textContent = `Episode ${offlineState.episode_index ?? "—"}`;
  elements.annotationOfflineProgress.textContent = `${Number(offlineState.episode_position || 0) + 1} / ${offlineState.episode_count || 0} · ${offlineState.reviewed_episode_count || 0} reviewed`;
  elements.annotationPreviousEpisode.disabled = captureLocked || !offlineState.can_previous || requestBusy;
  elements.annotationCompleteEpisode.disabled = captureLocked || requestBusy || (!offlineState.can_next && offlineState.current_episode_reviewed);
  elements.annotationCompleteEpisode.textContent = offlineState.can_next
    ? (offlineState.current_episode_reviewed ? "Confirmed · Next →" : "Confirm and Continue →")
    : (offlineState.current_episode_reviewed ? "Episode Confirmed" : "Confirm and Finish");
  try {
    window.localStorage.setItem("instadatacap.annotationWorkspace", workspaceRoot);
  } catch (_error) {
    // Remembering the path is a convenience only.
  }

  const allItems = state.items || [];
  elements.annotationFrameDetail.textContent = "Same episode · left/right first frames paired by the conversion plan";
  Object.entries(annotationViews).forEach(([cameraRole, view]) => {
    const asset = annotationAssetForRole(state, cameraRole);
    const handName = cameraRole === "insta_left" ? "Left hand" : "Right hand";
    if (asset) {
      view.frame.textContent = `Source frame ${asset.source_frame_index}`;
      const desired = `/api/annotation-frame/${asset.asset_id}`;
      if (view.image.dataset.assetId !== asset.asset_id) {
        delete view.image.dataset.loadFailedAsset;
        view.image.dataset.assetId = asset.asset_id;
        view.image.src = desired;
      }
      const loadFailed = view.image.dataset.loadFailedAsset === asset.asset_id;
      view.image.hidden = loadFailed;
      view.empty.hidden = !loadFailed;
    } else {
      view.frame.textContent = "Frame missing";
      view.image.hidden = true;
      view.image.removeAttribute("src");
      delete view.image.dataset.assetId;
      view.empty.hidden = false;
    }
    const visibleItems = asset
      ? allItems.filter((item) => item.reference_asset_id === asset.asset_id)
      : [];
    view.count.textContent = `${handName}: ${visibleItems.length} target${visibleItems.length === 1 ? "" : "s"}`;
    view.detail.textContent = asset
      ? "Click a target to add a red point; click the point again to remove it."
      : `${handName} panorama is missing.`;
    view.clear.disabled = captureLocked || visibleItems.length === 0 || requestBusy;
  });
  const pairedViewsReady = Object.entries(annotationViews).every(([cameraRole, view]) => {
    const asset = annotationAssetForRole(state, cameraRole);
    return asset && view.image.dataset.loadFailedAsset !== asset.asset_id;
  });
  if (!pairedViewsReady) elements.annotationCompleteEpisode.disabled = true;
  const totalCount = Number(offlineState.target_instance_count || 0);
  elements.annotationCount.textContent = `${totalCount} target${totalCount === 1 ? "" : "s"} across both hands`;
  elements.annotationDetail.textContent = offlineState.current_episode_reviewed
    ? "This episode is confirmed; annotations for both hands were saved automatically."
    : "Review both hand panoramas separately. Either side may be left empty if no target is visible.";
  window.requestAnimationFrame(() => drawActiveViewAnnotations());
}

function setGripperPreviewNotice(state, text) {
  gripperPreviewNotice = { state, text };
  elements.gripperPreviewStatus.textContent = text;
  elements.gripperPreviewStatus.dataset.state = state;
}

function gripperPreviewCaptureLocked(status = lastRuntimeStatus) {
  return Boolean(status.running || status.starting || status.stopping);
}

function gripperEpisodePosition() {
  if (!gripperPreviewPayload) return -1;
  return (gripperPreviewPayload.episode_indices || []).indexOf(
    Number(gripperPreviewPayload.episode_index),
  );
}

function renderGripperPreviewAvailability(status = lastRuntimeStatus) {
  const configuredThresholdM = Number(
    status.gripper_binary_preview?.configured_threshold_m,
  );
  if (
    !gripperThresholdInitialized
    && Number.isFinite(configuredThresholdM)
    && configuredThresholdM > 0
  ) {
    setGripperThresholdMm(configuredThresholdM * 1000);
    gripperThresholdInitialized = true;
  }
  const captureLocked = gripperPreviewCaptureLocked(status);
  const locked = captureLocked || gripperPreviewBusy || gripperPreviewEstimateBusy || gripperPreviewDiscoveryBusy;
  elements.gripperPreviewRoot.disabled = locked;
  elements.gripperPreviewCapture.disabled = locked || elements.gripperPreviewCapture.options.length <= 1;
  elements.gripperPreviewEpisode.disabled = locked || !elements.gripperPreviewCapture.value;

  const position = gripperEpisodePosition();
  const indices = gripperPreviewPayload?.episode_indices || [];
  elements.gripperPreviewPrevious.disabled = locked || position <= 0;
  elements.gripperPreviewNext.disabled = locked || position < 0 || position >= indices.length - 1;

  if (captureLocked) {
    elements.gripperPreviewStatus.textContent = "End the active capture before using the read-only gripper post-processing preview. Any chart already loaded remains visible locally.";
    elements.gripperPreviewStatus.dataset.state = "warning";
  } else if (gripperPreviewNotice) {
    elements.gripperPreviewStatus.textContent = gripperPreviewNotice.text;
    elements.gripperPreviewStatus.dataset.state = gripperPreviewNotice.state;
  }
}

function selectedGripperThresholdMm() {
  const numberValue = Number(elements.gripperThresholdMm.value);
  if (Number.isFinite(numberValue)) return Math.max(0, Math.min(100, numberValue));
  const rangeValue = Number(elements.gripperThresholdRange.value);
  return Number.isFinite(rangeValue) ? Math.max(0, Math.min(100, rangeValue)) : 20;
}

function setGripperThresholdMm(value, { preserveNumberText = false } = {}) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return;
  const thresholdMm = Math.max(0, Math.min(100, numeric));
  elements.gripperThresholdRange.value = String(thresholdMm);
  if (!preserveNumberText) elements.gripperThresholdMm.value = thresholdMm.toFixed(1);
  elements.gripperThresholdLabel.textContent = `${thresholdMm.toFixed(1)} mm`;
  const thresholdM = thresholdMm / 1000;
  elements.gripperThresholdCommand.textContent = `${thresholdMm.toFixed(1)} mm = ${thresholdM.toFixed(4)} m · Zarr binary export override: GRIPPER_BINARY_THRESHOLD_M=${thresholdM.toFixed(4)}`;
  try {
    window.localStorage.setItem(GRIPPER_PREVIEW_THRESHOLD_STORAGE_KEY, String(thresholdMm));
  } catch (_error) {
    // Remembering a display threshold is optional and never affects capture.
  }
  scheduleGripperPreviewDraw();
}

function selectedGripperSeries(channel) {
  return channel?.series?.[elements.gripperPreviewSource.value] || [];
}

function finiteGripperValue(value) {
  if (value === null || value === undefined || value === "") return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function gripperSeriesExtent(channels, scale = 1) {
  let minimum = Infinity;
  let maximum = -Infinity;
  let count = 0;
  channels.forEach((channel) => {
    selectedGripperSeries(channel).forEach((rawValue) => {
      const value = finiteGripperValue(rawValue);
      if (value === null) return;
      const scaled = value * scale;
      minimum = Math.min(minimum, scaled);
      maximum = Math.max(maximum, scaled);
      count += 1;
    });
  });
  return { minimum, maximum, count };
}

function prepareGripperCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const cssWidth = Math.max(300, rect.width || 300);
  const cssHeight = Math.max(220, rect.height || 300);
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.round(cssWidth * dpr);
  const height = Math.round(cssHeight * dpr);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, cssWidth, cssHeight);
  context.fillStyle = "#fbfbf8";
  context.fillRect(0, 0, cssWidth, cssHeight);
  return { context, width: cssWidth, height: cssHeight };
}

function drawGripperChartGrid(context, width, height, frameCount, fps, yTicks) {
  const plot = { left: 53, right: width - 14, top: 14, bottom: height - 32 };
  const plotWidth = Math.max(1, plot.right - plot.left);
  const plotHeight = Math.max(1, plot.bottom - plot.top);
  context.lineWidth = 1;
  context.font = "11px ui-monospace, SFMono-Regular, monospace";
  context.textBaseline = "middle";
  context.textAlign = "right";
  yTicks.forEach(({ value, label, ratio }) => {
    const y = plot.bottom - ratio * plotHeight;
    context.beginPath();
    context.moveTo(plot.left, y);
    context.lineTo(plot.right, y);
    context.strokeStyle = "rgba(26,26,26,.11)";
    context.stroke();
    context.fillStyle = "rgba(26,26,26,.62)";
    context.fillText(label ?? String(value), plot.left - 7, y);
  });
  context.textAlign = "center";
  context.textBaseline = "top";
  [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
    const x = plot.left + ratio * plotWidth;
    context.beginPath();
    context.moveTo(x, plot.top);
    context.lineTo(x, plot.bottom);
    context.strokeStyle = "rgba(26,26,26,.07)";
    context.stroke();
    const frame = Math.max(0, Math.round((Math.max(1, frameCount) - 1) * ratio));
    const label = fps > 0 ? `${(frame / fps).toFixed(1)}s` : `#${frame}`;
    context.fillStyle = "rgba(26,26,26,.58)";
    context.fillText(label, x, plot.bottom + 7);
  });
  context.strokeStyle = "rgba(26,26,26,.35)";
  context.strokeRect(plot.left, plot.top, plotWidth, plotHeight);
  return { ...plot, width: plotWidth, height: plotHeight };
}

function regularSampleIndices(length, pointBudget) {
  if (length <= 0) return [];
  const stride = Math.max(1, Math.ceil(length / Math.max(2, pointBudget)));
  const indices = [];
  for (let index = 0; index < length; index += stride) indices.push(index);
  if (indices.at(-1) !== length - 1) indices.push(length - 1);
  return indices;
}

function continuousSampleIndices(values, pointBudget) {
  if (!values.length) return [];
  if (values.length <= pointBudget) return values.map((_value, index) => index);
  const bucketCount = Math.max(1, Math.floor(pointBudget / 2));
  const indices = new Set([0, values.length - 1]);
  for (let bucket = 0; bucket < bucketCount; bucket += 1) {
    const start = Math.floor(bucket * values.length / bucketCount);
    const end = Math.min(values.length, Math.floor((bucket + 1) * values.length / bucketCount));
    let minimum = Infinity;
    let maximum = -Infinity;
    let minimumIndex = -1;
    let maximumIndex = -1;
    for (let index = start; index < end; index += 1) {
      const value = finiteGripperValue(values[index]);
      if (value === null) continue;
      if (value < minimum) {
        minimum = value;
        minimumIndex = index;
      }
      if (value > maximum) {
        maximum = value;
        maximumIndex = index;
      }
    }
    if (minimumIndex >= 0) indices.add(minimumIndex);
    if (maximumIndex >= 0) indices.add(maximumIndex);
  }
  return [...indices].sort((a, b) => a - b);
}

function binarySampleIndices(values, thresholdM, pointBudget) {
  if (!values.length) return [];
  if (values.length <= pointBudget) return values.map((_value, index) => index);
  const indices = new Set(regularSampleIndices(values.length, Math.floor(pointBudget / 2)));
  let previous = null;
  values.forEach((value, index) => {
    const numeric = finiteGripperValue(value);
    const binary = numeric === null ? null : (numeric < thresholdM ? 0 : 1);
    if (index > 0 && binary !== previous) {
      indices.add(index - 1);
      indices.add(index);
    }
    previous = binary;
  });
  // Keep every threshold transition even for a noisy episode. Falling back to
  // uniform sampling here would make a short 0/1 state change disappear.
  return [...indices].sort((a, b) => a - b);
}

function drawContinuousGripperChart() {
  const payload = gripperPreviewPayload;
  if (!payload) return;
  const { context, width, height } = prepareGripperCanvas(elements.gripperContinuousCanvas);
  const channels = payload.channels || [];
  const thresholdMm = selectedGripperThresholdMm();
  const extent = gripperSeriesExtent(channels, 1000);
  if (!extent.count) {
    context.fillStyle = "rgba(26,26,26,.58)";
    context.font = "14px sans-serif";
    context.textAlign = "center";
    context.fillText("No finite gripper values", width / 2, height / 2);
    return;
  }
  const dataMin = extent.minimum;
  const dataMax = extent.maximum;
  const yMin = dataMin < 0 ? Math.floor(dataMin / 10) * 10 : 0;
  const yMax = Math.max(yMin + 10, Math.ceil(Math.max(dataMax, thresholdMm) / 10) * 10);
  const frameCount = Math.max(1, ...channels.map((channel) => selectedGripperSeries(channel).length));
  const yTicks = Array.from({ length: 6 }, (_unused, index) => {
    const ratio = index / 5;
    const value = yMin + ratio * (yMax - yMin);
    return { value, ratio, label: `${value.toFixed(0)} mm` };
  });
  const plot = drawGripperChartGrid(context, width, height, frameCount, Number(payload.fps || 0), yTicks);
  const xFor = (index) => plot.left + (index / Math.max(1, frameCount - 1)) * plot.width;
  const yFor = (valueMm) => plot.bottom - ((valueMm - yMin) / (yMax - yMin)) * plot.height;

  if (thresholdMm >= yMin && thresholdMm <= yMax) {
    const thresholdY = yFor(thresholdMm);
    context.save();
    context.setLineDash([7, 5]);
    context.strokeStyle = "#914621";
    context.lineWidth = 1.5;
    context.beginPath();
    context.moveTo(plot.left, thresholdY);
    context.lineTo(plot.right, thresholdY);
    context.stroke();
    context.restore();
  }

  channels.forEach((channel) => {
    const values = selectedGripperSeries(channel);
    // Pixel-aware min/max decimation covers the full episode while preserving
    // short peaks that a regular stride could silently skip.
    const indices = continuousSampleIndices(values, Math.max(400, Math.round(plot.width * 2)));
    context.beginPath();
    let drawing = false;
    indices.forEach((index) => {
      const value = finiteGripperValue(values[index]);
      if (value === null) {
        drawing = false;
        return;
      }
      const x = xFor(index);
      const y = yFor(value * 1000);
      if (drawing) context.lineTo(x, y);
      else context.moveTo(x, y);
      drawing = true;
    });
    context.strokeStyle = channel.color || "#1a1a1a";
    context.lineWidth = 2;
    context.lineJoin = "round";
    context.lineCap = "round";
    context.stroke();
  });
}

function drawBinaryGripperChart() {
  const payload = gripperPreviewPayload;
  if (!payload) return;
  const { context, width, height } = prepareGripperCanvas(elements.gripperBinaryCanvas);
  const channels = payload.channels || [];
  const thresholdM = selectedGripperThresholdMm() / 1000;
  const frameCount = Math.max(1, ...channels.map((channel) => selectedGripperSeries(channel).length));
  const plot = drawGripperChartGrid(
    context,
    width,
    height,
    frameCount,
    Number(payload.fps || 0),
    [
      { value: 0, ratio: 0.08, label: "0" },
      { value: 1, ratio: 0.92, label: "1" },
    ],
  );
  const xFor = (index) => plot.left + (index / Math.max(1, frameCount - 1)) * plot.width;
  const yFor = (value) => plot.bottom - (value ? 0.92 : 0.08) * plot.height;
  channels.forEach((channel) => {
    const values = selectedGripperSeries(channel);
    const indices = binarySampleIndices(values, thresholdM, Math.max(400, Math.round(plot.width * 2)));
    context.beginPath();
    let previousY = null;
    indices.forEach((index) => {
      const value = finiteGripperValue(values[index]);
      if (value === null) {
        previousY = null;
        return;
      }
      const binary = value < thresholdM ? 0 : 1;
      const x = xFor(index);
      const y = yFor(binary);
      if (previousY === null) context.moveTo(x, y);
      else {
        context.lineTo(x, previousY);
        context.lineTo(x, y);
      }
      previousY = y;
    });
    context.strokeStyle = channel.color || "#1a1a1a";
    context.lineWidth = 2.4;
    context.lineJoin = "round";
    context.stroke();
  });
}

function renderGripperPreviewStats() {
  elements.gripperPreviewStats.replaceChildren();
  if (!gripperPreviewPayload) return;
  const thresholdM = selectedGripperThresholdMm() / 1000;
  (gripperPreviewPayload.channels || []).forEach((channel) => {
    let finiteCount = 0;
    let minimum = Infinity;
    let maximum = -Infinity;
    let zeroCount = 0;
    let oneCount = 0;
    let transitions = 0;
    let previous = null;
    selectedGripperSeries(channel).forEach((rawValue) => {
      const value = finiteGripperValue(rawValue);
      if (value === null) return;
      finiteCount += 1;
      minimum = Math.min(minimum, value);
      maximum = Math.max(maximum, value);
      const binary = value < thresholdM ? 0 : 1;
      if (binary === 0) zeroCount += 1;
      else oneCount += 1;
      if (previous !== null && binary !== previous) transitions += 1;
      previous = binary;
    });
    const percent = (count) => finiteCount ? `${count} (${(count * 100 / finiteCount).toFixed(1)}%)` : "—";
    const row = document.createElement("tr");
    const channelCell = document.createElement("td");
    const channelName = document.createElement("span");
    channelName.className = "gripper-preview-channel-name";
    channelName.style.setProperty("--series-color", channel.color || "#1a1a1a");
    channelName.append(document.createElement("i"), document.createTextNode(channel.label || channel.id));
    channelCell.append(channelName);
    const range = finiteCount
      ? `${(minimum * 1000).toFixed(1)}–${(maximum * 1000).toFixed(1)} mm`
      : "—";
    [channelCell, String(finiteCount), range, percent(zeroCount), percent(oneCount), String(transitions)]
      .forEach((value) => {
        if (value instanceof HTMLElement) row.append(value);
        else {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        }
      });
    elements.gripperPreviewStats.append(row);
  });
}

function drawGripperPreview() {
  gripperPreviewDrawFrame = null;
  if (!gripperPreviewPayload || elements.gripperPreviewEditor.hidden) return;
  elements.gripperThresholdLabel.textContent = `${selectedGripperThresholdMm().toFixed(1)} mm`;
  const thresholdM = selectedGripperThresholdMm() / 1000;
  elements.gripperThresholdCommand.textContent = `${selectedGripperThresholdMm().toFixed(1)} mm = ${thresholdM.toFixed(4)} m · Zarr binary export override: GRIPPER_BINARY_THRESHOLD_M=${thresholdM.toFixed(4)}`;
  drawContinuousGripperChart();
  drawBinaryGripperChart();
  renderGripperPreviewStats();
}

function scheduleGripperPreviewDraw() {
  if (gripperPreviewDrawFrame !== null) return;
  gripperPreviewDrawFrame = window.requestAnimationFrame(drawGripperPreview);
}

function renderGripperPreviewPayload({ inputRoot = null } = {}) {
  const payload = gripperPreviewPayload;
  elements.gripperPreviewEditor.hidden = !payload;
  if (!payload) return;
  if (!gripperThresholdInitialized) {
    const configuredThresholdM = Number(payload.configured_binary_threshold_m);
    if (Number.isFinite(configuredThresholdM) && configuredThresholdM > 0) {
      setGripperThresholdMm(configuredThresholdM * 1000);
      gripperThresholdInitialized = true;
    }
  }
  elements.gripperPreviewDatasetName.textContent = payload.dataset_name || "Gripper preview source";
  elements.gripperPreviewDatasetPath.textContent = payload.dataset_root || "—";
  elements.gripperPreviewEpisodeLabel.textContent = `Episode ${payload.episode_index}`;
  const episodeValue = String(payload.episode_index);
  if (![...elements.gripperPreviewEpisode.options].some((option) => option.value === episodeValue)) {
    const option = document.createElement("option");
    option.value = episodeValue;
    option.textContent = `Episode ${episodeValue}`;
    elements.gripperPreviewEpisode.append(option);
  }
  elements.gripperPreviewEpisode.value = episodeValue;
  // A one-episode estimate may live in a generated artifact directory. Keep
  // the raw capture root in the input so the operator can estimate another
  // episode without finding the capture again.
  elements.gripperPreviewRoot.value = inputRoot || payload.dataset_root || elements.gripperPreviewRoot.value;
  const duration = Number(payload.duration_sec);
  const durationText = Number.isFinite(duration) ? `${duration.toFixed(2)} s` : "duration unknown";
  elements.gripperPreviewMeta.textContent = `${payload.frame_count} frames · ${Number(payload.fps || 0).toFixed(3)} Hz · ${durationText}${payload.task ? ` · ${payload.task}` : ""}`;

  const previousSeries = elements.gripperPreviewSource.value;
  const seriesOptions = Array.isArray(payload.series_options) && payload.series_options.length
    ? payload.series_options
    : [{ id: "observation_state", label: "observation.state" }, { id: "action", label: "action" }];
  elements.gripperPreviewSource.replaceChildren();
  seriesOptions.forEach((series) => {
    const option = document.createElement("option");
    option.value = String(series.id || "");
    option.textContent = String(series.label || series.id || "series");
    elements.gripperPreviewSource.append(option);
  });
  if (seriesOptions.some((series) => String(series.id) === previousSeries)) {
    elements.gripperPreviewSource.value = previousSeries;
  }
  elements.gripperPreviewSource.disabled = seriesOptions.length < 2;

  elements.gripperPreviewLegend.replaceChildren();
  (payload.channels || []).forEach((channel) => {
    const item = document.createElement("span");
    const swatch = document.createElement("i");
    swatch.style.setProperty("--series-color", channel.color || "#1a1a1a");
    item.append(swatch, document.createTextNode(channel.label || channel.id));
    elements.gripperPreviewLegend.append(item);
  });
  if (payload.source_mode === "binary") {
    elements.gripperPreviewWarning.hidden = false;
    const qualityWarning = String(payload.source_quality_warning || "").trim();
    elements.gripperPreviewWarning.textContent = `${qualityWarning ? `${qualityWarning} ` : ""}This source already contains 0/1 gripper values. Load an accepted continuous-width Zarr shard or collection to judge a new millimeter threshold meaningfully.`;
  } else {
    elements.gripperPreviewWarning.hidden = true;
    elements.gripperPreviewWarning.textContent = "";
  }
  renderGripperPreviewAvailability();
  scheduleGripperPreviewDraw();
}

function gripperEstimateRecord(payload) {
  const response = payload && typeof payload === "object" ? payload : {};
  const job = response.job && typeof response.job === "object" ? response.job : {};
  const result = job.result && typeof job.result === "object"
    ? job.result
    : (response.result && typeof response.result === "object" ? response.result : {});
  const state = String(job.state ?? job.status ?? response.state ?? response.status ?? "").toLowerCase();
  const jobId = job.job_id ?? job.id ?? response.job_id ?? response.id ?? null;
  const cacheValue = job.cache_hit ?? response.cache_hit ?? job.cached ?? response.cached ?? false;
  const cacheHit = cacheValue === true
    || cacheValue === 1
    || ["true", "hit", "cached"].includes(String(cacheValue).toLowerCase());
  return {
    state,
    jobId: jobId === null || jobId === undefined ? null : String(jobId),
    cacheHit,
    preview: job.preview ?? response.preview ?? result.preview ?? null,
    message: job.message ?? response.message ?? result.message ?? "",
    error: job.error ?? response.error ?? result.error ?? "",
    stage: job.stage ?? response.stage ?? result.stage ?? "",
    elapsedSec: Number(job.elapsed_sec ?? response.elapsed_sec ?? result.elapsed_sec),
    progress: Number(job.progress ?? response.progress ?? result.progress),
    logPath: job.log_path ?? response.log_path ?? result.log_path ?? job.log ?? response.log ?? "",
    captureRoot: job.capture_root ?? response.capture_root ?? result.capture_root ?? null,
  };
}

function gripperEstimateSucceeded(record) {
  return ["complete", "completed", "done", "ready", "success", "succeeded"].includes(record.state)
    || Boolean(record.preview && !record.state);
}

function gripperEstimateFailed(record) {
  return ["cancelled", "canceled", "error", "failed", "failure"].includes(record.state);
}

function gripperEstimateProgressText(record) {
  const lines = [];
  lines.push(gripperPreviewEstimateCacheHit
    ? `Cache hit for raw episode ${gripperPreviewEstimateEpisode}; validating the cached estimate…`
    : `Estimating calibrated gripper width for raw episode ${gripperPreviewEstimateEpisode}…`);
  const details = [];
  if (record.stage) details.push(String(record.stage));
  if (record.message && record.message !== record.stage) details.push(String(record.message));
  if (Number.isFinite(record.progress)) {
    const percent = record.progress <= 1 ? record.progress * 100 : record.progress;
    details.push(`${Math.max(0, Math.min(100, percent)).toFixed(1)}%`);
  }
  if (Number.isFinite(record.elapsedSec)) details.push(`${record.elapsedSec.toFixed(1)} s elapsed`);
  if (details.length) lines.push(details.join(" · "));
  if (record.logPath) lines.push(`Log: ${record.logPath}`);
  return lines.join("\n");
}

function stopGripperEstimatePolling() {
  if (gripperPreviewEstimatePollTimer !== null) {
    window.clearTimeout(gripperPreviewEstimatePollTimer);
    gripperPreviewEstimatePollTimer = null;
  }
}

function finishGripperEstimateWithError(message, generation) {
  if (generation !== gripperPreviewEstimatePollGeneration) return;
  stopGripperEstimatePolling();
  gripperPreviewEstimateBusy = false;
  gripperPreviewEstimateJobId = null;
  gripperPreviewAutoSelectionKey = "";
  setGripperPreviewNotice("error", message);
  renderGripperPreviewAvailability();
}

async function finishGripperEstimateSuccess(record, generation) {
  if (generation !== gripperPreviewEstimatePollGeneration) return;
  stopGripperEstimatePolling();
  gripperPreviewEstimateCacheHit ||= record.cacheHit;
  const captureRoot = String(record.captureRoot || gripperPreviewEstimateCaptureRoot || "");
  const inputRoot = gripperPreviewDiscoveryInputRoot || captureRoot;
  const episode = gripperPreviewEstimateEpisode;
  const successPrefix = gripperPreviewEstimateCacheHit
    ? "Cache hit: the existing one-episode estimate was reused and validated."
    : "Raw one-episode estimation completed and was cached.";
  gripperPreviewEstimateBusy = false;
  gripperPreviewEstimateJobId = null;

  if (record.preview) {
    gripperPreviewPayload = record.preview;
    setGripperPreviewNotice(
      "success",
      `${successPrefix} Loaded episode ${Number(record.preview.episode_index ?? episode)} automatically.`,
    );
    try {
      window.localStorage.setItem(GRIPPER_PREVIEW_PATH_STORAGE_KEY, inputRoot);
      window.localStorage.setItem(GRIPPER_PREVIEW_CAPTURE_STORAGE_KEY, captureRoot);
      window.localStorage.setItem(GRIPPER_PREVIEW_EPISODE_STORAGE_KEY, String(episode));
    } catch (_error) {
      // Remembering the raw capture root is optional.
    }
    renderGripperPreviewPayload({ inputRoot });
    renderGripperPreviewAvailability();
    return;
  }

  // The worker may return only a completion marker. Reuse the established
  // read-only endpoint to load its cached per-episode artifact.
  renderGripperPreviewAvailability();
  await loadGripperPreview(episode, {
    preserveInputRoot: inputRoot,
    successPrefix,
  });
}

function selectedGripperCapture() {
  return (gripperPreviewDiscovery?.captures || []).find(
    (capture) => String(capture.path) === elements.gripperPreviewCapture.value,
  ) || null;
}

function populateGripperEpisodes(capture, preferredEpisode = "") {
  elements.gripperPreviewEpisode.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = capture ? "Select an episode" : "Select a capture first";
  elements.gripperPreviewEpisode.append(placeholder);
  (capture?.episode_indices || []).forEach((episode) => {
    const option = document.createElement("option");
    option.value = String(episode);
    option.textContent = `Episode ${episode}`;
    elements.gripperPreviewEpisode.append(option);
  });
  const preferred = String(preferredEpisode ?? "");
  if (preferred && [...elements.gripperPreviewEpisode.options].some((option) => option.value === preferred)) {
    elements.gripperPreviewEpisode.value = preferred;
  }
}

function populateGripperCaptures(discovery, { restoreSelection = false } = {}) {
  elements.gripperPreviewCapture.replaceChildren();
  const captures = Array.isArray(discovery?.captures) ? discovery.captures : [];
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = captures.length === 1 ? "One capture found" : "Select a raw capture";
  elements.gripperPreviewCapture.append(placeholder);
  captures.forEach((capture) => {
    const option = document.createElement("option");
    option.value = String(capture.path);
    option.textContent = `${capture.name} · ${capture.episode_count} episodes`;
    elements.gripperPreviewCapture.append(option);
  });

  let preferredCapture = "";
  let preferredEpisode = "";
  if (restoreSelection) {
    try {
      preferredCapture = window.localStorage.getItem(GRIPPER_PREVIEW_CAPTURE_STORAGE_KEY) || "";
      preferredEpisode = window.localStorage.getItem(GRIPPER_PREVIEW_EPISODE_STORAGE_KEY) || "";
    } catch (_error) {
      // Local storage is optional.
    }
  }
  if (!captures.some((capture) => String(capture.path) === preferredCapture)) {
    preferredCapture = captures.length === 1 ? String(captures[0].path) : "";
    preferredEpisode = "";
  }
  elements.gripperPreviewCapture.value = preferredCapture;
  populateGripperEpisodes(selectedGripperCapture(), preferredEpisode);
}

async function discoverGripperPreviewCaptures({ restoreSelection = false } = {}) {
  if (gripperPreviewEstimateBusy || gripperPreviewCaptureLocked()) return;
  const root = elements.gripperPreviewRoot.value.trim();
  gripperPreviewDiscoveryGeneration += 1;
  const generation = gripperPreviewDiscoveryGeneration;
  gripperPreviewDiscovery = null;
  gripperPreviewDiscoveryInputRoot = root;
  populateGripperCaptures(null);
  if (!root) {
    setGripperPreviewNotice("idle", "Enter a raw capture/parent, audited Zarr shard or collection, legacy LeRobot dataset, or one episode Parquet.");
    renderGripperPreviewAvailability();
    return;
  }

  gripperPreviewDiscoveryBusy = true;
  setGripperPreviewNotice("loading", "Discovering raw captures and complete episodes…");
  renderGripperPreviewAvailability();
  try {
    const response = await fetchJson("/api/gripper-preview-discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root }),
    }, 30000);
    if (generation !== gripperPreviewDiscoveryGeneration) return;
    gripperPreviewDiscovery = response.discovery;
    populateGripperCaptures(gripperPreviewDiscovery, { restoreSelection });
    const captureCount = Number(gripperPreviewDiscovery.capture_count || 0);
    const episodeCount = Number(gripperPreviewDiscovery.episode_count || 0);
    setGripperPreviewNotice(
      "success",
      `Found ${captureCount} raw capture${captureCount === 1 ? "" : "s"} and ${episodeCount} complete episodes. Select an episode; calibrated width estimation starts automatically.`,
    );
    try {
      window.localStorage.setItem(GRIPPER_PREVIEW_PATH_STORAGE_KEY, root);
    } catch (_error) {
      // Remembering the directory is optional.
    }
  } catch (error) {
    if (generation !== gripperPreviewDiscoveryGeneration) return;
    populateGripperCaptures(null);
    // An exact dataset path is not a raw-capture discovery root.  Fall back to
    // the same read-only endpoint used for episode navigation; it understands
    // Zarr shards, zero-copy collections, and legacy LeRobot datasets.
    gripperPreviewDiscoveryBusy = false;
    setGripperPreviewNotice("loading", "No raw-capture index found; trying the path as an audited dataset source…");
    renderGripperPreviewAvailability();
    await loadGripperPreview(undefined, { preserveInputRoot: root });
  } finally {
    if (generation === gripperPreviewDiscoveryGeneration) {
      gripperPreviewDiscoveryBusy = false;
      renderGripperPreviewAvailability();
    }
  }
}

function scheduleGripperPreviewDiscovery() {
  if (gripperPreviewDiscoveryTimer !== null) window.clearTimeout(gripperPreviewDiscoveryTimer);
  gripperPreviewDiscoveryTimer = window.setTimeout(() => {
    gripperPreviewDiscoveryTimer = null;
    discoverGripperPreviewCaptures();
  }, 600);
}

function scheduleGripperEstimatePoll(generation, delayMs = GRIPPER_PREVIEW_ESTIMATE_POLL_MS) {
  stopGripperEstimatePolling();
  gripperPreviewEstimatePollTimer = window.setTimeout(
    () => pollGripperPreviewEstimate(generation),
    delayMs,
  );
}

async function pollGripperPreviewEstimate(generation) {
  if (
    generation !== gripperPreviewEstimatePollGeneration
    || !gripperPreviewEstimateBusy
    || !gripperPreviewEstimateJobId
  ) return;
  try {
    const response = await fetchJson("/api/gripper-preview-estimate/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: gripperPreviewEstimateJobId }),
    }, 30000);
    if (generation !== gripperPreviewEstimatePollGeneration) return;
    gripperPreviewEstimatePollFailures = 0;
    const record = gripperEstimateRecord(response);
    gripperPreviewEstimateCacheHit ||= record.cacheHit;
    if (gripperEstimateFailed(record)) {
      finishGripperEstimateWithError(
        `Raw episode estimation failed: ${record.error || record.message || "the worker returned a failure state"}${record.logPath ? `\nLog: ${record.logPath}` : ""}`,
        generation,
      );
      return;
    }
    if (gripperEstimateSucceeded(record)) {
      await finishGripperEstimateSuccess(record, generation);
      return;
    }
    setGripperPreviewNotice("loading", gripperEstimateProgressText(record));
    renderGripperPreviewAvailability();
    scheduleGripperEstimatePoll(generation);
  } catch (error) {
    if (generation !== gripperPreviewEstimatePollGeneration) return;
    gripperPreviewEstimatePollFailures += 1;
    if (gripperPreviewEstimatePollFailures >= GRIPPER_PREVIEW_ESTIMATE_MAX_POLL_FAILURES) {
      finishGripperEstimateWithError(
        `Unable to monitor the raw estimation job after ${gripperPreviewEstimatePollFailures} attempts: ${error.message || error}. The backend job may still finish and cache the result; select another episode and then select this episode again to reuse it.`,
        generation,
      );
      return;
    }
    setGripperPreviewNotice(
      "warning",
      `Status check failed (${gripperPreviewEstimatePollFailures}/${GRIPPER_PREVIEW_ESTIMATE_MAX_POLL_FAILURES}): ${error.message || error}. Retrying without starting a duplicate job…`,
    );
    renderGripperPreviewAvailability();
    scheduleGripperEstimatePoll(generation, 2000);
  }
}

async function estimateRawGripperPreviewEpisode() {
  if (gripperPreviewBusy || gripperPreviewEstimateBusy || gripperPreviewCaptureLocked()) return;
  const root = elements.gripperPreviewCapture.value.trim();
  if (!root) {
    setGripperPreviewNotice("error", "Select one discovered raw capture first.");
    elements.gripperPreviewCapture.focus();
    return;
  }
  const episodeText = elements.gripperPreviewEpisode.value.trim();
  const episode = Number(episodeText);
  if (episodeText === "" || !Number.isInteger(episode) || episode < 0) {
    setGripperPreviewNotice("error", "Raw estimation requires one non-negative segment / episode number.");
    elements.gripperPreviewEpisode.focus();
    return;
  }

  const selectionKey = `${root}\n${episode}`;
  if (selectionKey === gripperPreviewAutoSelectionKey) return;
  gripperPreviewAutoSelectionKey = selectionKey;

  stopGripperEstimatePolling();
  gripperPreviewEstimatePollGeneration += 1;
  const generation = gripperPreviewEstimatePollGeneration;
  gripperPreviewEstimateBusy = true;
  gripperPreviewEstimateJobId = null;
  gripperPreviewEstimateCaptureRoot = root;
  gripperPreviewEstimateEpisode = episode;
  gripperPreviewEstimateCacheHit = false;
  gripperPreviewEstimatePollFailures = 0;
  setGripperPreviewNotice(
    "loading",
    `Starting calibrated gripper estimation for raw episode ${episode} only…\nNo full dataset or Zarr conversion will run.`,
  );
  renderGripperPreviewAvailability();
  try {
    const response = await fetchJson("/api/gripper-preview-estimate/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, episode }),
    }, 60000);
    if (generation !== gripperPreviewEstimatePollGeneration) return;
    const record = gripperEstimateRecord(response);
    gripperPreviewEstimateCacheHit = record.cacheHit;
    gripperPreviewEstimateCaptureRoot = String(record.captureRoot || root);
    if (gripperEstimateFailed(record)) {
      finishGripperEstimateWithError(
        `Unable to start raw episode estimation: ${record.error || record.message || "the service rejected the job"}`,
        generation,
      );
      return;
    }
    if (gripperEstimateSucceeded(record)) {
      await finishGripperEstimateSuccess(record, generation);
      return;
    }
    if (!record.jobId) {
      finishGripperEstimateWithError(
        "The estimation service did not return a job_id, preview, or completion state; no polling job can be monitored safely.",
        generation,
      );
      return;
    }
    gripperPreviewEstimateJobId = record.jobId;
    setGripperPreviewNotice("loading", gripperEstimateProgressText(record));
    renderGripperPreviewAvailability();
    scheduleGripperEstimatePoll(generation);
  } catch (error) {
    finishGripperEstimateWithError(
      `Unable to start raw episode estimation: ${error.message || error}`,
      generation,
    );
  }
}

async function loadGripperPreview(episodeOverride = undefined, { preserveInputRoot = null, successPrefix = "" } = {}) {
  if (gripperPreviewBusy || gripperPreviewEstimateBusy || gripperPreviewCaptureLocked()) return;
  const root = elements.gripperPreviewCapture.value.trim() || elements.gripperPreviewRoot.value.trim() || gripperPreviewEstimateCaptureRoot;
  if (!root) {
    setGripperPreviewNotice("error", "Enter a capture, _03 preview manifest, Zarr shard/collection, legacy LeRobot dataset, or episode Parquet first.");
    elements.gripperPreviewRoot.focus();
    return;
  }
  let episode = episodeOverride;
  if (episode === undefined) {
    const episodeText = elements.gripperPreviewEpisode.value.trim();
    episode = episodeText === "" ? null : Number(episodeText);
  }
  if (/episode_\d+\.parquet$/i.test(root)) episode = null;
  if (episode !== null && (!Number.isInteger(episode) || episode < 0)) {
    setGripperPreviewNotice("error", "Episode must be a non-negative integer, or leave it blank to load the first episode.");
    elements.gripperPreviewEpisode.focus();
    return;
  }

  gripperPreviewBusy = true;
  setGripperPreviewNotice("loading", "Reading one gripper segment or episode… no files will be changed.");
  renderGripperPreviewAvailability();
  try {
    const response = await fetchJson("/api/gripper-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, episode }),
    }, 60000);
    gripperPreviewPayload = response.preview;
    const loadedEpisode = Number(gripperPreviewPayload.episode_index);
    setGripperPreviewNotice(
      "success",
      `${successPrefix}${successPrefix ? " " : ""}Loaded episode ${loadedEpisode} read-only. Move the threshold to compare the two signals instantly.`,
    );
    try {
      window.localStorage.setItem(
        GRIPPER_PREVIEW_PATH_STORAGE_KEY,
        preserveInputRoot || gripperPreviewPayload.dataset_root || root,
      );
    } catch (_error) {
      // Remembering the path is optional.
    }
    renderGripperPreviewPayload({ inputRoot: preserveInputRoot });
  } catch (error) {
    setGripperPreviewNotice("error", `Unable to load episode: ${error.message || error}`);
  } finally {
    gripperPreviewBusy = false;
    renderGripperPreviewAvailability();
  }
}

function navigateGripperPreview(direction) {
  const position = gripperEpisodePosition();
  const indices = gripperPreviewPayload?.episode_indices || [];
  const targetPosition = position + direction;
  if (targetPosition < 0 || targetPosition >= indices.length) return;
  loadGripperPreview(Number(indices[targetPosition]));
}

async function toggleTargetAtPointer(event, cameraRole) {
  const view = annotationViews[cameraRole];
  const state = activeViewAnnotationState();
  const asset = annotationAssetForRole(state, cameraRole);
  if (!asset || requestBusy || gripperPreviewCaptureLocked() || view.image.hidden) return;
  const rect = view.canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return;
  const xNorm = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  const yNorm = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
  const points = (state.items || []).filter(
    (item) => item.reference_asset_id === asset.asset_id,
  );
  const nearest = points
    .map((item) => ({
      item,
      distance: Math.hypot(
        (Number(item.x_norm) - xNorm) * rect.width,
        (Number(item.y_norm) - yNorm) * rect.height,
      ),
    }))
    .sort((a, b) => a.distance - b.distance)[0];
  if (nearest && nearest.distance <= 18) {
    await runAction("delete_target_annotation", nearest.item.annotation_id);
    return;
  }
  await runAction("add_target_annotation", {
    asset_id: asset.asset_id,
    camera_role: asset.camera_role,
    x_norm: xNorm,
    y_norm: yNorm,
    source_frame_index: asset.source_frame_index,
    reference_width: view.image.naturalWidth,
    reference_height: view.image.naturalHeight,
  });
}

const trajectory = new TrackerTrajectory3D(
  elements.trajectoryCanvas,
  elements.trajectoryEmpty,
  elements.trajectoryLegend,
  elements.trajectoryLabels,
);
elements.resetView.addEventListener("click", () => trajectory.resetView());
elements.lockView.addEventListener("click", () => trajectory.toggleViewLock());
elements.clearTrajectory.addEventListener("click", () => trajectory.clear());
document.addEventListener("visibilitychange", () => { if (!document.hidden) trajectory.requestDraw(); });

async function refresh() {
  if (statusRefreshBusy || document.hidden) return;
  statusRefreshBusy = true;
  try {
    const payload = await fetchJson("/api/status", { cache: "no-store" }, 2000);
    if (payload.ok) render(payload.status);
  } catch (error) {
    elements.message.textContent = `Cannot connect to capture service: ${error}`;
  } finally {
    statusRefreshBusy = false;
  }
}

async function refreshTracker() {
  if (trackerRefreshBusy || document.hidden) return;
  trackerRefreshBusy = true;
  try {
    const payload = await fetchJson("/api/tracker-status", { cache: "no-store" }, 1000);
    if (payload.ok && payload.tracker) {
      renderTracker(payload.tracker, Number(lastRuntimeStatus.expected_trackers || 2));
    }
  } catch (_error) {
    // Main status refresh owns user-visible service errors. Trajectory polling
    // is deliberately non-blocking and must never interfere with controls.
  } finally {
    trackerRefreshBusy = false;
  }
}

async function refreshPedal() {
  if (pedalRefreshBusy || document.hidden) return;
  pedalRefreshBusy = true;
  try {
    const payload = await fetchJson("/api/pedal-status", { cache: "no-store" }, 1000);
    if (payload.ok && payload.pedal) renderPedalLive(payload.pedal);
  } catch (_error) {
    // Pedal visualization is auxiliary and never blocks capture controls.
  } finally {
    pedalRefreshBusy = false;
  }
}

async function runAction(action, value = "") {
  if (requestBusy) return;
  if (action === "start" && !totalStartViewportLock) {
    totalStartViewportLock = { left: window.scrollX, top: window.scrollY };
    if (document.activeElement === elements.totalButton) elements.totalButton.blur();
  }
  requestBusy = true;
  document.body.classList.add("busy");
  try {
    const payload = await fetchJson("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, value }),
    }, 60000);
    elements.message.textContent = payload.message || "Action complete";
    if (action === "open_offline_annotation_workspace") {
      annotationLoaderNotice = { state: "success", text: payload.message || "Annotation workspace opened." };
    }
    if (payload.status) render(payload.status);
  } catch (error) {
    elements.message.textContent = `Action failed: ${error}`;
    if (action === "open_offline_annotation_workspace") {
      annotationLoaderNotice = { state: "error", text: `Unable to open: ${error.message || error}` };
      elements.annotationLoaderHint.textContent = annotationLoaderNotice.text;
      elements.annotationLoaderHint.dataset.state = annotationLoaderNotice.state;
    }
  } finally {
    requestBusy = false;
    document.body.classList.remove("busy");
    refresh();
  }
}

document.querySelectorAll("button[data-action]").forEach((button) => {
  button.addEventListener("click", () => runAction(button.dataset.action));
});
elements.scanInsta.addEventListener("click", () => {
  const insta = lastRuntimeStatus.insta || adapterStatus(lastRuntimeStatus, "insta");
  runAction(isHostPreviewCamera(insta) ? "scan_cameras" : "scan_insta");
});
elements.connectInsta.addEventListener("click", () => {
  const insta = lastRuntimeStatus.insta || adapterStatus(lastRuntimeStatus, "insta");
  if (isHostPreviewCamera(insta)) {
    runAction(cameraPreviewRunning(insta) ? "disconnect_cameras" : "connect_cameras");
    return;
  }
  runAction("connect_insta");
});
document.querySelectorAll("button[data-camera-view-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    const insta = lastRuntimeStatus.insta || adapterStatus(lastRuntimeStatus, "insta");
    if (!isUsbWebcam(insta) || requestBusy) return;
    elements.cameraViewPanorama.disabled = true;
    elements.cameraViewFlat.disabled = true;
    runAction("set_camera_view_mode", { view_mode: button.dataset.cameraViewMode });
  });
});
Object.values(cameraPreviewViews).forEach((view) => {
  view.image.addEventListener("load", () => {
    delete view.image.dataset.previewRequestPending;
    delete view.image.dataset.failedSequence;
    view.image.dataset.loadedSequence = view.image.dataset.sequence || "";
    view.image.hidden = false;
    if (!view.stage.classList.contains("state-stale")) view.empty.hidden = true;
  });
  view.image.addEventListener("error", () => {
    delete view.image.dataset.previewRequestPending;
    view.image.dataset.failedSequence = view.image.dataset.sequence || "unknown";
    view.image.hidden = true;
    view.empty.hidden = false;
    view.empty.querySelector("strong").textContent = "Preview unavailable";
    view.empty.querySelector("span").textContent = "The latest preview image could not be loaded. Waiting for a new USB frame.";
    view.state.textContent = "Error";
    view.state.dataset.state = "error";
    view.stage.classList.add("state-error");
  });
});
elements.releaseOtherHtc.addEventListener("click", () => {
  if (!window.confirm(
    "Stop the service currently holding the VIVE receiver and give HTC to InstaDataCap?\n\nThis can stop an active OmniIMU or HTC teleoperation task.",
  )) return;
  runAction("release_other_tracker");
});
elements.recoverHtc.addEventListener("click", () => {
  if (!window.confirm(
    "Reopen the VIVE receiver and enable RF reconnect/pairing now?\n\nStored pair identities are not cleared. Do this only while InstaDataCap is idle, then wake and move both Trackers.",
  )) return;
  runAction("recover_tracker");
});
elements.quitApp.addEventListener("click", async () => {
  if (requestBusy) return;
  if (lastRuntimeStatus.running && !window.confirm(
    "Capture is still running. Safely end this capture and exit InstaDataCap?",
  )) return;
  requestBusy = true;
  elements.quitApp.disabled = true;
  elements.quitApp.textContent = "Exiting...";
  try {
    await fetchJson("/api/shutdown", { method: "POST", body: "{}" }, 5000);
    elements.message.textContent = "InstaDataCap is closing cameras, trackers, the foot pedal, and the web service...";
    window.setTimeout(() => window.close(), 150);
  } catch (error) {
    elements.message.textContent = `Exit failed: ${error}`;
    elements.quitApp.disabled = false;
    elements.quitApp.textContent = "⏻ Exit App";
    requestBusy = false;
  }
});
document.querySelectorAll("button[data-mode]").forEach((button) => {
  button.addEventListener("click", () => runAction("mode", button.dataset.mode));
});
document.querySelectorAll("button[data-arm-mode]").forEach((button) => {
  button.addEventListener("click", () => runAction("arm_mode", button.dataset.armMode));
});
document.querySelectorAll("button[data-camera-transport]").forEach((button) => {
  button.addEventListener("click", () => runAction("set_camera_transport", {
    mode: button.dataset.cameraTransport,
  }));
});
elements.captureSettingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction("capture_settings", {
    task_name: elements.taskNameInput.value,
    output_root: elements.outputRootInput.value,
  });
});
elements.clearTimestamps.addEventListener("click", () => {
  const confirmed = window.confirm(
    "Clear all SLAM and episode timestamps for this session?\n\nInsta videos, HTC trajectories, and the capture-start record will remain. The workflow will return to SLAM calibration.",
  );
  if (confirmed) runAction("clear_timestamps");
});
elements.eventsBody.addEventListener("click", (event) => {
  if (event.target.closest("[data-undo-latest]")) runAction("undo_timestamp");
});
elements.slamMarks.addEventListener("click", (event) => {
  const button = event.target.closest("[data-delete-slam]");
  if (button) runAction("delete_slam_timestamp", button.dataset.deleteSlam);
});
elements.annotationOpenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const root = elements.offlineAnnotationRoot.value.trim();
  if (!root) {
    annotationLoaderNotice = { state: "error", text: "Enter an annotation workspace or raw capture directory first." };
    elements.annotationLoaderHint.textContent = annotationLoaderNotice.text;
    elements.annotationLoaderHint.dataset.state = annotationLoaderNotice.state;
    elements.offlineAnnotationRoot.focus();
    return;
  }
  annotationLoaderNotice = { state: "loading", text: "Selecting one episode and generating separate left/right 360-degree panoramas... (no camera connection)" };
  elements.annotationLoaderHint.textContent = annotationLoaderNotice.text;
  elements.annotationLoaderHint.dataset.state = annotationLoaderNotice.state;
  runAction("open_offline_annotation_workspace", root);
});
elements.offlineAnnotationRoot.addEventListener("input", () => {
  annotationLoaderNotice = null;
  elements.annotationLoaderHint.textContent = "Select Open and Start Annotating to check this directory.";
  elements.annotationLoaderHint.dataset.state = "idle";
});
elements.closeOfflineAnnotation.addEventListener("click", () => {
  runAction("close_offline_annotation_workspace");
});
elements.annotationPreviousEpisode.addEventListener("click", () => {
  runAction("navigate_offline_annotation", "previous");
});
elements.annotationCompleteEpisode.addEventListener("click", () => {
  runAction("complete_offline_annotation_episode");
});
Object.entries(annotationViews).forEach(([cameraRole, view]) => {
  const handName = cameraRole === "insta_left" ? "left-hand" : "right-hand";
  view.image.addEventListener("load", () => {
    delete view.image.dataset.loadFailedAsset;
    view.image.hidden = false;
    view.empty.hidden = true;
    drawActiveViewAnnotations(cameraRole);
  });
  view.image.addEventListener("error", () => {
    view.image.dataset.loadFailedAsset = view.image.dataset.assetId || "unknown";
    view.image.hidden = true;
    view.empty.hidden = false;
    view.empty.querySelector("strong").textContent = `Unable to load the ${handName} panorama`;
    view.empty.querySelector("span").textContent = "The reference panorama is missing or was modified. Open the raw capture directory again.";
  });
  view.canvas.addEventListener("click", (event) => toggleTargetAtPointer(event, cameraRole));
  view.clear.addEventListener("click", () => {
    if (window.confirm(`Clear all ${handName} target points from this episode? The audit history will be retained.`)) {
      runAction("clear_target_annotations", cameraRole);
    }
  });
});
elements.gripperPreviewForm.addEventListener("submit", (event) => {
  event.preventDefault();
  discoverGripperPreviewCaptures();
});
elements.gripperPreviewPrevious.addEventListener("click", () => navigateGripperPreview(-1));
elements.gripperPreviewNext.addEventListener("click", () => navigateGripperPreview(1));
elements.gripperPreviewSource.addEventListener("change", () => scheduleGripperPreviewDraw());
elements.gripperThresholdRange.addEventListener("input", () => {
  setGripperThresholdMm(elements.gripperThresholdRange.value);
});
elements.gripperThresholdMm.addEventListener("input", () => {
  if (elements.gripperThresholdMm.value.trim() !== "") {
    setGripperThresholdMm(elements.gripperThresholdMm.value, { preserveNumberText: true });
  }
});
elements.gripperThresholdMm.addEventListener("change", () => {
  setGripperThresholdMm(selectedGripperThresholdMm());
});
elements.gripperPreviewRoot.addEventListener("input", () => {
  if (!gripperPreviewBusy && !gripperPreviewEstimateBusy) {
    gripperPreviewEstimateCacheHit = false;
    gripperPreviewAutoSelectionKey = "";
    gripperPreviewDiscovery = null;
    populateGripperCaptures(null);
    setGripperPreviewNotice("loading", "Waiting for the directory path, then captures and episodes will be discovered automatically…");
    renderGripperPreviewAvailability();
    scheduleGripperPreviewDiscovery();
  }
});
elements.gripperPreviewCapture.addEventListener("change", () => {
  if (!gripperPreviewBusy && !gripperPreviewEstimateBusy) {
    gripperPreviewEstimateCacheHit = false;
    gripperPreviewAutoSelectionKey = "";
    populateGripperEpisodes(selectedGripperCapture());
    if (elements.gripperPreviewCapture.value) {
      setGripperPreviewNotice("idle", "Select an episode. Its continuous gripper widths will be estimated automatically and cached beside the raw capture.");
    }
    renderGripperPreviewAvailability();
  }
});
elements.gripperPreviewEpisode.addEventListener("change", () => {
  if (!gripperPreviewBusy && !gripperPreviewEstimateBusy && elements.gripperPreviewEpisode.value) {
    gripperPreviewEstimateCacheHit = false;
    gripperPreviewAutoSelectionKey = "";
    try {
      window.localStorage.setItem(GRIPPER_PREVIEW_CAPTURE_STORAGE_KEY, elements.gripperPreviewCapture.value);
      window.localStorage.setItem(GRIPPER_PREVIEW_EPISODE_STORAGE_KEY, elements.gripperPreviewEpisode.value);
    } catch (_error) {
      // Remembering the selection is optional.
    }
    if (elements.gripperPreviewCapture.value) {
      estimateRawGripperPreviewEpisode();
    } else {
      loadGripperPreview(Number(elements.gripperPreviewEpisode.value));
    }
  }
});
let restoreGripperPreviewSelection = false;
try {
  elements.gripperPreviewRoot.value = window.localStorage.getItem(GRIPPER_PREVIEW_PATH_STORAGE_KEY) || "";
  restoreGripperPreviewSelection = Boolean(elements.gripperPreviewRoot.value);
  const savedThresholdText = window.localStorage.getItem(GRIPPER_PREVIEW_THRESHOLD_STORAGE_KEY);
  const savedThreshold = savedThresholdText === null ? null : Number(savedThresholdText);
  if (savedThreshold !== null && Number.isFinite(savedThreshold)) {
    setGripperThresholdMm(savedThreshold);
    gripperThresholdInitialized = true;
  }
} catch (_error) {
  // The first server status supplies the configured default threshold.
}
if (elements.gripperPreviewRoot.value) {
  discoverGripperPreviewCaptures({ restoreSelection: restoreGripperPreviewSelection });
}
window.addEventListener("resize", () => {
  drawActiveViewAnnotations();
  scheduleGripperPreviewDraw();
});

refresh();
refreshTracker();
refreshPedal();
setInterval(refresh, 500);
setInterval(refreshUsbCameraPreviewFrames, 100);
setInterval(refreshTracker, 100);
setInterval(refreshPedal, 50);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refresh();
    refreshTracker();
    refreshPedal();
  }
});
