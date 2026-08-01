const stage = document.getElementById("stage");
const form = document.getElementById("bar");
const goalInput = document.getElementById("q");
const runButton = document.getElementById("runButton");
const screen = document.getElementById("screen");
const frame = document.getElementById("frame");
const edge = document.getElementById("edge");
const placeholder = document.getElementById("screenPlaceholder");
const workingBadge = document.getElementById("workingBadge");
const resultBanner = document.getElementById("resultBanner");
const decisionPanel = document.getElementById("decisionPanel");
const decisionVisual = document.getElementById("decisionVisual");
const decisionStatus = document.getElementById("decisionStatus");
const decisionDestination = document.getElementById("decisionDestination");
const decisionService = document.getElementById("decisionService");
const decisionCategory = document.getElementById("decisionCategory");
const decisionSummary = document.getElementById("decisionSummary");
const decisionReason = document.getElementById("decisionReason");
const decisionNext = document.getElementById("decisionNext");
const decisionSource = document.getElementById("decisionSource");
const openHandoff = document.getElementById("openHandoff");
const timeline = document.getElementById("timeline");
const modelValue = document.getElementById("modelValue");
const runValue = document.getElementById("runValue");
const costValue = document.getElementById("costValue");
const streamMode = document.getElementById("streamMode");
const currentUrl = document.getElementById("currentUrl");
const stopCaption = document.getElementById("stopCaption");
const screenNotice = document.getElementById("screenNotice");
const termsPopup = document.getElementById("termsPopup");
const termsSummary = document.getElementById("termsSummary");
const missingInfoPopup = document.getElementById("missingInfoPopup");
const missingInfoTitle = document.getElementById("missingInfoTitle");
const missingInfoText = document.getElementById("missingInfoText");
const missingInfoClose = document.getElementById("missingInfoClose");
const emsCustomsForm = document.getElementById("emsCustomsForm");
const emsWeight = document.getElementById("emsWeight");
const emsValue = document.getElementById("emsValue");
const emsRecipientEmail = document.getElementById("emsRecipientEmail");
const emsCustomsDescription = document.getElementById("emsCustomsDescription");
const emsHsCode = document.getElementById("emsHsCode");
const emsQuantity = document.getElementById("emsQuantity");
const emsCustomsSubmit = document.getElementById("emsCustomsSubmit");
const rawToggle = document.getElementById("rawToggle");
const rawToggleLabel = document.getElementById("rawToggleLabel");
const rawPopup = document.getElementById("rawPopup");
const rawClose = document.getElementById("rawClose");
const rawPause = document.getElementById("rawPause");
const rawPopupLog = document.getElementById("rawPopupLog");
const ocrOpen = document.getElementById("ocrOpen");
const ocrFile = document.getElementById("ocrFile");
const ocrPanel = document.getElementById("ocrPanel");
const ocrClose = document.getElementById("ocrClose");
const ocrLoading = document.getElementById("ocrLoading");
const ocrReviewForm = document.getElementById("ocrReviewForm");
const ocrError = document.getElementById("ocrError");
const ocrWarnings = document.getElementById("ocrWarnings");
const ocrSubmit = document.getElementById("ocrSubmit");
const ocrFields = {
  senderName: document.getElementById("ocrSenderName"),
  senderPostal: document.getElementById("ocrSenderPostal"),
  senderAddress: document.getElementById("ocrSenderAddress"),
  senderDetail: document.getElementById("ocrSenderDetail"),
  senderPhone: document.getElementById("ocrSenderPhone"),
  recipientName: document.getElementById("ocrRecipientName"),
  recipientPostal: document.getElementById("ocrRecipientPostal"),
  recipientAddress: document.getElementById("ocrRecipientAddress"),
  recipientDetail: document.getElementById("ocrRecipientDetail"),
  recipientPhone: document.getElementById("ocrRecipientPhone"),
  contents: document.getElementById("ocrContents"),
};

const protocol = location.protocol === "https:" ? "wss" : "ws";
const socket = new WebSocket(`${protocol}://${location.host}/ws/run`);
let sent = false;
let activeRunId = "";
let adapterMatched = null;
let viewport = {width: 1100, height: 720};
let finalEdge = null;
let lastTimelineKey = "";
let rawSocket = null;
let rawPaused = false;
let rawQueue = [];
let termsTimer = null;
let runFinished = false;
let needsInformation = false;
let resolvedContactLabel = "받는 분";
let ocrResult = null;

