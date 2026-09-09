"""M-Pesa payments (spec §9, §24).

The three cases §24 names — duplicate callback, wrong amount, already-paid
order — are the ones where getting it wrong costs real money, so they are
asserted directly rather than inferred from a happy path.

Everything here runs in simulator mode, which is what the test config selects.
That is not a weaker test than one against Daraja would be: the simulator only
replaces the HTTP call that puts a prompt on a handset. The Payment row, the
callback parsing, the amount check, the unique receipt constraint and the order
transition are the same code either way, and they are what these assert.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.extensions import db
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentStatus
from app.services import mpesa, payments as payment_service


@pytest.fixture
def payable(app):
    """An order sitting where checkout leaves one: PAYMENT_PENDING."""
    order = Order(
        order_number="KC-100501",
        confirmation_token="token-for-payment-tests",
        status=OrderStatus.PAYMENT_PENDING,
        customer_name="Achieng",
        customer_phone="254712345678",
        delivery_location="Westlands",
        subtotal=Decimal("8000.00"),
        delivery_fee=Decimal("300.00"),
        total=Decimal("8300.00"),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _callback(payment, *, amount=None, receipt="QGR7XKL2P9", result_code=0):
    """A Daraja callback envelope for ``payment``."""
    if result_code != 0:
        return {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": payment.merchant_request_id,
                    "CheckoutRequestID": payment.checkout_request_id,
                    "ResultCode": result_code,
                    "ResultDesc": "Request cancelled by user",
                }
            }
        }
    return mpesa.simulated_callback(
        checkout_request_id=payment.checkout_request_id,
        merchant_request_id=payment.merchant_request_id,
        amount=amount if amount is not None else int(payment.amount),
        phone=payment.phone_number,
        receipt=receipt,
    )


def _post_callback(client, body):
    return client.post("/api/payments/mpesa/callback", json=body)


# --- initiating -------------------------------------------------------------


def test_paying_an_order_records_a_pending_payment(client, payable):
    response = client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})

    assert response.status_code == 202
    body = response.get_json()["data"]["payment"]
    assert body["status"] == "PENDING"
    assert body["settled"] is False
    # Acceptance is not payment.
    assert body["successful"] is False
    assert db.session.get(Order, payable.id).status == OrderStatus.PAYMENT_PENDING


def test_the_amount_charged_is_the_order_total_not_the_client_s(client, payable):
    """Business rule 2 reaches all the way to the payment: the figure sent to
    M-Pesa comes from the order row, and the request body cannot influence it."""
    client.post(
        f"/api/orders/{payable.confirmation_token}/pay",
        json={"phone": "0712345678"},
    )

    payment = db.session.scalar(db.select(Payment))
    assert payment.amount == Decimal("8300")


def test_a_second_push_while_one_is_pending_is_refused(client, payable):
    """Tapping "Pay" twice must not put two PIN prompts on one handset."""
    first = client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    second = client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.get_json()["error"]["code"] == "PAYMENT_ALREADY_PENDING"
    assert db.session.scalar(db.select(db.func.count()).select_from(Payment)) == 1


def test_an_unknown_token_does_not_reveal_whether_an_order_exists(client):
    response = client.post("/api/orders/not-a-real-token/pay", json={})

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_a_malformed_phone_is_rejected(client, payable):
    response = client.post(
        f"/api/orders/{payable.confirmation_token}/pay",
        json={"phone": "12345"},
    )

    assert response.status_code == 422


def test_the_payer_may_use_a_different_number_than_the_order(client, payable):
    """Paying from another handset is normal — someone's partner settles it."""
    client.post(
        f"/api/orders/{payable.confirmation_token}/pay",
        json={"phone": "0722000111"},
    )

    payment = db.session.scalar(db.select(Payment))
    assert payment.phone_number == "254722000111"
    assert db.session.get(Order, payable.id).customer_phone == "254712345678"


# --- the callback: the only route to PAID -----------------------------------


