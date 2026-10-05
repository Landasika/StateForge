"""S8:Order lifecycle — create/pay/cancel/ship/refund。"""


class Order:
    def __init__(self, oid):
        self.id = oid
        self.status = "CREATED"
        self.total = 100


ORDERS = {}


def create_order():
    oid = len(ORDERS) + 1
    ORDERS[oid] = Order(oid)
    return ORDERS[oid]


def pay_order(oid):
    order = ORDERS.get(oid)
    if order is None:
        return None
    if order.status != "CREATED":
        return None
    order.status = "PAID"
    return order


def cancel_order(oid):
    order = ORDERS.get(oid)
    if order is None:
        return None
    if order.status != "CREATED":
        return None
    order.status = "CANCELLED"
    return order


def ship_order(oid):
    order = ORDERS.get(oid)
    if order is None:
        return None
    order.status = "SHIPPED"
    return order


def refund_order(oid):
    order = ORDERS.get(oid)
    if order is None:
        return None
    if order.status != "PAID":
        return None
    order.status = "REFUNDED"
    return order