ocrOpen.addEventListener("click", () => { if (!sent) ocrFile.click(); });
ocrClose.addEventListener("click", () => { if (!ocrLoading.hidden) return; ocrPanel.hidden = true; });
ocrFile.addEventListener("change", async () => {
  const file = ocrFile.files?.[0];
  ocrFile.value = "";
  if (!file) return;
  ocrPanel.hidden = false;
  ocrLoading.hidden = false;
  ocrReviewForm.hidden = true;
  ocrError.hidden = true;
  if (file.size > 8 * 1024 * 1024) {
    ocrLoading.hidden = true;
    ocrError.textContent = "사진은 8MB 이하만 사용할 수 있습니다.";
    ocrError.hidden = false;
    return;
  }
  try {
    const imageBase64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    const response = await fetch("/api/ocr/address-note", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({image_base64: imageBase64, mime_type: file.type}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "쪽지를 읽지 못했습니다.");
    ocrResult = payload;
    const sender = payload.sender || {};
    const recipient = payload.recipient || {};
    ocrFields.senderName.value = sender.name_ko || sender.name_en || "";
    ocrFields.senderPostal.value = sender.postal_code || "";
    ocrFields.senderAddress.value = sender.address_base || sender.address || "";
    ocrFields.senderDetail.value = sender.address_detail || "";
    ocrFields.senderPhone.value = sender.phone || "";
    ocrFields.recipientName.value = recipient.name_ko || recipient.name_en || "";
    ocrFields.recipientPostal.value = recipient.postal_code || "";
    ocrFields.recipientAddress.value = recipient.address_base || recipient.address || "";
    ocrFields.recipientDetail.value = recipient.address_detail || "";
    ocrFields.recipientPhone.value = recipient.phone || "";
    ocrFields.contents.value = payload.contents || "사과";
    const warnings = payload.warnings || [];
    ocrWarnings.textContent = warnings.join(" · ");
    ocrWarnings.hidden = warnings.length === 0;
    ocrReviewForm.hidden = false;
  } catch (error) {
    ocrError.textContent = error.message || "쪽지를 읽지 못했습니다.";
    ocrError.hidden = false;
  } finally {
    ocrLoading.hidden = true;
  }
});

ocrReviewForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!ocrResult || !ocrReviewForm.reportValidity()) return;
  ocrSubmit.disabled = true;
  ocrSubmit.textContent = "안전하게 불러오는 중";
  const makeParty = (original, name, postal, address, detail, phone) => ({
    ...original,
    name_ko: name.value.trim(),
    postal_code: postal.value.trim(),
    address: [address.value.trim(), detail.value.trim()].filter(Boolean).join(" "),
    address_base: address.value.trim(),
    address_detail: detail.value.trim(),
    phone: phone.value.trim(),
  });
  try {
    const response = await fetch("/api/ocr/contacts", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        sender: makeParty(ocrResult.sender, ocrFields.senderName, ocrFields.senderPostal, ocrFields.senderAddress, ocrFields.senderDetail, ocrFields.senderPhone),
        recipient: makeParty(ocrResult.recipient, ocrFields.recipientName, ocrFields.recipientPostal, ocrFields.recipientAddress, ocrFields.recipientDetail, ocrFields.recipientPhone),
        contents: ocrFields.contents.value.trim(),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "주소 정보를 불러오지 못했습니다.");
    ocrPanel.hidden = true;
    goalInput.value = payload.goal;
    resolvedContactLabel = payload.recipient_label || "쪽지 받는 분";
    form.requestSubmit();
  } catch (error) {
    ocrError.textContent = error.message || "주소 정보를 불러오지 못했습니다.";
    ocrError.hidden = false;
  } finally {
    ocrSubmit.disabled = false;
    ocrSubmit.textContent = "이 정보로 접수 준비";
  }
});