def test_a_successful_callback_pays_the_order(client, payable):
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    payment = db.session.scalar(db.select(Payment))

    assert _post_callback(client, _callback(payment)).status_code == 200

    db.session.expire_all()
    order = db.session.get(Order, payable.id)
    assert order.status == OrderStatus.PAID
    assert order.paid_at is not None
    assert db.session.get(Payment, payment.id).status == PaymentStatus.SUCCESSFUL
    assert db.session.get(Payment, payment.id).mpesa_receipt_number == "QGR7XKL2P9"


def test_a_duplicate_callback_does_not_pay_twice(client, payable):
    """§24. Daraja does resend, and a second payment row for one order would
    corrupt the books as surely as a second charge would."""
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    payment = db.session.scalar(db.select(Payment))
    body = _callback(payment)

    _post_callback(client, body)
    _post_callback(client, body)
    _post_callback(client, body)

    db.session.expire_all()
    assert db.session.scalar(db.select(db.func.count()).select_from(Payment)) == 1
    assert db.session.get(Order, payable.id).status == OrderStatus.PAID


def test_a_callback_for_the_wrong_amount_never_pays_the_order(client, payable):
    """§24. Business rule 3 is not "a callback arrived", it is "the right
    callback arrived"."""
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    payment = db.session.scalar(db.select(Payment))

    _post_callback(client, _callback(payment, amount=100))

    db.session.expire_all()
    assert db.session.get(Order, payable.id).status != OrderStatus.PAID
    assert db.session.get(Payment, payment.id).status == PaymentStatus.FAILED


def test_an_already_paid_order_is_not_paid_again(client, payable):
    """§24. A late callback for an order that is already settled must not
    reopen or re-stamp it."""
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    payment = db.session.scalar(db.select(Payment))
    _post_callback(client, _callback(payment))

    db.session.expire_all()
    paid_at = db.session.get(Order, payable.id).paid_at

    _post_callback(client, _callback(payment, receipt="DIFFERENT01"))

    db.session.expire_all()
    order = db.session.get(Order, payable.id)
    assert order.status == OrderStatus.PAID
    assert order.paid_at == paid_at
    assert db.session.get(Payment, payment.id).mpesa_receipt_number == "QGR7XKL2P9"


def test_a_failed_callback_marks_the_order_payment_failed(client, payable):
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    payment = db.session.scalar(db.select(Payment))

    _post_callback(client, _callback(payment, result_code=1032))

    db.session.expire_all()
    assert db.session.get(Order, payable.id).status == OrderStatus.PAYMENT_FAILED
    assert db.session.get(Payment, payment.id).status == PaymentStatus.CANCELLED


def test_a_failed_payment_can_be_retried(client, payable):
    """PAYMENT_FAILED must not be a dead end — the customer's card declined,
    not their intent to buy."""
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    payment = db.session.scalar(db.select(Payment))
    _post_callback(client, _callback(payment, result_code=1032))

    retry = client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})

    assert retry.status_code == 202


# --- the callback is public, so it is trusted for nothing -------------------


def test_a_callback_naming_an_unknown_push_is_ignored(client, payable):
    """The CheckoutRequestID is the whole credential. An invented one buys
    nothing."""
    forged = {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "made-up",
                "CheckoutRequestID": "ws_CO_NEVER_ISSUED",
                "ResultCode": 0,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 8300},
                        {"Name": "MpesaReceiptNumber", "Value": "FORGED123"},
                        {"Name": "PhoneNumber", "Value": 254712345678},
                    ]
                },
            }
        }
    }

    assert _post_callback(client, forged).status_code == 200

    db.session.expire_all()
    assert db.session.get(Order, payable.id).status == OrderStatus.PAYMENT_PENDING
    assert db.session.scalar(db.select(db.func.count()).select_from(Payment)) == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Body": {}},
        {"Body": {"stkCallback": {}}},
        {"Body": {"stkCallback": {"CheckoutRequestID": "x"}}},
        [],
        "not json at all",
    ],
)
def test_a_malformed_callback_is_answered_200_and_changes_nothing(
    client, payable, body
):
    """Daraja retries anything that is not a 200, so a bad body must not
    produce a retry storm — but it must not change anything either."""
    assert _post_callback(client, body).status_code == 200

    db.session.expire_all()
    assert db.session.get(Order, payable.id).status == OrderStatus.PAYMENT_PENDING


