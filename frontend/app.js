let currentOrder = null;
let countdownInterval = null;
let socket = null;
let qrCodeInstance = null;

// Storage key for session persistence
const STORAGE_KEY = "esewa_active_order_session";

// DOM Elements
const stepCart = document.getElementById("step-cart");
const stepPayment = document.getElementById("step-payment");
const stepSuccess = document.getElementById("step-success");

const orderForm = document.getElementById("order-form");
const baseAmountInput = document.getElementById("base-amount");
const bankSelect = document.getElementById("bank-select");
const customerNameInput = document.getElementById("customer-name");

const displayTargetAmount = document.getElementById("display-target-amount");
const displayBaseAmount = document.getElementById("display-base-amount");
const copyAmountVal = document.getElementById("copy-amount-val");
const btnCopyAmount = document.getElementById("btn-copy-amount");
const instructionAmount = document.getElementById("instruction-amount");
const selectedBankLabel = document.getElementById("selected-bank-label");
const countdownTimer = document.getElementById("countdown-timer");

const btnToggleFallback = document.getElementById("btn-toggle-fallback");
const fallbackBox = document.getElementById("fallback-box");
const fallbackAmountInput = document.getElementById("fallback-amount");
const fallbackRefInput = document.getElementById("fallback-ref");
const btnClaimFallback = document.getElementById("btn-claim-fallback");
const fallbackMsg = document.getElementById("fallback-msg");
const btnCancelOrder = document.getElementById("btn-cancel-order");

const receiptOrderId = document.getElementById("receipt-order-id");
const receiptAmount = document.getElementById("receipt-amount");
const receiptRef = document.getElementById("receipt-ref");
const receiptBank = document.getElementById("receipt-bank");

// 1. Fetch available banks, check configuration, and restore active session
async function loadBanks() {
  try {
    const healthRes = await fetch("/api/health");
    const healthData = await healthRes.json();
    const envAlert = document.getElementById("env-alert");
    const envAlertText = document.getElementById("env-alert-text");

    if (healthData && !healthData.configured && envAlert && envAlertText) {
      envAlert.classList.remove("hidden");
      envAlertText.textContent = `Configuration missing in .env (${healthData.missing_variables.join(", ")}). Running in simulation mode.`;
    }

    const res = await fetch("/api/banks");
    const data = await res.json();
    if (data.banks) {
      bankSelect.innerHTML = '<option value="" disabled selected>Select Bank or eSewa</option>';
      data.banks.forEach(bank => {
        const opt = document.createElement("option");
        opt.value = bank.name;
        opt.textContent = bank.name;
        bankSelect.appendChild(opt);
      });

      // Check URL parameters for custom prefill (e.g. ?amount=500&bank=Global%20IME)
      const urlParams = new URLSearchParams(window.location.search);
      const paramAmt = urlParams.get("amount") || urlParams.get("amt");
      const paramBank = urlParams.get("bank");
      const paramName = urlParams.get("name") || urlParams.get("customer");

      if (paramAmt && !isNaN(parseFloat(paramAmt)) && parseFloat(paramAmt) > 0) {
        setAmountValue(parseFloat(paramAmt));
      }
      if (paramName && customerNameInput) {
        customerNameInput.value = paramName;
      }
      if (paramBank) {
        const matchingOpt = Array.from(bankSelect.options).find(o => 
          o.value.toLowerCase().includes(paramBank.toLowerCase())
        );
        if (matchingOpt) {
          bankSelect.value = matchingOpt.value;
        }
      }
    }

    // Check for existing active session on page refresh
    await restoreSavedSession();

  } catch (err) {
    console.error("Failed to load setup:", err);
  }
}

// 2. Helper to set and sync amount
function setAmountValue(amt) {
  baseAmountInput.value = amt;
  document.querySelectorAll(".preset-btn").forEach(b => {
    b.classList.toggle("active", parseFloat(b.dataset.amt) === parseFloat(amt));
  });
}

// Preset button click listeners
document.querySelectorAll(".preset-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    setAmountValue(btn.dataset.amt);
  });
});

// Custom typed amount input listener
baseAmountInput.addEventListener("input", () => {
  const currentVal = parseFloat(baseAmountInput.value);
  document.querySelectorAll(".preset-btn").forEach(b => {
    b.classList.toggle("active", parseFloat(b.dataset.amt) === currentVal);
  });
});

