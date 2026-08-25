"""
================================================================================
  eSewa Payment Gateway Bridge (Educational & Testing Proof-of-Concept)
================================================================================
  DISCLAIMER:
  This open-source project is strictly intended for educational, research,
  and developer testing purposes. It is an independent proof-of-concept
  demonstrating email-based receipt parsing via standard IMAP protocols.

  NOT AFFILIATED WITH ESEWA LTD. OR F1SOFT INTERNATIONAL.
================================================================================
"""

import os
import re
import hmac
import json
import time
import uuid
import email
import random
import imaplib
import logging
import hashlib
import asyncio
import secrets
from pathlib import Path
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Set, Callable, Tuple
from contextlib import asynccontextmanager

import aiosqlite
import uvicorn
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Load .env if present
load_dotenv()

# Configure structured logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("EsewaGateway")

# Standard Nepali Banks Mapping
BANK_NORMALIZATIONS = {
    "GLOBAL IME": "Global IME Bank",
    "NIC ASIA": "NIC Asia Bank",
    "NABIL": "Nabil Bank",
    "SANIMA": "Sanima Bank",
    "PRABHU": "Prabhu Bank",
    "HIMALAYAN": "Himalayan Bank",
    "EVEREST": "Everest Bank",
    "KUMARI": "Kumari Bank",
    "NEPAL INVESTMENT MEGA": "NIMB Bank",
    "NIMB": "NIMB Bank",
    "PRIME COMMERCIAL": "Prime Commercial Bank",
    "LAXMI SUNRISE": "Laxmi Sunrise Bank",
    "MACHHAPUCHCHHRE": "Machhapuchchhre Bank",
    "CITIZENS": "Citizens Bank",
    "SIDDHARTHA": "Siddhartha Bank",
    "STANDARD CHARTERED": "Standard Chartered Bank",
    "AGRICULTURAL DEVELOPMENT": "ADBL",
    "NEPAL BANK": "Nepal Bank",
    "RASTRIYA BANIJYA": "RBB",
    "ESEWA": "eSewa Wallet",
    "FONEPAY": "Fonepay / Interoperable QR"
}

SUPPORTED_BANKS = [
    {"id": "esewa", "name": "eSewa Direct Wallet", "code": "ESEWA"},
    {"id": "global_ime", "name": "Global IME Bank", "code": "GLOBAL IME"},
    {"id": "nic_asia", "name": "NIC Asia Bank", "code": "NIC ASIA"},
    {"id": "nabil", "name": "Nabil Bank", "code": "NABIL"},
    {"id": "sanima", "name": "Sanima Bank", "code": "SANIMA"},
    {"id": "prabhu", "name": "Prabhu Bank", "code": "PRABHU"},
    {"id": "himalayan", "name": "Himalayan Bank", "code": "HIMALAYAN"},
    {"id": "everest", "name": "Everest Bank", "code": "EVEREST"},
    {"id": "kumari", "name": "Kumari Bank", "code": "KUMARI"},
    {"id": "nimb", "name": "Nepal Investment Mega Bank (NIMB)", "code": "NIMB"},
    {"id": "prime", "name": "Prime Commercial Bank", "code": "PRIME"},
    {"id": "laxmi_sunrise", "name": "Laxmi Sunrise Bank", "code": "LAXMI SUNRISE"},
    {"id": "machhapuchchhre", "name": "Machhapuchchhre Bank", "code": "MACHHAPUCHCHHRE"},
    {"id": "citizens", "name": "Citizens Bank", "code": "CITIZENS"},
    {"id": "siddhartha", "name": "Siddhartha Bank", "code": "SIDDHARTHA"},
    {"id": "standard_chartered", "name": "Standard Chartered Bank", "code": "STANDARD CHARTERED"},
    {"id": "adbl", "name": "Agricultural Development Bank (ADBL)", "code": "ADBL"},
    {"id": "nepal_bank", "name": "Nepal Bank Limited", "code": "NEPAL BANK"},
    {"id": "rbb", "name": "Rastriya Banijya Bank (RBB)", "code": "RBB"},
    {"id": "other", "name": "Other Mobile Banking / Fonepay", "code": "OTHER"}
]


# ==========================================
# 1. PARSER ENGINE
# ==========================================

