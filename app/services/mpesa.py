"""Daraja M-Pesa: the STK push and its callback (spec §9).

No SDK, for the same reason ``services/cloudinary.py`` has none. Stripped of
the wrapper, Daraja is three things:

1. Exchange a consumer key and secret for a bearer token, which lasts an hour.
2. POST a JSON body to ``/mpesa/stkpush/v1/processrequest``.
3. Read the JSON Safaricom POSTs back.

That is ``urllib`` and a base64 digest. A package to do it would be a
dependency to audit, pin and update in exchange for nothing.

**This module never touches the database.** It speaks the protocol and returns
plain values; ``services/payments.py`` is what decides an order is paid. Keeping
that line means the amount check, the idempotency constraint and the order
transition are all in one place and are the same code whether the callback came
from Safaricom or from the simulator.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

#: Kenya is UTC+3 all year — no daylight saving, so this is a constant rather
#: than something needing a timezone database.
NAIROBI = timezone(timedelta(hours=3))

from flask import current_app

logger = logging.getLogger(__name__)

#: Daraja tokens last 3599 seconds. Cached per process with a safety margin,
#: because fetching one is a round trip to Safaricom that the customer waits
#: through — on a cold worker that is otherwise added to every first payment.
_TOKEN_MARGIN_SECONDS = 60
_token_cache: dict[str, tuple[float, str]] = {}
_token_lock = Lock()


class MpesaError(RuntimeError):
    """Daraja refused, or could not be reached."""


class MpesaNotConfigured(MpesaError):
    """Credentials needed for a live push are missing."""


@dataclass(frozen=True)
class PushResult:
    """What Safaricom returns when it accepts a push request.

    Acceptance is not payment. It means a prompt is on its way to the handset;
    whether anyone enters a PIN arrives later, on the callback.
    """

    merchant_request_id: str
    checkout_request_id: str
    customer_message: str


@dataclass(frozen=True)
class CallbackResult:
    """A parsed ``stkCallback`` body.

    ``successful`` is ``result_code == 0`` and nothing else — every other code
    is a failure of some kind, and the ones that matter to a customer (they
    cancelled, they had no balance, they let it time out) are distinguished by
    ``result_desc`` rather than by branching on numbers Safaricom may add to.
    """

    merchant_request_id: str
    checkout_request_id: str
    result_code: int
    result_desc: str
    amount: int | None
    receipt: str | None
    phone_number: str | None
    transaction_date: datetime | None

    @property
    def successful(self) -> bool:
        return self.result_code == 0


# --- configuration ----------------------------------------------------------


def mode() -> str:
    return current_app.config["MPESA_MODE"]


def is_simulated() -> bool:
    return mode() == "simulator"


def is_configured() -> bool:
    """Can a payment actually be initiated in this environment?

    The simulator needs nothing; a live push needs the account credentials.
    """
    if is_simulated():
        return True
    config = current_app.config
    return bool(
        config.get("MPESA_CONSUMER_KEY")
        and config.get("MPESA_CONSUMER_SECRET")
        and config.get("MPESA_CALLBACK_URL")
    )


def timestamp(now: datetime | None = None) -> str:
    """Daraja's timestamp format, on Nairobi's clock."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(NAIROBI).strftime("%Y%m%d%H%M%S")


def password(shortcode: str, passkey: str, stamp: str) -> str:
    """base64(shortcode + passkey + timestamp), as Daraja specifies."""
    return base64.b64encode(f"{shortcode}{passkey}{stamp}".encode()).decode()


# --- HTTP -------------------------------------------------------------------