// 3. Handle Order Creation
orderForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const rawAmt = baseAmountInput.value.trim();
  const baseAmount = parseFloat(rawAmt);
  const bankName = bankSelect.value;
  const customerName = customerNameInput.value.trim() || "Customer";

  if (!rawAmt || isNaN(baseAmount) || baseAmount <= 0) {
    alert("Please enter a valid payment amount greater than 0.");
    baseAmountInput.focus();
    return;
  }

  if (!bankName) {
    alert("Please select a bank or payment method.");
    bankSelect.focus();
    return;
  }

  const btn = document.getElementById("btn-create-order");
  btn.disabled = true;
  btn.textContent = "Generating QR...";

  try {
    const res = await fetch("/api/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_amount: baseAmount,
        bank_name: bankName,
        customer_name: customerName,
        item_description: "Order Checkout"
      })
    });

    const data = await res.json();
    if (res.ok && data.success && data.order) {
      currentOrder = data.order;
      // Save order to session storage for persistence on refresh
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(currentOrder));
      showPaymentStep(currentOrder);
    } else {
      alert("Error: " + (data.detail || data.message || "Failed to create order"));
    }
  } catch (err) {
    console.error("Order creation failed:", err);
    alert("Connection error. Ensure the backend server is running.");
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate Payment QR";
  }
});

// 4. Render Active Payment Screen
function showPaymentStep(order) {
  stepCart.classList.add("hidden");
  stepPayment.classList.remove("hidden");
  stepSuccess.classList.add("hidden");

  const targetAmt = parseFloat(order.target_amount !== undefined ? order.target_amount : order.base_amount);
  const baseAmt = parseFloat(order.base_amount !== undefined ? order.base_amount : targetAmt);

  displayTargetAmount.textContent = `NPR ${targetAmt.toFixed(2)}`;
  if (displayBaseAmount) {
    if (Math.abs(targetAmt - baseAmt) >= 0.009) {
      displayBaseAmount.textContent = `NPR ${baseAmt.toFixed(2)}`;
      displayBaseAmount.style.display = "inline";
    } else {
      displayBaseAmount.textContent = "";
      displayBaseAmount.style.display = "none";
    }
  }
  copyAmountVal.textContent = targetAmt.toFixed(2);
  instructionAmount.textContent = `NPR ${targetAmt.toFixed(2)}`;
  selectedBankLabel.textContent = order.bank_name;
  
  fallbackAmountInput.value = targetAmt.toFixed(2);

  // Render QR Code
  const qrContainer = document.getElementById("qrcode");
  qrContainer.innerHTML = "";
  const payload = order.qr_payload || JSON.stringify({
    eSewa_id: order.esewa_id || "",
    name: order.esewa_name || "",
    amount: targetAmt,
    remarks: order.id
  });
  qrCodeInstance = new QRCode(qrContainer, {
    text: payload,
    width: 185,
    height: 185,
    colorDark: "#070b13",
    colorLight: "#ffffff",
    correctLevel: QRCode.CorrectLevel.M
  });

  // Start countdown timer
  const expiryEpoch = order.expires_at ? new Date(order.expires_at).getTime() : Date.now() + (order.expires_in_seconds || 600) * 1000;
  startCountdown(expiryEpoch);

  // Connect WebSocket
  connectOrderWebSocket(order.id);
}

// 5. Restore Saved Session On Refresh
async function restoreSavedSession() {
  const savedData = sessionStorage.getItem(STORAGE_KEY);
  if (!savedData) return;

  try {
    const order = JSON.parse(savedData);
    if (!order || !order.id) return;

    // Check order state from server
    const res = await fetch(`/api/orders/${order.id}`);
    if (!res.ok) {
      sessionStorage.removeItem(STORAGE_KEY);
      return;
    }

    const data = await res.json();
    if (data.success && data.order) {
      const liveOrder = data.order;
      if (liveOrder.status === "PENDING") {
        const remaining = new Date(liveOrder.expires_at).getTime() - Date.now();
        if (remaining > 0) {
          currentOrder = liveOrder;
          showPaymentStep(currentOrder);
        } else {
          sessionStorage.removeItem(STORAGE_KEY);
        }
      } else if (liveOrder.status === "PAID") {
        showSuccessStep({
          order_id: liveOrder.id,
          amount_paid: liveOrder.target_amount || liveOrder.base_amount,
          ref_code: liveOrder.matched_ref_code || "VERIFIED",
          bank_name: liveOrder.bank_name
        });
      } else {
        sessionStorage.removeItem(STORAGE_KEY);
      }
    }
  } catch (e) {
    console.error("Error restoring session:", e);
    sessionStorage.removeItem(STORAGE_KEY);
  }
}

// 6. Reset to Checkout Form
function resetToCheckout() {
  sessionStorage.removeItem(STORAGE_KEY);
  currentOrder = null;
  if (countdownInterval) clearInterval(countdownInterval);
  if (socket) socket.close();

  stepPayment.classList.add("hidden");
  stepSuccess.classList.add("hidden");
  stepCart.classList.remove("hidden");
}