def normalize_bank_name(raw_name: Optional[str]) -> str:
    if not raw_name:
        return "Unknown / Bank"
    clean = raw_name.upper().strip()
    for key, normalized in BANK_NORMALIZATIONS.items():
        if key in clean:
            return normalized
    clean = re.sub(r'\b(LTD\.?|LIMITED|BANK)\b', '', clean, flags=re.IGNORECASE).strip()
    return f"{clean.title()} Bank" if clean else "Bank"

def extract_text_from_html(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for br in soup.find_all(["br", "p", "tr", "div"]):
        br.append("\n")
    for td in soup.find_all(["td", "th"]):
        td.append(" ")
    return soup.get_text()

def parse_esewa_email(subject: str, body: str, is_html: bool = False) -> Optional[Dict[str, Any]]:
    if not body:
        return None

    raw_text = extract_text_from_html(body) if is_html or "<html" in body.lower() or "<table" in body.lower() else body

    # Extract Reference Code
    ref_code = None
    url_match = re.search(r'transaction\/([A-Z0-9]{4,15})', raw_text, re.IGNORECASE)
    if url_match:
        ref_code = url_match.group(1).strip().upper()

    if not ref_code:
        kv_match = re.search(r'(?:Reference\s*Code|Ref\.?\s*Code|Transaction\s*ID|Txn\s*ID)\s*:\s*([A-Z0-9]+)', raw_text, re.IGNORECASE)
        if kv_match:
            candidate = kv_match.group(1).strip().upper()
            if candidate != "TRANSACTION" and len(candidate) >= 4:
                ref_code = candidate

    if not ref_code:
        tokens = re.findall(r'\b(?=[A-Z0-9]*[0-9])(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,12}\b', raw_text, re.IGNORECASE)
        if tokens:
            ref_code = tokens[0].upper()

    # Extract Amount
    amount = 0.0
    amt_kv_match = re.search(
        r'(?:Transaction\s*Amount\s*(?:\(NPR\))?|Amount\s*(?:\(NPR\)|NPR|Rs\.?)?)\s*:\s*(?:NPR|Rs\.?)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)',
        raw_text,
        re.IGNORECASE
    )
    if amt_kv_match:
        try:
            amount = float(amt_kv_match.group(1).replace(",", "").strip())
        except ValueError:
            amount = 0.0

    if amount <= 0 and ref_code:
        parts = raw_text.split(ref_code)
        if len(parts) > 1:
            after_ref = parts[1]
            num_match = re.search(r'^\s*([0-9]+(?:\.[0-9]+)?)', after_ref, re.MULTILINE)
            if num_match:
                try:
                    amount = float(num_match.group(1))
                except ValueError:
                    pass

    # Extract Bank Name
    bank_name_raw = None
    for bank_key in BANK_NORMALIZATIONS.keys():
        if bank_key in raw_text.upper():
            bank_name_raw = bank_key
            break
    normalized_bank = normalize_bank_name(bank_name_raw)

    # Extract Time & Remarks
    tx_time = None
    time_match = re.search(r'([0-9]{1,2}\s+[A-Za-z]{3}\s+[0-9]{4}[^,\n\r]*,\s*[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\s*(?:AM|PM)?)', raw_text, re.IGNORECASE)
    if time_match:
        tx_time = time_match.group(1).strip()

    remarks = None
    remarks_match = re.search(r'(?:Remarks|Purpose|Description)\s*[:\-]?\s*([A-Za-z0-9\-\_]+)', raw_text, re.IGNORECASE)
    if remarks_match:
        remarks = remarks_match.group(1).strip()

    if not ref_code or amount <= 0:
        return None

    return {
        "ref_code": ref_code,
        "amount": round(amount, 2),
        "bank_name": normalized_bank,
        "transaction_time": tx_time,
        "remarks": remarks,
        "raw_subject": subject,
        "raw_body": raw_text[:1000]
    }


# ==========================================
# 2. IN-MEMORY RATE LIMITER
# ==========================================

class SimpleRateLimiter:
    """Sliding-window in-memory rate limiter per client IP."""
    def __init__(self, requests_per_minute: int = 60):
        self.rpm = requests_per_minute
        self.clients: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, ip: str) -> bool:
        now = time.time()
        window_start = now - 60.0
        async with self._lock:
            if ip not in self.clients:
                self.clients[ip] = [now]
                return True
            # Evict timestamps older than 60 seconds
            self.clients[ip] = [t for t in self.clients[ip] if t > window_start]
            if len(self.clients[ip]) < self.rpm:
                self.clients[ip].append(now)
                return True
            return False


