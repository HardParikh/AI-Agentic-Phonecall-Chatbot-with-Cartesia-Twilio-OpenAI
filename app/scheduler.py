from datetime import timedelta, datetime
from app.db import Availability

def find_slot(session, service_id: int, preferred_time: datetime | None = None) -> tuple[Availability | None, str]:
    from app.db import Availability, Service
    svc = session.get(Service, service_id)
    if not svc:
        return None, "Unknown service"

    q = session.query(Availability).filter(Availability.booked == False)
    if preferred_time:
        q = q.filter(Availability.start >= preferred_time)
    # greedily pick first slot that fits duration (assumes 30‑min increments)
    for avail in q.order_by(Availability.start.asc()).all():
        end_needed = avail.start + timedelta(minutes=svc.duration_min)
        # ensure continuous blocks exist for same barber
        slots = session.query(Availability).filter(
            Availability.barber_id == avail.barber_id,
            Availability.booked == False,
            Availability.start >= avail.start,
            Availability.end <= end_needed
        ).order_by(Availability.start).all()
        if slots and len(slots) * 30 >= svc.duration_min:
            return avail, "OK"
    return None, "No matching availability"

def book(session, customer_name: str, phone: str, barber_id: int, service_id: int, start: datetime):
    from app.db import Availability, Appointment, Service
    svc = session.get(Service, service_id)
    end = start + timedelta(minutes=svc.duration_min)
    appt = Appointment(customer_name=customer_name, phone=phone, barber_id=barber_id,
                       service_id=service_id, start=start, end=end)
    session.add(appt)
    # mark slots booked
    slots = session.query(Availability).filter(
        Availability.barber_id == barber_id,
        Availability.start >= start,
        Availability.end <= end,
        Availability.booked == False
    ).all()
    for s in slots:
        s.booked = True
    session.commit()
    return appt