// Cancel / Change Order Button Listener
if (btnCancelOrder) {
  btnCancelOrder.addEventListener("click", () => {
    resetToCheckout();
  });
}

// 7. Copy Exact Amount
btnCopyAmount.addEventListener("click", () => {
  if (!currentOrder) return;
  const targetAmt = typeof currentOrder.target_amount === "number" ? currentOrder.target_amount : currentOrder.base_amount;
  const amtStr = targetAmt.toFixed(2);
  navigator.clipboard.writeText(amtStr).then(() => {
    const originalHtml = btnCopyAmount.innerHTML;
    btnCopyAmount.innerHTML = `
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"></polyline>
      </svg>
      <span>Copied ${amtStr}</span>
    `;
    setTimeout(() => {
      btnCopyAmount.innerHTML = originalHtml;
    }, 2000);
  });
});

// 8. Real-time WebSocket connection
function connectOrderWebSocket(orderId) {
  if (socket) {
    try { socket.close(); } catch (e) {}
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/orders/${orderId}`;
  
  socket = new WebSocket(wsUrl);

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === "PAYMENT_SUCCESS" || data.status === "PAID") {
        sessionStorage.removeItem(STORAGE_KEY);
        showSuccessStep({
          order_id: data.order_id || orderId,
          amount_paid: data.amount_paid || currentOrder.base_amount,
          ref_code: data.ref_code || "VERIFIED",
          bank_name: data.bank_name || currentOrder.bank_name
        });
      }
    } catch (e) {
      console.error("WebSocket parse error:", e);
    }
  };

  socket.onclose = () => {
    console.log("WebSocket closed.");
  };
}

// 9. Render Success Screen
function showSuccessStep(receiptData) {
  sessionStorage.removeItem(STORAGE_KEY);
  if (countdownInterval) clearInterval(countdownInterval);
  if (socket) socket.close();

  stepCart.classList.add("hidden");
  stepPayment.classList.add("hidden");
  stepSuccess.classList.remove("hidden");

  receiptOrderId.textContent = receiptData.order_id;
  receiptAmount.textContent = `NPR ${parseFloat(receiptData.amount_paid).toFixed(2)}`;
  receiptRef.textContent = receiptData.ref_code;
  receiptBank.textContent = receiptData.bank_name;
}

// 10. Countdown Timer
function startCountdown(expiryEpochMs) {
  if (countdownInterval) clearInterval(countdownInterval);

  function update() {
    const now = Date.now();
    const remaining = expiryEpochMs - now;

    if (remaining <= 0) {
      clearInterval(countdownInterval);
      countdownTimer.textContent = "00:00 (Expired)";
      countdownTimer.style.color = "var(--danger)";
      sessionStorage.removeItem(STORAGE_KEY);
      return;
    }

    const minutes = Math.floor((remaining % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((remaining % (1000 * 60)) / 1000);
    countdownTimer.textContent = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }

  update();
  countdownInterval = setInterval(update, 1000);
}

// 11. Manual Fallback Claim
btnToggleFallback.addEventListener("click", () => {
  fallbackBox.classList.toggle("open");
});

btnClaimFallback.addEventListener("click", async () => {
  if (!currentOrder) return;
  const paidAmt = parseFloat(fallbackAmountInput.value);
  const refCode = fallbackRefInput.value.trim();

  if (!paidAmt || paidAmt <= 0) {
    fallbackMsg.style.color = "var(--danger)";
    fallbackMsg.textContent = "Please enter the amount paid.";
    return;
  }

  btnClaimFallback.disabled = true;
  btnClaimFallback.textContent = "Verifying...";
  fallbackMsg.textContent = "";

  try {
    const res = await fetch(`/api/orders/${currentOrder.id}/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        paid_amount: paidAmt,
        bank_name: currentOrder.bank_name,
        ref_code: refCode || null
      })
    });

    const data = await res.json();
    if (res.ok && data.success) {
      sessionStorage.removeItem(STORAGE_KEY);
      showSuccessStep({
        order_id: currentOrder.id,
        amount_paid: paidAmt,
        ref_code: data.ref_code || "CLAIMED",
        bank_name: currentOrder.bank_name
      });
    } else {
      fallbackMsg.style.color = "var(--danger)";
      fallbackMsg.textContent = data.detail || "No matching transaction record found.";
    }
  } catch (err) {
    fallbackMsg.style.color = "var(--danger)";
    fallbackMsg.textContent = "Request error.";
  } finally {
    btnClaimFallback.disabled = false;
    btnClaimFallback.textContent = "Verify Claim";
  }
});

window.addEventListener("DOMContentLoaded", loadBanks);