# ==========================================
# 3. MAIN GATEWAY ENGINE
# ==========================================

class EsewaGateway:
    def __init__(
        self,
        gmail_email: Optional[str] = None,
        gmail_app_password: Optional[str] = None,
        esewa_id: Optional[str] = None,
        esewa_name: Optional[str] = None,
        admin_password: Optional[str] = None,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        database_path: Optional[str] = None,
        order_expiry_minutes: int = 10,
        discount_min_offset: float = 0.01,
        discount_max_offset: float = 0.50,
        imap_server: str = "imap.gmail.com",
        imap_port: int = 993,
        poll_interval: int = 3
    ):
        self.gmail_email = (gmail_email or os.getenv("GMAIL_EMAIL", "")).strip()
        self.gmail_app_password = (gmail_app_password or os.getenv("GMAIL_APP_PASSWORD", "")).strip()
        self.esewa_id = (esewa_id or os.getenv("ESEWA_ID", "")).strip()
        self.esewa_name = (esewa_name or os.getenv("ESEWA_NAME", "")).strip()
        self.admin_password = (admin_password or os.getenv("ADMIN_PASSWORD", "")).strip()
        self.webhook_url = (webhook_url or os.getenv("WEBHOOK_URL", "")).strip()
        self.webhook_secret = (webhook_secret or os.getenv("WEBHOOK_SECRET", "")).strip()
        self.database_path = database_path or os.getenv("DATABASE_PATH", str(Path.cwd() / "payment_gateway.db"))
        self.order_expiry_minutes = order_expiry_minutes
        self.discount_min_offset = discount_min_offset
        self.discount_max_offset = discount_max_offset
        self.imap_server = imap_server
        self.imap_port = imap_port
        self.poll_interval = poll_interval

        self._check_configuration()

        self.is_running = False
        self._listener_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._active_ws_connections: Dict[str, Set[WebSocket]] = {}
        self._payment_callbacks: List[Callable] = []

        self.rate_limiter = SimpleRateLimiter(requests_per_minute=60)
        self.router = APIRouter()
        self._setup_routes()

    def _check_configuration(self):
        missing = []
        if not self.esewa_id:
            missing.append("ESEWA_ID")
        if not self.esewa_name:
            missing.append("ESEWA_NAME")
        if not self.gmail_email:
            missing.append("GMAIL_EMAIL")
        if not self.gmail_app_password:
            missing.append("GMAIL_APP_PASSWORD")
        if not self.admin_password:
            missing.append("ADMIN_PASSWORD")

        if missing:
            logger.warning("=" * 60)
            logger.warning("[CONFIGURATION ALERT] Missing environment variables:")
            for var in missing:
                logger.warning(f"  - {var} is not set in .env")
            logger.warning("Please copy .env.example to .env and configure your details.")
            logger.warning("Running in simulation mode.")
            logger.warning("=" * 60)

    def on_payment_success(self, callback: Callable):
        self._payment_callbacks.append(callback)
        return callback

    def get_db(self):
        return aiosqlite.connect(self.database_path)

    async def init_database(self):
        """Initializes SQLite tables with WAL mode and high-concurrency PRAGMAs."""
        async with self.get_db() as conn:
            # WAL mode for non-blocking concurrent reads & writes
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.execute("PRAGMA synchronous=NORMAL;")

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                base_amount REAL NOT NULL,
                target_amount REAL NOT NULL,
                discount_amount REAL NOT NULL,
                bank_name TEXT NOT NULL,
                customer_name TEXT,
                customer_email TEXT,
                item_description TEXT,
                ip_address TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                matched_ref_code TEXT,
                matched_at TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """)

            # Safe column migration for existing databases
            try:
                await conn.execute("ALTER TABLE orders ADD COLUMN ip_address TEXT;")
            except Exception:
                pass
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_code TEXT UNIQUE,
                amount REAL NOT NULL,
                bank_name TEXT,
                transaction_time TEXT,
                remarks TEXT,
                raw_subject TEXT,
                raw_body TEXT,
                matched_order_id TEXT,
                is_matched INTEGER DEFAULT 0,
                processed_at TEXT NOT NULL
            )
            """)

            await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_target ON orders(target_amount);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_txs_ref ON transactions(ref_code);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_txs_amount ON transactions(amount);")
            await conn.commit()
        logger.info(f"Database initialized with WAL mode at {self.database_path}")

    # --- Outbound Webhook Dispatcher ---

    async def _dispatch_webhook(self, payload: Dict[str, Any]):
        if not self.webhook_url:
            return

        body = json.dumps(payload, sort_keys=True)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "EsewaGateway-Webhook/1.0"
        }

        if self.webhook_secret:
            signature = hmac.new(
                self.webhook_secret.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            headers["X-Signature-SHA256"] = signature

        # Async retry loop (up to 3 attempts with exponential backoff)
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.post(self.webhook_url, content=body, headers=headers)
                    if res.is_success:
                        logger.info(f"Webhook delivered successfully to {self.webhook_url} (HTTP {res.status_code})")
                        return
                    else:
                        logger.warning(f"Webhook delivery attempt {attempt} failed: HTTP {res.status_code}")
            except Exception as e:
                logger.error(f"Webhook delivery attempt {attempt} exception: {e}")
            await asyncio.sleep(2 ** attempt)

    # --- Order Expiry Background Worker ---

    async def _order_expiry_worker(self):
        """Periodically sweeps expired pending orders to free up target amount slots."""
        while self.is_running:
            try:
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                async with self.get_db() as conn:
                    cursor = await conn.execute(
                        "UPDATE orders SET status = 'EXPIRED' WHERE status = 'PENDING' AND expires_at <= ?",
                        (now_iso,)
                    )
                    if cursor.rowcount > 0:
                        await conn.commit()
                        logger.info(f"Cleaned up {cursor.rowcount} expired pending order(s).")
            except Exception as e:
                logger.error(f"Error in expiry worker: {e}")
            await asyncio.sleep(30)

    # --- Order Management Methods ---

    async def generate_unique_target_amount(self, base_amount: float) -> Tuple[float, float]:
        """
        Generates a collision-free micro-discounted target amount in paisa
        (e.g., NPR 100.00 -> NPR 99.85 with discount NPR 0.15).
        Ensures multiple concurrent orders of the same base amount never collide.
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self.get_db() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT target_amount FROM orders WHERE status = 'PENDING' AND expires_at > ?",
                (now_iso,)
            )
            rows = await cursor.fetchall()
            used_targets = {round(r["target_amount"], 2) for r in rows}

        min_paisa = int(round(self.discount_min_offset * 100))
        max_paisa = int(round(self.discount_max_offset * 100))
        if max_paisa < min_paisa:
            max_paisa = min_paisa

        possible_paisa = list(range(min_paisa, max_paisa + 1))
        random.shuffle(possible_paisa)

        for paisa in possible_paisa:
            discount = round(paisa / 100.0, 2)
            target = round(base_amount - discount, 2)
            if target > 0 and target not in used_targets:
                return target, discount

        # Fallback: find any free 2-decimal paisa offset between 0.01 and 0.99
        for extra in range(1, 100):
            discount = round(extra / 100.0, 2)
            target = round(base_amount - discount, 2)
            if target > 0 and target not in used_targets:
                return target, discount

        return round(base_amount, 2), 0.0

    async def create_order(
        self,
        base_amount: float,
        bank_name: str,
        customer_name: Optional[str] = "Customer",
        customer_email: Optional[str] = "",
        item_description: Optional[str] = "Purchase",
        ip_address: Optional[str] = "Unknown"
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=self.order_expiry_minutes)
        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"

        target_amount, discount_amount = await self.generate_unique_target_amount(base_amount)

        order_data = {
            "id": order_id,
            "base_amount": round(base_amount, 2),
            "target_amount": target_amount,
            "discount_amount": discount_amount,
            "bank_name": bank_name,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "item_description": item_description,
            "ip_address": ip_address,
            "status": "PENDING",
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        }

        async with self.get_db() as conn:
            await conn.execute("""
            INSERT INTO orders (
                id, base_amount, target_amount, discount_amount, bank_name,
                customer_name, customer_email, item_description, ip_address, status,
                created_at, expires_at
            ) VALUES (
                :id, :base_amount, :target_amount, :discount_amount, :bank_name,
                :customer_name, :customer_email, :item_description, :ip_address, :status,
                :created_at, :expires_at
            )
            """, order_data)
            await conn.commit()

        qr_payload = json.dumps({
            "eSewa_id": self.esewa_id,
            "name": self.esewa_name,
            "amount": target_amount,
            "remarks": order_id
        })
        return {
            **order_data,
            "esewa_id": self.esewa_id,
            "esewa_name": self.esewa_name,
            "qr_payload": qr_payload,
            "expires_in_seconds": self.order_expiry_minutes * 60
        }

    async def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        async with self.get_db() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            qr_payload = json.dumps({
                "eSewa_id": self.esewa_id,
                "name": self.esewa_name,
                "amount": data["target_amount"],
                "remarks": data["id"]
            })
            return {
                **data,
                "esewa_id": self.esewa_id,
                "esewa_name": self.esewa_name,
                "qr_payload": qr_payload,
                "expires_in_seconds": self.order_expiry_minutes * 60
            }

    async def match_transaction(self, parsed_tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ref_code = parsed_tx.get("ref_code")
        amount = parsed_tx.get("amount", 0.0)
        bank_name = parsed_tx.get("bank_name", "")
        remarks = parsed_tx.get("remarks") or ""

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self.get_db() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM orders WHERE status = 'PENDING' AND expires_at > ?",
                (now_iso,)
            )
            active_orders = [dict(r) for r in await cursor.fetchall()]

        matched_order = None
        if remarks:
            for order in active_orders:
                if order["id"].upper() in remarks.upper():
                    matched_order = order
                    break

        if not matched_order:
            for order in active_orders:
                if abs(order["target_amount"] - amount) < 0.001:
                    matched_order = order
                    break

        if not matched_order:
            for order in active_orders:
                if abs(order["base_amount"] - amount) < 0.001:
                    if bank_name and order.get("bank_name") and (
                        bank_name.lower() in order["bank_name"].lower() or 
                        order["bank_name"].lower() in bank_name.lower()
                    ):
                        matched_order = order
                        break

        # Record in DB
        async with self.get_db() as conn:
            try:
                await conn.execute("""
                INSERT INTO transactions (
                    ref_code, amount, bank_name, transaction_time, remarks,
                    raw_subject, raw_body, matched_order_id, is_matched, processed_at
                ) VALUES (
                    :ref_code, :amount, :bank_name, :transaction_time, :remarks,
                    :raw_subject, :raw_body, :matched_order_id, :is_matched, :processed_at
                )
                """, {
                    "ref_code": ref_code,
                    "amount": amount,
                    "bank_name": bank_name,
                    "transaction_time": parsed_tx.get("transaction_time"),
                    "remarks": remarks,
                    "raw_subject": parsed_tx.get("raw_subject", ""),
                    "raw_body": parsed_tx.get("raw_body", ""),
                    "matched_order_id": matched_order["id"] if matched_order else None,
                    "is_matched": 1 if matched_order else 0,
                    "processed_at": now_iso
                })
                await conn.commit()
            except Exception:
                pass

        if matched_order:
            order_id = matched_order["id"]
            logger.info(f"Payment matched: Order {order_id}, Ref {ref_code}, Amount NPR {amount}")

            async with self.get_db() as conn:
                await conn.execute(
                    "UPDATE orders SET status = 'PAID', matched_ref_code = ?, matched_at = ? WHERE id = ?",
                    (ref_code, now_iso, order_id)
                )
                await conn.commit()

            # Push live update to WebSockets
            payload = {
                "type": "PAYMENT_SUCCESS",
                "order_id": order_id,
                "status": "PAID",
                "ref_code": ref_code,
                "amount_paid": amount,
                "bank_name": bank_name
            }
            await self._broadcast_ws(order_id, payload)

            # Dispatch outbound webhook asynchronously
            asyncio.create_task(self._dispatch_webhook({
                "event": "payment.success",
                "order_id": order_id,
                "status": "PAID",
                "amount": amount,
                "ref_code": ref_code,
                "bank_name": bank_name,
                "timestamp": now_iso
            }))

            # Fire internal python callbacks
            for cb in self._payment_callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(order_id, amount, ref_code, bank_name)
                    else:
                        cb(order_id, amount, ref_code, bank_name)
                except Exception as e:
                    logger.error(f"Error in payment callback: {e}")

            return matched_order

        return None

    async def claim_fallback(self, order_id: str, paid_amount: float, bank_name: Optional[str] = None, ref_code: Optional[str] = None):
        order = await self.get_order(order_id)
        if not order:
            return {"success": False, "message": "Order not found"}
        if order["status"] == "PAID":
            return {"success": True, "message": "Order is already PAID"}

        async with self.get_db() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM transactions WHERE is_matched = 0 ORDER BY id DESC LIMIT 50")
            unmatched = [dict(r) for r in await cursor.fetchall()]

        candidate = None
        if ref_code:
            for tx in unmatched:
                if tx.get("ref_code") and tx["ref_code"].strip().upper() == ref_code.strip().upper():
                    candidate = tx
                    break

        if not candidate:
            for tx in unmatched:
                if (abs(tx["amount"] - paid_amount) < 0.01 or 
                    abs(tx["amount"] - order["target_amount"]) < 0.01 or 
                    abs(tx["amount"] - order["base_amount"]) < 0.01):
                    if bank_name and tx.get("bank_name"):
                        if bank_name.lower() in tx["bank_name"].lower() or tx["bank_name"].lower() in bank_name.lower():
                            candidate = tx
                            break
                    else:
                        candidate = tx
                        break

        if candidate:
            ref = candidate["ref_code"]
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            async with self.get_db() as conn:
                await conn.execute("UPDATE orders SET status = 'PAID', matched_ref_code = ?, matched_at = ? WHERE id = ?", (ref, now_iso, order_id))
                await conn.execute("UPDATE transactions SET is_matched = 1, matched_order_id = ? WHERE id = ?", (order_id, candidate["id"]))
                await conn.commit()

            payload = {
                "type": "PAYMENT_SUCCESS",
                "order_id": order_id,
                "status": "PAID",
                "ref_code": ref,
                "amount_paid": candidate["amount"],
                "bank_name": candidate.get("bank_name", "Bank")
            }
            await self._broadcast_ws(order_id, payload)

            asyncio.create_task(self._dispatch_webhook({
                "event": "payment.success",
                "order_id": order_id,
                "status": "PAID",
                "amount": candidate["amount"],
                "ref_code": ref,
                "bank_name": candidate.get("bank_name", "Bank"),
                "timestamp": now_iso
            }))

            return {"success": True, "message": "Payment verified!", "ref_code": ref}

        return {"success": False, "message": "No matching incoming transaction found yet."}

    # --- WebSocket Helpers ---

    async def connect_ws(self, order_id: str, websocket: WebSocket):
        await websocket.accept()
        if order_id not in self._active_ws_connections:
            self._active_ws_connections[order_id] = set()
        self._active_ws_connections[order_id].add(websocket)

    def disconnect_ws(self, order_id: str, websocket: WebSocket):
        if order_id in self._active_ws_connections:
            self._active_ws_connections[order_id].discard(websocket)
            if not self._active_ws_connections[order_id]:
                del self._active_ws_connections[order_id]

    async def _broadcast_ws(self, order_id: str, data: Dict[str, Any]):
        if order_id in self._active_ws_connections:
            for ws in list(self._active_ws_connections[order_id]):
                try:
                    await ws.send_json(data)
                except Exception:
                    self._active_ws_connections[order_id].discard(ws)

    # --- IMAP Listener Engine ---

    def _fetch_emails_sync(self, mail: imaplib.IMAP4_SSL):
        mail.select("INBOX")
        status, messages = mail.search(None, '(UNSEEN FROM "donotreply@esewa.com.np")')
        if status != "OK" or not messages[0]:
            status, messages = mail.search(None, '(FROM "donotreply@esewa.com.np")')
        if status != "OK" or not messages[0]:
            return []

        email_ids = messages[0].split()
        latest_ids = email_ids[-10:]
        parsed_list = []

        for msg_id in reversed(latest_ids):
            try:
                res, msg_data = mail.fetch(msg_id, "(RFC822)")
                if res != "OK":
                    continue
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        raw_email = response_part[1]
                        msg = email.message_from_bytes(raw_email)
                        
                        subject_header = msg.get("Subject", "")
                        decoded_sub = "".join(
                            t.decode(enc or "utf-8", errors="ignore") if isinstance(t, bytes) else str(t)
                            for t, enc in decode_header(subject_header)
                        )

                        body = ""
                        is_html = False
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/html":
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                                        is_html = True
                                        break
                                elif part.get_content_type() == "text/plain" and not body:
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                        else:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
                                is_html = msg.get_content_type() == "text/html"

                        parsed = parse_esewa_email(subject=decoded_sub, body=body, is_html=is_html)
                        if parsed:
                            parsed_list.append(parsed)
            except Exception as e:
                logger.error(f"Error reading email ID {msg_id}: {e}")

        return parsed_list

    async def _listener_loop(self):
        if not self.gmail_email or not self.gmail_app_password:
            logger.warning("Gmail credentials not provided. EsewaGateway running in SIMULATION mode.")
            return

        logger.info(f"Connecting to IMAP {self.imap_server} as {self.gmail_email}...")
        while self.is_running:
            mail: Optional[imaplib.IMAP4_SSL] = None
            try:
                mail = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
                mail.login(self.gmail_email, self.gmail_app_password)
                logger.info("Connected to Gmail IMAP successfully.")

                while self.is_running:
                    txs = await asyncio.to_thread(self._fetch_emails_sync, mail)
                    for tx in txs:
                        await self.match_transaction(tx)
                    await asyncio.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"IMAP error: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)
            finally:
                if mail:
                    try:
                        mail.close()
                        mail.logout()
                    except Exception:
                        pass

    def start(self):
        if not self.is_running:
            self.is_running = True
            self._listener_task = asyncio.create_task(self._listener_loop())
            self._cleanup_task = asyncio.create_task(self._order_expiry_worker())
            logger.info("EsewaGateway background services started.")

    def stop(self):
        self.is_running = False
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        logger.info("EsewaGateway background services stopped.")

    # --- Routes Definition ---

    def _setup_routes(self):
        router = self.router

        class OrderCreateReq(BaseModel):
            base_amount: float = Field(..., gt=0, le=1000000, description="Order amount in NPR")
            bank_name: str = Field(..., min_length=2, max_length=100)
            customer_name: Optional[str] = Field("Customer", max_length=100)
            customer_email: Optional[str] = Field("", max_length=100)
            item_description: Optional[str] = Field("Purchase", max_length=200)

        class FallbackReq(BaseModel):
            paid_amount: float = Field(..., gt=0)
            bank_name: Optional[str] = Field(None, max_length=100)
            ref_code: Optional[str] = Field(None, max_length=30)

        # Admin Authentication Helper
        def verify_admin_auth(request: Request):
            if not self.admin_password:
                return True
            
            # Check X-Admin-Password header
            auth_header = request.headers.get("X-Admin-Password")
            if auth_header and secrets.compare_digest(auth_header, self.admin_password):
                return True

            # Check Authorization: Basic
            auth_basic = request.headers.get("Authorization")
            if auth_basic and auth_basic.startswith("Basic "):
                import base64
                try:
                    decoded = base64.b64decode(auth_basic[6:]).decode("utf-8")
                    if ":" in decoded:
                        _, password = decoded.split(":", 1)
                        if secrets.compare_digest(password, self.admin_password):
                            return True
                except Exception:
                    pass

            raise HTTPException(status_code=401, detail="Unauthorized: Invalid Admin Password")

        @router.get("/banks")
        async def api_banks():
            return {"banks": SUPPORTED_BANKS}

        @router.post("/orders")
        async def api_create_order(req: OrderCreateReq, request: Request):
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
            else:
                client_ip = request.client.host if request.client else "Unknown"

            if not await self.rate_limiter.is_allowed(client_ip):
                raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a moment.")

            order = await self.create_order(
                base_amount=req.base_amount,
                bank_name=req.bank_name,
                customer_name=req.customer_name,
                customer_email=req.customer_email,
                item_description=req.item_description,
                ip_address=client_ip
            )
            return {"success": True, "order": order}

        @router.get("/orders/{order_id}")
        async def api_get_order(order_id: str):
            order = await self.get_order(order_id)
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")
            return {"success": True, "order": order}

        @router.post("/orders/{order_id}/claim")
        async def api_claim_order(order_id: str, req: FallbackReq, request: Request):
            client_ip = request.client.host if request.client else "unknown"
            if not await self.rate_limiter.is_allowed(client_ip):
                raise HTTPException(status_code=429, detail="Too many claim attempts. Please wait a moment.")

            res = await self.claim_fallback(order_id, req.paid_amount, req.bank_name, req.ref_code)
            if not res.get("success"):
                raise HTTPException(status_code=400, detail=res.get("message"))
            return res

        @router.post("/admin/login")
        async def api_admin_login(request: Request):
            verify_admin_auth(request)
            return {"success": True, "message": "Authenticated"}

        @router.get("/admin/orders")
        async def api_admin_orders(request: Request):
            verify_admin_auth(request)
            async with self.get_db() as conn:
                conn.row_factory = aiosqlite.Row
                c = await conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50")
                return {"orders": [dict(r) for r in await c.fetchall()]}

        @router.get("/admin/transactions")
        async def api_admin_transactions(request: Request):
            verify_admin_auth(request)
            async with self.get_db() as conn:
                conn.row_factory = aiosqlite.Row
                c = await conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 50")
                return {"transactions": [dict(r) for r in await c.fetchall()]}

        @router.get("/health")
        async def api_health():
            missing = []
            if not self.esewa_id:
                missing.append("ESEWA_ID")
            if not self.esewa_name:
                missing.append("ESEWA_NAME")
            if not self.gmail_email or not self.gmail_app_password:
                missing.append("GMAIL_CREDENTIALS")
            if not self.admin_password:
                missing.append("ADMIN_PASSWORD")

            return {
                "status": "online",
                "listener_running": self.is_running,
                "configured": len(missing) == 0,
                "missing_variables": missing,
                "esewa_id": self.esewa_id if self.esewa_id else "Not Set",
                "esewa_name": self.esewa_name if self.esewa_name else "Not Set",
                "database": self.database_path
            }

        @router.websocket("/ws/orders/{order_id}")
        async def ws_order(websocket: WebSocket, order_id: str):
            await self.connect_ws(order_id, websocket)
            try:
                order = await self.get_order(order_id)
                if order:
                    await websocket.send_json({
                        "type": "INITIAL_STATUS",
                        "order_id": order_id,
                        "status": order["status"],
                        "target_amount": order["target_amount"]
                    })
                while True:
                    msg = await websocket.receive_text()
                    if msg == "ping":
                        await websocket.send_text("pong")
            except WebSocketDisconnect:
                self.disconnect_ws(order_id, websocket)
            except Exception:
                self.disconnect_ws(order_id, websocket)


# ==========================================
# 4. SECURITY HEADERS MIDDLEWARE
# ==========================================

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# ==========================================
# 5. STANDALONE FASTAPI APP FACTORY
# ==========================================

def create_app(gateway: Optional[EsewaGateway] = None) -> FastAPI:
    gw = gateway or EsewaGateway()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await gw.init_database()
        gw.start()
        yield
        gw.stop()

    app = FastAPI(
        title="eSewa Payment Gateway Bridge",
        description="Production-grade eSewa automated payment bridge via Gmail IMAP",
        version="1.0.0",
        lifespan=lifespan
    )

    # Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"]
    )

    # Mount API routes
    app.include_router(gw.router, prefix="/api")
    app.include_router(gw.router)

    # Static Frontend
    frontend_dir = Path(__file__).resolve().parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/")
        async def serve_index():
            return FileResponse(frontend_dir / "index.html")

        @app.get("/admin")
        async def serve_admin():
            return FileResponse(frontend_dir / "admin.html")

    return app


# Default app instance
gateway = EsewaGateway()
app = create_app(gateway)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print("=" * 60)
    print("  eSewa Payment Gateway Bridge")
    print(f"  Checkout UI:        http://localhost:{port}/")
    print(f"  Admin Console:      http://localhost:{port}/admin")
    print(f"  Interactive Docs:   http://localhost:{port}/docs")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=port)