missingInfoClose.addEventListener("click", () => { missingInfoPopup.hidden = true; });
emsCustomsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activeRunId || !emsCustomsForm.reportValidity()) return;
  emsCustomsSubmit.disabled = true;
  emsCustomsSubmit.textContent = "EMS 입력 중";
  missingInfoClose.hidden = true;
  setState("running");
  showResult("EMS 접수를 이어서 입력하고 있습니다.");
  addTimeline("EMS 필수정보 입력", `${emsWeight.value}kg · $${emsValue.value} · 이메일 확인`);
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(activeRunId)}/ems-customs`, {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        weight_kg: Number(emsWeight.value),
        customs_value_usd: Number(emsValue.value),
        recipient_email: emsRecipientEmail.value.trim(),
        customs_description_en: emsCustomsDescription.value.trim(),
        hs_code: emsHsCode.value.trim(),
        quantity: Number(emsQuantity.value),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "EMS 접수를 이어가지 못했습니다.");
    missingInfoPopup.hidden = true;
    needsInformation = false;
    if (payload.status === "staged") {
      setState("stopped");
      showResult("아래 접수신청 버튼만 누르면 실제 접수가 되기에 중단했습니다.");
      stopCaption.hidden = false;
      addTimeline("EMS 접수신청 직전 중단", "최종 선택은 사용자가 직접 합니다.", "done");
      openHandoff.href = `/handoff?run_id=${encodeURIComponent(activeRunId)}`;
      openHandoff.hidden = false;
    } else if (payload.status === "continued") {
      if (payload.frame_data) {
        frame.src = `data:image/jpeg;base64,${payload.frame_data}`;
        frame.style.display = "block";
        screen.classList.add("has-frame");
        placeholder.hidden = true;
        if (payload.viewport?.width && payload.viewport?.height) viewport = payload.viewport;
      }
      setState("paused");
      runButton.textContent = "입력 완료";
      showResult("EMS 필수정보를 실제 세관신고 내역에 입력했습니다.");
      addTimeline("EMS 세관신고 내역 확인", "왼쪽 실제 화면에 입력한 내용이 표시됩니다.", "done");
    }
  } catch (error) {
    setState("needs-info");
    showResult(error.message, true);
    missingInfoClose.hidden = false;
    addTimeline("EMS 입력을 멈췄어요", error.message, "error");
  } finally {
    emsCustomsSubmit.disabled = false;
    emsCustomsSubmit.textContent = "EMS 계속하기";
  }
});

function showTermsSummary(text, duration = 5000) {
  clearTimeout(termsTimer);
  termsSummary.textContent = text;
  termsPopup.hidden = false;
  const countdown = termsPopup.querySelector("i");
  countdown.style.animation = "none";
  void countdown.offsetWidth;
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    countdown.style.animation = `terms-countdown ${duration}ms linear forwards`;
  }
  termsTimer = setTimeout(() => { termsPopup.hidden = true; }, duration);
}

function appendRaw(wrapper) {
  if (!activeRunId || wrapper.run_id !== activeRunId) return;
  const text = JSON.stringify(wrapper.event || wrapper, null, 2);
  const line = document.createElement("div");
  line.className = `raw-popup-line${/(tool_call|tool_result|function_call|function_call_output)/.test(text) ? " is-tool" : ""}`;
  line.textContent = text;
  rawPopupLog.querySelector(".raw-popup-empty")?.remove();
  rawPopupLog.append(line);
  while (rawPopupLog.children.length > 80) rawPopupLog.firstElementChild.remove();
  rawPopupLog.scrollTop = rawPopupLog.scrollHeight;
}

function connectRaw() {
  if (rawSocket) return;
  rawSocket = new WebSocket(`${protocol}://${location.host}/ws/raw?history=1&wrapped=1`);
  rawSocket.addEventListener("message", ({data}) => {
    const wrapper = JSON.parse(data);
    if (rawPaused) rawQueue.push(wrapper);
    else appendRaw(wrapper);
  });
  rawSocket.addEventListener("close", () => { rawSocket = null; });
}

function setRawPopup(open) {
  rawPopup.hidden = !open;
  rawToggle.setAttribute("aria-expanded", String(open));
  rawToggleLabel.textContent = open ? "RAW API 닫기" : "RAW API 열기";
  if (open) connectRaw();
}

rawToggle.addEventListener("click", () => setRawPopup(rawPopup.hidden));
rawClose.addEventListener("click", () => setRawPopup(false));
rawPause.addEventListener("click", () => {
  rawPaused = !rawPaused;
  rawPause.textContent = rawPaused ? `계속 (${rawQueue.length})` : "일시정지";
  if (!rawPaused) {
    rawQueue.splice(0).forEach(appendRaw);
    rawPause.textContent = "일시정지";
  }
});