def _request(url: str, *, data: bytes | None, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    timeout = current_app.config["MPESA_TIMEOUT_SECONDS"]

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # Daraja puts the useful part in the error body, so read it before
        # giving up — "Invalid Access Token" and "Bad Request - Invalid
        # PhoneNumber" are both 400s and mean very different things.
        detail = ""
        try:
            detail = error.read().decode()[:500]
        except Exception:  # pragma: no cover - defensive
            pass
        raise MpesaError(f"Daraja returned {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise MpesaError(f"Could not reach Daraja: {error}") from error
    except json.JSONDecodeError as error:
        raise MpesaError("Daraja returned a body that was not JSON.") from error


def access_token() -> str:
    """A bearer token, reused until it is nearly expired."""
    config = current_app.config
    key = config.get("MPESA_CONSUMER_KEY")
    secret = config.get("MPESA_CONSUMER_SECRET")

    if not key or not secret:
        raise MpesaNotConfigured(
            "MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET are required. Register "
            "an app at developer.safaricom.co.ke, or set MPESA_MODE=simulator."
        )

    cache_key = f"{config['MPESA_BASE_URL']}:{key}"
    now = time.monotonic()

    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    payload = _request(
        f"{config['MPESA_BASE_URL']}/oauth/v1/generate?grant_type=client_credentials",
        data=None,
        headers={"Authorization": f"Basic {basic}"},
    )

    token = payload.get("access_token")
    if not token:
        raise MpesaError("Daraja did not return an access token.")

    try:
        lifetime = int(payload.get("expires_in", 3599))
    except (TypeError, ValueError):
        lifetime = 3599

    with _token_lock:
        _token_cache[cache_key] = (now + max(lifetime - _TOKEN_MARGIN_SECONDS, 30), token)

    return token


def stk_push(
    *,
    phone: str,
    amount: int,
    reference: str,
    description: str,
) -> PushResult:
    """Ask Safaricom to put a PIN prompt on ``phone``.

    ``amount`` is whole shillings — Daraja rejects decimals. ``reference`` is
    what the customer sees as the account number on their statement, so it is
    the order number.
    """
    config = current_app.config

    if is_simulated():
        raise MpesaError(
            "stk_push() was called in simulator mode. services/payments.py is "
            "meant to branch before reaching here."
        )

    stamp = timestamp()
    shortcode = config["MPESA_SHORTCODE"]

    body = {
        "BusinessShortCode": shortcode,
        "Password": password(shortcode, config["MPESA_PASSKEY"], stamp),
        "Timestamp": stamp,
        "TransactionType": config["MPESA_TRANSACTION_TYPE"],
        "Amount": amount,
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": config["MPESA_CALLBACK_URL"],
        "AccountReference": reference[:12],
        "TransactionDesc": description[:13],
    }

    payload = _request(
        f"{config['MPESA_BASE_URL']}/mpesa/stkpush/v1/processrequest",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json",
        },
    )

    # Daraja answers a rejected push with 200 and a non-zero ResponseCode, so
    # the HTTP status is not the thing to check.
    if str(payload.get("ResponseCode", "")) != "0":
        raise MpesaError(
            payload.get("ResponseDescription")
            or payload.get("errorMessage")
            or "Daraja declined the payment request."
        )

    return PushResult(
        merchant_request_id=str(payload.get("MerchantRequestID", "")),
        checkout_request_id=str(payload.get("CheckoutRequestID", "")),
        customer_message=str(
            payload.get("CustomerMessage", "A payment request has been sent.")
        ),
    )


# --- callback ---------------------------------------------------------------


def _metadata(callback: dict[str, Any]) -> dict[str, Any]:
    """Flatten ``CallbackMetadata.Item`` — a list of name/value pairs — into a
    dict. Absent entirely on a failed payment, which is not an error."""
    items = (callback.get("CallbackMetadata") or {}).get("Item") or []
    found: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict) and "Name" in item:
            found[item["Name"]] = item.get("Value")
    return found


def _transaction_date(raw: Any) -> datetime | None:
    """Daraja sends ``20191219102115`` as a number, in Nairobi time."""
    if raw is None:
        return None
    try:
        naive = datetime.strptime(str(int(raw)), "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=NAIROBI)


def parse_callback(body: Any) -> CallbackResult:
    """Read the ``Body.stkCallback`` envelope Safaricom POSTs back.

    Deliberately tolerant about *shape* and strict about *meaning*: this is an
    unauthenticated public endpoint, so it is handed whatever arrives. Anything
    it cannot read raises, and the caller answers 200 regardless so Safaricom
    stops retrying — but nothing gets marked paid.
    """
    if not isinstance(body, dict):
        raise MpesaError("Callback body was not a JSON object.")

    callback = (body.get("Body") or {}).get("stkCallback")
    if not isinstance(callback, dict):
        raise MpesaError("Callback body had no Body.stkCallback.")

    checkout_request_id = callback.get("CheckoutRequestID")
    if not checkout_request_id:
        raise MpesaError("Callback had no CheckoutRequestID.")

    try:
        result_code = int(callback.get("ResultCode"))
    except (TypeError, ValueError):
        raise MpesaError("Callback had no usable ResultCode.") from None

    values = _metadata(callback)

    amount = values.get("Amount")
    try:
        amount = int(float(amount)) if amount is not None else None
    except (TypeError, ValueError):
        amount = None

    phone = values.get("PhoneNumber")

    return CallbackResult(
        merchant_request_id=str(callback.get("MerchantRequestID", "")),
        checkout_request_id=str(checkout_request_id),
        result_code=result_code,
        result_desc=str(callback.get("ResultDesc", ""))[:255],
        amount=amount,
        receipt=(str(values["MpesaReceiptNumber"]) if values.get("MpesaReceiptNumber") else None),
        phone_number=str(phone) if phone else None,
        transaction_date=_transaction_date(values.get("TransactionDate")),
    )


def simulated_callback(
    *,
    checkout_request_id: str,
    merchant_request_id: str,
    amount: int,
    phone: str,
    receipt: str,
) -> dict[str, Any]:
    """A successful Daraja callback body, built by hand.

    Returned as the raw envelope rather than a ``CallbackResult`` on purpose:
    the simulator then goes through ``parse_callback`` and the same handler as
    Safaricom, so the parsing is exercised too and there is no second code path
    that only the demo uses.
    """
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": merchant_request_id,
                "CheckoutRequestID": checkout_request_id,
                "ResultCode": 0,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": amount},
                        {"Name": "MpesaReceiptNumber", "Value": receipt},
                        {"Name": "TransactionDate", "Value": int(timestamp())},
                        {"Name": "PhoneNumber", "Value": int(phone)},
                    ]
                },
            }
        }
    }