# --- status polling ---------------------------------------------------------


def test_the_guest_can_poll_their_payment(client, payable):
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})

    body = client.get(f"/api/orders/{payable.confirmation_token}/payment").get_json()

    assert body["data"]["order_status"] == "PAYMENT_PENDING"
    assert body["data"]["payment"]["status"] == "PENDING"


def test_polling_before_any_payment_is_not_an_error(client, payable):
    body = client.get(f"/api/orders/{payable.confirmation_token}/payment").get_json()

    assert body["data"]["payment"] is None


def test_payment_status_never_carries_the_raw_callback(client, payable):
    """Business rule 8: the outcome, not the wire traffic."""
    client.post(f"/api/orders/{payable.confirmation_token}/pay", json={})
    payment = db.session.scalar(db.select(Payment))
    _post_callback(client, _callback(payment))

    body = client.get(f"/api/orders/{payable.confirmation_token}/payment").get_json()
    serialized = body["data"]["payment"]

    assert "raw_callback" not in serialized
    assert "result_desc" not in serialized
    assert serialized["receipt"] == "QGR7XKL2P9"


def test_payment_endpoints_are_never_publicly_cached(client, payable):
    response = client.get(f"/api/orders/{payable.confirmation_token}/payment")

    assert response.headers["Cache-Control"] == "no-store"


# --- the protocol itself ----------------------------------------------------


def test_the_daraja_password_matches_the_documented_recipe(app):
    """base64(shortcode + passkey + timestamp). Getting this wrong is a 400
    from Safaricom with a message that does not say why."""
    import base64

    with app.app_context():
        produced = mpesa.password("174379", "passkey-value", "20260909120000")

    assert base64.b64decode(produced).decode() == "174379passkey-value20260909120000"


def test_amounts_round_up_to_whole_shillings():
    """Daraja rejects decimals, and rounding down would undercharge."""
    assert payment_service.stk_amount(Decimal("8300.00")) == 8300
    assert payment_service.stk_amount(Decimal("8300.01")) == 8301
    assert payment_service.stk_amount(Decimal("8300.99")) == 8301


def test_a_cancelled_callback_is_read_as_cancelled_not_merely_failed(app):
    with app.app_context():
        parsed = mpesa.parse_callback(
            {
                "Body": {
                    "stkCallback": {
                        "MerchantRequestID": "m",
                        "CheckoutRequestID": "c",
                        "ResultCode": 1032,
                        "ResultDesc": "Request cancelled by user",
                    }
                }
            }
        )

    assert parsed.successful is False
    assert parsed.result_code == 1032
    assert parsed.receipt is None


def test_the_simulator_is_refused_in_production(monkeypatch):
    """The one configuration that must be impossible: a live shop that can
    mark orders paid without Safaricom."""
    from app.config import ConfigError, ProductionConfig

    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("JWT_SECRET_KEY", "y" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("CORS_ORIGINS", "https://example.com")
    monkeypatch.setenv("MPESA_MODE", "simulator")

    with pytest.raises(ConfigError, match="MPESA_MODE"):
        ProductionConfig()


# --- what the storefront is told to offer -----------------------------------


def test_payment_methods_reports_mpesa_as_simulated_in_this_environment(client):
    body = client.get("/api/payments/methods").get_json()["data"]

    assert body["mpesa"]["available"] is True
    assert body["mpesa"]["simulated"] is True


def test_whatsapp_is_hidden_when_no_number_is_configured(client, app):
    app.config["WHATSAPP_NUMBER"] = ""

    body = client.get("/api/payments/methods").get_json()["data"]

    assert body["whatsapp"]["available"] is False
    assert body["whatsapp"]["number"] is None


def test_whatsapp_is_offered_when_a_number_is_configured(client, app):
    app.config["WHATSAPP_NUMBER"] = "254712345678"

    body = client.get("/api/payments/methods").get_json()["data"]

    assert body["whatsapp"]["available"] is True
    assert body["whatsapp"]["number"] == "254712345678"