function setState(value) {
  stage.dataset.state = value;
  workingBadge.hidden = value !== "running";
}

function showResult(text, error = false) {
  resultBanner.textContent = text;
  resultBanner.hidden = false;
  resultBanner.classList.toggle("is-error", error);
}

function showPolicyDecision(message) {
  const labels = {
    clear_to_prepare: "접수 준비 가능",
    needs_review: "확인 후 진행",
    blocked: "발송 중단",
  };
  const categories = {
    ordinary_goods: "일반 물품",
    fresh_produce: "생과일",
    air_security_goods: "항공 제한품",
    food: "식품",
  };
  const visuals = {
    clear_to_prepare: {
      src: "/visual-assets/decision-clear.png",
      alt: "안전 확인을 마치고 접수 준비가 가능한 소포",
    },
    needs_review: {
      src: "/visual-assets/decision-review.png",
      alt: "발송 전 추가 조건을 확인하는 소포",
    },
    blocked: {
      src: "/visual-assets/decision-blocked.png",
      alt: "사용자를 보호하기 위해 발송을 중단한 소포",
    },
  };
  const visual = visuals[message.decision] || visuals.needs_review;
  decisionPanel.className = "decision-panel";
  decisionPanel.classList.add(
    message.decision === "clear_to_prepare"
      ? "is-clear"
      : message.decision === "needs_review" ? "is-review" : "is-blocked"
  );
  decisionVisual.src = visual.src;
  decisionVisual.alt = visual.alt;
  decisionStatus.textContent = labels[message.decision] || "확인 필요";
  decisionDestination.textContent = message.destination_country === "KR" ? "대한민국" : message.destination_country === "US" ? "미국" : "해외";
  decisionService.textContent = message.service === "ems" ? "EMS" : "국내소포";
  decisionCategory.textContent = categories[message.category] || "물품 확인";
  decisionSummary.textContent = message.plain_summary || "발송 조건을 확인했습니다.";
  decisionReason.textContent = message.reason || "";
  decisionNext.textContent = message.next_action || "";
  decisionSource.textContent = "공식 근거 보기 ↗";
  decisionSource.title = message.source_title || "공식 근거";
  decisionSource.href = message.source_url || "#";
  decisionPanel.hidden = false;
}

function addTimeline(title, detail = "", state = "current") {
  const key = `${title}|${detail}`;
  if (key === lastTimelineKey) return;
  lastTimelineKey = key;
  timeline.querySelectorAll(".is-current").forEach((item) => {
    item.classList.remove("is-current");
    item.classList.add("is-done");
  });
  const item = document.createElement("article");
  item.className = `timeline-item is-${state}`;
  const dot = document.createElement("i");
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  const span = document.createElement("span");
  strong.textContent = title;
  span.textContent = detail;
  copy.append(strong, span);
  item.append(dot, copy);
  timeline.append(item);
  timeline.scrollTop = timeline.scrollHeight;
}

function updateEdge() {
  if (!finalEdge || !screen.clientWidth || !screen.clientHeight) return;
  const scale = Math.min(screen.clientWidth / viewport.width, screen.clientHeight / viewport.height);
  const drawnWidth = viewport.width * scale;
  const offsetX = (screen.clientWidth - drawnWidth) / 2;
  const offsetY = 0;
  const padding = 6;
  Object.assign(edge.style, {
    left: `${offsetX + finalEdge.x * viewport.width * scale - padding}px`,
    top: `${offsetY + finalEdge.y * viewport.height * scale - padding}px`,
    width: `${finalEdge.w * viewport.width * scale + padding * 2}px`,
    height: `${finalEdge.h * viewport.height * scale + padding * 2}px`,
  });
}

let noticeTimer = null;
function showScreenNotice(text) {
  screenNotice.textContent = text;
  screenNotice.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { screenNotice.hidden = true; }, 2600);
}

