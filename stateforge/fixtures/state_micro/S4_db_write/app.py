def pay(order, db):
    order.status = "PAID"
    db.session.commit()
    return "paid"
