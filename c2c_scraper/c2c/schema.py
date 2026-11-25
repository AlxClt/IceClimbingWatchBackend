"""SQLAlchemy schema of the camptocamp data: routes, outings, countries, orientations."""
import datetime
from typing import List
from sqlalchemy import ForeignKey, Table, Column, Integer, String, Date
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# Schema changes must come with an Alembic migration in c2c/migrations/versions/
# (applied at startup by c2c.db.migrate_db).


class Base(DeclarativeBase):
    pass


class IsoDate(TypeDecorator):
    """DATE column that also accepts 'YYYY-MM-DD' strings, as returned by the camptocamp API."""
    impl = Date
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, str):
            return datetime.date.fromisoformat(value)
        return value

# -----------------------
# Routes data
# -----------------------

class Routes(Base):
    __tablename__ = "routes"

    route_id=Column(Integer, primary_key=True)
    name=Column(String, nullable=True)
    lat: Mapped[float]
    lon: Mapped[float]
    snow_ice_mixed=Column(Integer, nullable=True)
    mountain_climbing=Column(Integer, nullable=True)
    ice_climbing=Column(Integer, nullable=True)
    elevation_min=Column(Integer, nullable=True)
    elevation_max=Column(Integer, nullable=True)
    difficulties_height=Column(Integer, nullable=True)
    height_diff_difficulties=Column(Integer, nullable=True)
    glacier=Column(String, nullable=True) 
    global_rating=Column(String, nullable=True) 
    ice_rating=Column(String, nullable=True) 
    mixed_rating=Column(String, nullable=True) 
    rock_free_rating=Column(String, nullable=True)
    last_updated=Column(IsoDate, nullable=True)

    orientations: Mapped[List["Orientations"]] = relationship(
        secondary="orientation_mapping", #For a many-to-many relationship, specifies the intermediary table, and is typically an instance of Table
    )

    countries: Mapped[List["Countries"]] = relationship(
        secondary="countries_mapping", 
    )

    outings: Mapped[List["Outings"]] = relationship(
        secondary="outings_mapping", back_populates='routes'
    )
    
    def __repr__(self):
         return f"Route(id={self.route_id}, link=https://www.camptocamp.org/routes/{self.route_id}, count_outings={len(self.outings)})"


# From https://docs.sqlalchemy.org/en/20/orm/basic_relationships.html#many-to-many:
# note for a Core table, we use the sqlalchemy.Column construct,
# not sqlalchemy.orm.mapped_column
orientation_mapping = Table(
    "orientation_mapping",
    Base.metadata,
    Column("route_id", ForeignKey("routes.route_id"), primary_key=True),
    Column("orientation_id", ForeignKey("orientations.orientation_id"), primary_key=True),
)

countries_mapping = Table(
    "countries_mapping",
    Base.metadata,
    Column("route_id", ForeignKey("routes.route_id"), primary_key=True),
    Column("country_id", ForeignKey("countries.country_id"), primary_key=True),
)

outings_mapping = Table(
    "outings_mapping",
    Base.metadata,
    Column("route_id", ForeignKey("routes.route_id"), primary_key=True),
    Column("outing_id", ForeignKey("outings.outing_id"), primary_key=True),
)

class Countries(Base):
    __tablename__ = "countries"

    country_id=Column(Integer, primary_key=True)
    countryName: Mapped[str] = mapped_column(nullable=True, unique=True)

    def __repr__(self):
         return f"Country(id={self.country_id}, name={self.countryName})"

class Orientations(Base):
    __tablename__ = "orientations"

    orientation_id=Column(Integer, primary_key=True)
    orientation: Mapped[str] = mapped_column(unique=True)

    def __repr__(self):
         return f"Orientation(id={self.orientation_id}, orientation={self.orientation})"

class Outings(Base):
    __tablename__ = "outings"

    outing_id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[datetime.date] = mapped_column(IsoDate)
    conditions=Column(String, nullable=True)
    last_updated=Column(IsoDate, nullable=True)
    
    routes: Mapped[List["Routes"]] = relationship(
        secondary="outings_mapping", back_populates='outings'
    )

    def __repr__(self):
         return f"Outing(id={self.outing_id}, link=https://www.camptocamp.org/outings/{self.outing_id})"
