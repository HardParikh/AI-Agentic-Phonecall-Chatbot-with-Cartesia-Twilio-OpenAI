from datetime import timedelta, datetime

def seed_db():
    from app.db import SessionLocal, Barber, Service, BarberService, Availability
    session = SessionLocal()
    if session.query(Barber).count() > 0:
        session.close(); return

    # Barbers
    a = Barber(name="Alex")
    b = Barber(name="Brook")
    c = Barber(name="Casey")
    session.add_all([a, b, c])

    # Services (fixed catalog)
    svc = [
        Service(code="HAIRCUT", name="Haircut", duration_min=30, price_cents=2500),
        Service(code="BEARD", name="Beard Trim", duration_min=15, price_cents=1500),
        Service(code="SHAVE", name="Hot Towel Shave", duration_min=25, price_cents=2200),
        Service(code="KIDS", name="Kids Haircut", duration_min=25, price_cents=2000),
        Service(code="STYLE", name="Wash & Style", duration_min=20, price_cents=1800),
        Service(code="COLOR", name="Color Touch‑up", duration_min=45, price_cents=5500),
    ]
    session.add_all(svc)
    session.flush()

    # Map services to barbers (all do haircut; others vary)
    for s in svc:
        session.add_all([
            BarberService(barber_id=a.id, service_id=s.id),
            BarberService(barber_id=b.id, service_id=s.id),
        ])
    # Casey doesn't do color
    for s in [svc[0], svc[1], svc[2], svc[3], svc[4]]:
        session.add(BarberService(barber_id=c.id, service_id=s.id))

    # Simple availability today/tomorrow 9am–5pm in 30‑min slots
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    for day in range(0, 2):
        start_day = now + timedelta(days=day)
        for i in range(16):
            slot_start = start_day + timedelta(minutes=30 * i)
            slot_end = slot_start + timedelta(minutes=30)
            for barber in [a, b, c]:
                session.add(Availability(barber_id=barber.id, start=slot_start, end=slot_end))

    session.commit(); session.close()