screen.addEventListener("click", async (event) => {
  if (!activeRunId || frame.style.display !== "block") return;
  if (stage.dataset.state !== "stopped") {
    showScreenNotice("에이전트 실행이 끝난 뒤 직접 조작할 수 있습니다.");
    return;
  }
  const bounds = screen.getBoundingClientRect();
  const scale = Math.min(bounds.width / viewport.width, bounds.height / viewport.height);
  const drawnWidth = viewport.width * scale;
  const drawnHeight = viewport.height * scale;
  const offsetX = (bounds.width - drawnWidth) / 2;
  const offsetY = 0;
  const sourceX = (event.clientX - bounds.left - offsetX) / scale;
  const sourceY = (event.clientY - bounds.top - offsetY) / scale;
  if (sourceX < 0 || sourceY < 0 || sourceX > viewport.width || sourceY > viewport.height) return;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(activeRunId)}/click`, {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({x: sourceX / viewport.width, y: sourceY / viewport.height}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "화면을 조작하지 못했습니다.");
    showScreenNotice("실제 우체국 화면에 클릭을 전달했습니다.");
  } catch (error) {
    showScreenNotice(error.message);
  }
});

window.addEventListener("resize", updateEdge);

socket.addEventListener("message", ({data}) => {
  const message = JSON.parse(data);
  if (message.type === "run") {
    activeRunId = message.run_id;
    runValue.textContent = message.run_id.slice(0, 10);
    if (message.model) modelValue.textContent = message.model;
  } else if (message.type === "state" && message.value === "running") {
    setState("running");
    addTimeline("요청을 이해했어요", "우체국 접수에 필요한 순서를 계획합니다.");
  } else if (message.type === "adapter") {
    adapterMatched = message.matched;
    if (message.matched) {
      addTimeline("우체국 비회원 접수 선택", "저장된 주소를 확인해 국내소포와 EMS를 구분합니다.");
    } else {
      showResult("현재 데모는 우체국 소포 접수만 지원합니다.", true);
      addTimeline("지원하지 않는 요청", "등록된 우체국 소포 작업과 일치하지 않습니다.", "error");
    }
  } else if (message.type === "route") {
    if (message.service === "ems") {
      addTimeline("미국 주소 확인 · EMS 자동 선택", "해외 주소이므로 우체국 EMS 비회원 접수로 전환했습니다.");
      currentUrl.textContent = "ems.epost.go.kr";
    } else {
      addTimeline("국내 주소 확인 · 창구소포 선택", "국내 주소이므로 우체국 간편사전접수를 사용합니다.");
    }
  } else if (message.type === "contact") {
    resolvedContactLabel = message.display_label || "받는 분";
    addTimeline("격리된 연락처 확인", `${message.display_label || "큰딸"} 정보를 안전하게 불러왔습니다.`);
  } else if (message.type === "policy") {
    showPolicyDecision(message);
    if (message.decision === "clear_to_prepare") {
      addTimeline("발송 위험 사전점검 통과", "우체국 공식 화면 검증을 계속합니다.");
    } else if (message.decision === "needs_review") {
      needsInformation = true;
      setState("policy-stop");
      runButton.textContent = "확인 필요";
      showResult(message.plain_summary || "발송 전 확인이 필요합니다.");
      addTimeline("조건 확인이 필요해요", message.next_action || message.reason, "error");
      placeholder.querySelector("strong").textContent = "확인 전에는 접수를 시작하지 않아요";
      placeholder.querySelector("span").textContent = message.reason || "추가 조건을 먼저 확인해 주세요.";
    } else {
      needsInformation = true;
      setState("policy-stop");
      runButton.textContent = "발송 중단";
      showResult(message.plain_summary || "이 물건은 지금 보낼 수 없습니다.");
      addTimeline("반송 위험을 먼저 막았어요", message.reason || "공식 발송 조건과 충돌합니다.", "error");
      placeholder.querySelector("strong").textContent = "접수를 시작하지 않았어요";
      placeholder.querySelector("span").textContent = message.reason || "보낼 수 없는 물품을 먼저 확인했습니다.";
    }
  } else if (message.type === "terms_summary") {
    showTermsSummary(message.text, Number(message.duration_ms || 5000));
    addTimeline("필수 약관 핵심 안내", message.text);
  } else if (message.type === "frame") {
    frame.src = `data:image/jpeg;base64,${message.data}`;
    frame.style.display = "block";
    screen.classList.add("has-frame");
    placeholder.hidden = true;
    if (message.viewport?.width && message.viewport?.height) viewport = message.viewport;
    if (message.url) currentUrl.textContent = message.url.replace(/^https?:\/\//, "");
    if (message.stream_mode) streamMode.textContent = message.stream_mode === "cdp" ? "CDP LIVE" : "LIVE";
    updateEdge();
  } else if (message.type === "step") {
    addTimeline(message.label || "우체국 화면 입력", `${Math.round(message.progress || 0)}% 완료`);
    if (message.url) currentUrl.textContent = message.url.replace(/^https?:\/\//, "");
  } else if (message.type === "stage_result") {
    if (message.status === "needs_information") {
      needsInformation = true;
      setState("needs-info");
      runButton.textContent = "정보 필요";
      const missingFields = message.missing_fields || [];
      const evidence = message.evidence || {};
      emsCustomsDescription.value = evidence.customs_description_en || "";
      emsHsCode.value = evidence.hs_code || "";
      emsQuantity.value = String(evidence.quantity || 1);
      const missingEmsCustoms = ["shipment.weight", "shipment.customs_value", "recipient.email", "shipment.customs_description_en", "shipment.hs_code"].some((field) => missingFields.includes(field));
      const missingDomesticAddress = missingFields.includes("recipient.address_domestic");
      const detail = missingEmsCustoms
        ? "미국 주소를 확인해 EMS로 자동 전환했습니다. 에이전트가 해석한 통관 품목을 확인하고 무게·물품가액·이메일을 입력해 주세요."
        : missingDomesticAddress
        ? `${resolvedContactLabel}은 현재 해외 주소만 저장되어 있습니다. 국내소포로 보내려면 국내 주소와 국내 연락처가 필요합니다.`
        : `${resolvedContactLabel}의 접수 필수정보가 부족합니다.`;
      missingInfoText.textContent = detail;
      missingInfoTitle.textContent = missingEmsCustoms ? "EMS 통관정보가 필요해요" : "접수정보가 필요해요";
      emsCustomsForm.hidden = !missingEmsCustoms;
      missingInfoClose.hidden = false;
      missingInfoPopup.hidden = false;
      showResult(missingEmsCustoms ? "EMS 통관정보를 확인해 주세요." : `${resolvedContactLabel}의 접수정보가 필요합니다.`, true);
      addTimeline("추가 정보 필요", detail, "error");
    } else {
      addTimeline("입력 완료", "접수신청 직전 화면을 확인합니다.");
    }
  } else if (message.type === "safe_stop") {
    addTimeline("안전 경계 확인", `${message.button_text || "접수신청"} 버튼을 누르지 않았습니다.`);
  } else if (message.type === "edge") {
    finalEdge = message;
    updateEdge();
  } else if (message.type === "verdict") {
    showResult(message.text);
    stopCaption.textContent = message.text;
    stopCaption.hidden = false;
  } else if (message.type === "state" && message.value === "stopped") {
    setState("stopped");
    updateEdge();
    addTimeline("접수신청 직전 중단", "최종 선택은 사용자가 직접 합니다.", "done");
    openHandoff.href = `/handoff?run_id=${encodeURIComponent(activeRunId)}`;
    openHandoff.hidden = adapterMatched !== true;
  } else if (message.type === "completed") {
    runFinished = true;
    costValue.textContent = `$${Number(message.cost || 0).toFixed(4)}`;
    if (adapterMatched === false) setState("error");
  } else if (message.type === "error") {
    setState("error");
    showResult(message.text || "실행 중 연결 문제가 생겼습니다.", true);
    addTimeline("실행을 멈췄어요", "접수는 진행되지 않았습니다.", "error");
  }
});

socket.addEventListener("close", () => {
  if (sent && !runFinished && !needsInformation && !["stopped", "error"].includes(stage.dataset.state)) {
    setState("error");
    showResult("실행 화면 연결이 끊겼습니다. 접수는 진행되지 않았습니다.", true);
  }
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const goal = goalInput.value.trim();
  if (!goal || sent || socket.readyState !== WebSocket.OPEN) return;
  sent = true;
  decisionPanel.hidden = true;
  goalInput.disabled = true;
  runButton.disabled = true;
  ocrOpen.disabled = true;
  runButton.textContent = "실행 중";
  timeline.replaceChildren();
  addTimeline("사용자 입력 1회", goal);
  setState("running");
  socket.send(JSON.stringify({type: "goal", text: goal}));
});

goalInput.focus();
