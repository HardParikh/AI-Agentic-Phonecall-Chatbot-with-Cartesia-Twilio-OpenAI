from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
from app.config import settings

engine = create_engine(settings.db_url, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class Barber(Base):
    __tablename__ = "barbers"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    services = relationship("Service", secondary="barber_services", back_populates="barbers")

class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)  # e.g., HAIRCUT, SHAVE
    name = Column(String, unique=True, nullable=False)
    duration_min = Column(Integer, nullable=False)
    price_cents = Column(Integer, nullable=False)
    barbers = relationship("Barber", secondary="barber_services", back_populates="services")

class BarberService(Base):
    __tablename__ = "barber_services"
    barber_id = Column(Integer, ForeignKey("barbers.id"), primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id"), primary_key=True)

class Availability(Base):
    __tablename__ = "availability"
    id = Column(Integer, primary_key=True)
    barber_id = Column(Integer, ForeignKey("barbers.id"))
    start = Column(DateTime, nullable=False)
    end = Column(DateTime, nullable=False)
    booked = Column(Boolean, default=False)
    barber = relationship("Barber")

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    customer_name = Column(String)
    phone = Column(String)
    barber_id = Column(Integer, ForeignKey("barbers.id"))
    service_id = Column(Integer, ForeignKey("services.id"))
    start = Column(DateTime)
    end = Column(DateTime)
    notes = Column(String)
    barber = relationship("Barber")
    service = relationship("Service")

# init
Base.metadata.create_all(engine)